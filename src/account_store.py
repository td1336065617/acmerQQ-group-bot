"""账号/群排行/Rating 快照的本地 SQLite 存储层。

设计约束：
- 不引入任何第三方依赖：默认使用标准库 ``sqlite3``，所有操作经
  ``asyncio.to_thread`` 以“短连接”方式执行（WAL 支持并发读，写操作天然串行）。
- 仅在环境已安装 ``aiosqlite`` 时作为可选加速（运行时探测），不进 requirements。
- 数据落在 AstrBot 数据目录 ``data/plugin_data/acmer_qq_group_bot/acmer_store.db``，
  与插件代码目录分离，避免升级/重装插件时丢数据。

迁移语义与旧 AstrBot KV 保持一致：
- 同一 OJ 账号（platform + 小写 platform_user_id）全局唯一属于一个 QQ 用户；
- 群成员 enabled 记录沿用旧结构；
- pending 绑定只存验证码哈希；
- rating_history 保留“同值 15 分钟内不重复写、每用户每平台最多 90 条”的旧语义。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from astrbot.api import logger

try:
    import aiosqlite  # type: ignore

    _HAS_AIOSQLITE = True
except Exception:  # noqa: BLE001 - 可选依赖
    aiosqlite = None  # type: ignore[assignment]
    _HAS_AIOSQLITE = False

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    user_id          TEXT NOT NULL,
    platform         TEXT NOT NULL,
    handle           TEXT NOT NULL,
    platform_user_id TEXT NOT NULL,
    display_name     TEXT NOT NULL DEFAULT '',
    profile_url      TEXT NOT NULL DEFAULT '',
    qq_name          TEXT NOT NULL DEFAULT '',
    verified_at      REAL NOT NULL,
    PRIMARY KEY (user_id, platform)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_accounts_platform_handle
    ON accounts (platform, lower(platform_user_id));
CREATE INDEX IF NOT EXISTS idx_accounts_user ON accounts (user_id);

CREATE TABLE IF NOT EXISTS group_members (
    group_id   TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL,
    PRIMARY KEY (group_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_members_group_enabled
    ON group_members (group_id, enabled);

CREATE TABLE IF NOT EXISTS pending_bindings (
    user_id          TEXT NOT NULL,
    platform         TEXT NOT NULL,
    token_hash       TEXT NOT NULL,
    group_id         TEXT NOT NULL DEFAULT '',
    handle           TEXT NOT NULL DEFAULT '',
    platform_user_id TEXT NOT NULL DEFAULT '',
    created_at       REAL NOT NULL,
    expires_at       REAL NOT NULL,
    PRIMARY KEY (user_id, platform)
);
CREATE INDEX IF NOT EXISTS idx_pending_expires ON pending_bindings (expires_at);

CREATE TABLE IF NOT EXISTS rating_history (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  TEXT NOT NULL,
    platform TEXT NOT NULL,
    ts       REAL NOT NULL,
    rating   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_user_platform_ts
    ON rating_history (user_id, platform, ts DESC, seq DESC);

CREATE TABLE IF NOT EXISTS profile_cache (
    platform    TEXT NOT NULL,
    handle_norm TEXT NOT NULL,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    fetched_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    PRIMARY KEY (platform, handle_norm, kind)
);
CREATE INDEX IF NOT EXISTS idx_profile_cache_expire ON profile_cache (expires_at);

CREATE TABLE IF NOT EXISTS fetch_failures (
    platform    TEXT NOT NULL,
    handle_norm TEXT NOT NULL,
    kind        TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    temporary   INTEGER NOT NULL DEFAULT 1,
    expires_at  REAL NOT NULL,
    PRIMARY KEY (platform, handle_norm, kind)
);
CREATE INDEX IF NOT EXISTS idx_failures_expire ON fetch_failures (expires_at);

CREATE TABLE IF NOT EXISTS rank_snapshot (
    group_id      TEXT NOT NULL,
    platform      TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    handle        TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    metric_label  TEXT NOT NULL,
    display_value TEXT NOT NULL,
    sort_value    INTEGER NOT NULL,
    delta         INTEGER,
    current_metric_label TEXT NOT NULL DEFAULT '',
    current_display_value TEXT NOT NULL DEFAULT '',
    rating        INTEGER,
    rating_rank   INTEGER,
    rating_rank_total INTEGER,
    rating_rank_note  TEXT,
    updated_at    REAL NOT NULL,
    PRIMARY KEY (group_id, platform, user_id)
);
CREATE INDEX IF NOT EXISTS idx_rank_group_platform_sort
    ON rank_snapshot (group_id, platform, sort_value DESC);

CREATE TABLE IF NOT EXISTS rank_meta (
    group_id     TEXT NOT NULL,
    platform     TEXT NOT NULL,
    refreshed_at REAL NOT NULL DEFAULT 0,
    in_flight    INTEGER NOT NULL DEFAULT 0,
    dirty_at     REAL NOT NULL DEFAULT 0,
    errors_json  TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (group_id, platform)
);

-- 本周进步榜物化读模型：与 rank_snapshot 结构一致，独立成表以避免
-- 修改既有表主键（SQLite 不支持 ALTER PRIMARY KEY），对现有库零风险。
CREATE TABLE IF NOT EXISTS progress_snapshot (
    group_id      TEXT NOT NULL,
    platform      TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    handle        TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    metric_label  TEXT NOT NULL,
    display_value TEXT NOT NULL,
    sort_value    INTEGER NOT NULL,
    delta         INTEGER,
    current_metric_label TEXT NOT NULL DEFAULT '',
    current_display_value TEXT NOT NULL DEFAULT '',
    rating        INTEGER,
    rating_rank   INTEGER,
    rating_rank_total INTEGER,
    rating_rank_note  TEXT,
    updated_at    REAL NOT NULL,
    PRIMARY KEY (group_id, platform, user_id)
);
CREATE INDEX IF NOT EXISTS idx_progress_group_platform_sort
    ON progress_snapshot (group_id, platform, sort_value DESC);

CREATE TABLE IF NOT EXISTS progress_meta (
    group_id     TEXT NOT NULL,
    platform     TEXT NOT NULL,
    refreshed_at REAL NOT NULL DEFAULT 0,
    in_flight    INTEGER NOT NULL DEFAULT 0,
    dirty_at     REAL NOT NULL DEFAULT 0,
    errors_json  TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (group_id, platform)
);
"""

# 快照模式 → (快照表, 元数据表)。rank=群排行，progress=本周进步榜。
_RANK_TABLES: Dict[str, Tuple[str, str]] = {
    "rank": ("rank_snapshot", "rank_meta"),
    "progress": ("progress_snapshot", "progress_meta"),
}


def _rank_tables(mode: str) -> Tuple[str, str]:
    key = "progress" if str(mode).lower() == "progress" else "rank"
    return _RANK_TABLES[key]


def _now() -> float:
    return time.time()


#: 快照表新增列：(表名, 列名, 类型)。线上旧库没有迁移框架，
#: 用 PRAGMA table_info 判定缺列后 ALTER；失败只记 warning，不阻塞启动。
_RANK_SNAPSHOT_NEW_COLUMNS: Tuple[Tuple[str, str, str], ...] = (
    ("rank_snapshot", "rating_rank", "INTEGER"),
    ("rank_snapshot", "rating_rank_total", "INTEGER"),
    ("rank_snapshot", "rating_rank_note", "TEXT"),
    ("progress_snapshot", "rating_rank", "INTEGER"),
    ("progress_snapshot", "rating_rank_total", "INTEGER"),
    ("progress_snapshot", "rating_rank_note", "TEXT"),
)


def _ensure_rank_snapshot_columns(conn: sqlite3.Connection) -> None:
    """幂等补齐快照表缺失列（目前是 rating_rank），已存在则跳过。"""
    for table, column, column_type in _RANK_SNAPSHOT_NEW_COLUMNS:
        try:
            existing = {
                str(row[1])
                for row in conn.execute(f"PRAGMA table_info({table})")
            }
        except sqlite3.Error as exc:
            logger.warning(
                "读取 %s 表结构失败，跳过 %s 列迁移：%s", table, column, exc
            )
            continue
        if column in existing:
            continue
        try:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"
            )
        except sqlite3.Error as exc:
            logger.warning(
                "为 %s 增加 %s 列失败（不影响启动）：%s", table, column, exc
            )


class AccountStore:
    """SQLite 存储实现；所有 public 方法均为 async（经 to_thread 执行）。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    # ------------------------------------------------------------------
    # 连接与建表
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _execute_sync(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(sql, params)
        finally:
            conn.close()

    def _query_sync(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def _schema_version_sync(self) -> int:
        conn = self._connect()
        try:
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()
            except sqlite3.OperationalError:
                return 0
            if row is None:
                return 0
            return int(row[0])
        finally:
            conn.close()

    async def initialize(self) -> None:
        """建目录、建表；幂等。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        def _init() -> None:
            conn = self._connect()
            try:
                with conn:
                    conn.executescript(_SCHEMA_SQL)
                    # 兼容早期 schema 缺列的库
                    for column_sql in (
                        "ALTER TABLE rank_meta "
                        "ADD COLUMN errors_json TEXT NOT NULL DEFAULT '[]'",
                        "ALTER TABLE rank_snapshot "
                        "ADD COLUMN current_metric_label TEXT NOT NULL DEFAULT ''",
                        "ALTER TABLE rank_snapshot "
                        "ADD COLUMN current_display_value TEXT NOT NULL DEFAULT ''",
                        "ALTER TABLE rank_snapshot "
                        "ADD COLUMN rating INTEGER",
                    ):
                        try:
                            conn.execute(column_sql)
                        except sqlite3.OperationalError:
                            pass
                    _ensure_rank_snapshot_columns(conn)
                    conn.execute(
                        "INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version', ?)",
                        (str(SCHEMA_VERSION),),
                    )
            finally:
                conn.close()

        await asyncio.to_thread(_init)

    async def schema_version(self) -> int:
        return await asyncio.to_thread(self._schema_version_sync)

    async def is_kv_migrated(self) -> bool:
        rows = await asyncio.to_thread(
            self._query_sync,
            "SELECT value FROM meta WHERE key='kv_migrated'",
        )
        return bool(rows and str(rows[0].get("value") or "") in {"1", "true", "yes"})

    async def set_kv_migrated(self) -> None:
        await asyncio.to_thread(
            self._execute_sync,
            "INSERT OR REPLACE INTO meta(key,value) VALUES('kv_migrated','1')",
        )

    # ------------------------------------------------------------------
    # accounts（绑定关系）
    # ------------------------------------------------------------------
    async def get_user_accounts(self, user_id: str) -> Dict[str, Dict[str, Any]]:
        rows = await asyncio.to_thread(
            self._query_sync,
            "SELECT * FROM accounts WHERE user_id=?",
            (str(user_id),),
        )
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            result[row["platform"]] = dict(row)
        return result

    async def get_all_accounts(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        rows = await asyncio.to_thread(self._query_sync, "SELECT * FROM accounts")
        result: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for row in rows:
            result.setdefault(row["user_id"], {})[row["platform"]] = dict(row)
        return result

    async def get_group_platform_accounts(
        self, group_id: str, platform: str
    ) -> List[Dict[str, Any]]:
        """返回该群已启用成员在该平台上的绑定记录（含 user_id）。"""

        def _query() -> List[Dict[str, Any]]:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT m.user_id AS user_id, a.*
                    FROM group_members m
                    JOIN accounts a ON a.user_id = m.user_id
                    WHERE m.group_id=? AND m.enabled=1 AND a.platform=?
                    """,
                    (str(group_id), str(platform)),
                ).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

        return await asyncio.to_thread(_query)

    async def save_binding(
        self,
        user_id: str,
        platform: str,
        account: Dict[str, Any],
    ) -> None:
        """保存绑定；同平台同账号已被他人绑定时抛 ValueError。"""

        def _save() -> None:
            conn = self._connect()
            try:
                with conn:
                    conflict = conn.execute(
                        """
                        SELECT user_id FROM accounts
                        WHERE platform=? AND lower(platform_user_id)=?
                          AND user_id<>?
                        LIMIT 1
                        """,
                        (
                            platform,
                            str(account["platform_user_id"]).casefold(),
                            str(user_id),
                        ),
                    ).fetchone()
                    if conflict is not None:
                        raise ValueError("这个平台账号已经绑定到其他 QQ 用户")
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO accounts(
                            user_id, platform, handle, platform_user_id,
                            display_name, profile_url, qq_name, verified_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(user_id),
                            platform,
                            str(account.get("handle") or ""),
                            str(account.get("platform_user_id") or ""),
                            str(account.get("display_name") or ""),
                            str(account.get("profile_url") or ""),
                            str(account.get("qq_name") or ""),
                            float(account.get("verified_at") or _now()),
                        ),
                    )
            finally:
                conn.close()

        await asyncio.to_thread(_save)

    async def save_binding_atomic(
        self,
        user_id: str,
        platform: str,
        account: Dict[str, Any],
        *,
        group_id: Optional[str] = None,
    ) -> None:
        """原子保存绑定 + 自动加入发起群 + 清理 pending。

        使用 BEGIN IMMEDIATE 串行化写入，并在拿到写锁后重查唯一冲突，
        避免两个用户并发绑定同一 OJ 账号时 REPLACE 顶号。
        """

        def _save() -> None:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conflict = conn.execute(
                        """
                        SELECT user_id FROM accounts
                        WHERE platform=? AND lower(platform_user_id)=?
                          AND user_id<>?
                        LIMIT 1
                        """,
                        (
                            platform,
                            str(account["platform_user_id"]).casefold(),
                            str(user_id),
                        ),
                    ).fetchone()
                    if conflict is not None:
                        raise ValueError("这个平台账号已经绑定到其他 QQ 用户")
                    try:
                        conn.execute(
                            """
                            INSERT INTO accounts(
                                user_id, platform, handle, platform_user_id,
                                display_name, profile_url, qq_name, verified_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(user_id, platform) DO UPDATE SET
                                handle=excluded.handle,
                                platform_user_id=excluded.platform_user_id,
                                display_name=excluded.display_name,
                                profile_url=excluded.profile_url,
                                qq_name=excluded.qq_name,
                                verified_at=excluded.verified_at
                            """,
                            (
                                str(user_id),
                                platform,
                                str(account.get("handle") or ""),
                                str(account.get("platform_user_id") or ""),
                                str(account.get("display_name") or ""),
                                str(account.get("profile_url") or ""),
                                str(account.get("qq_name") or ""),
                                float(account.get("verified_at") or _now()),
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise ValueError(
                            "这个平台账号已经绑定到其他 QQ 用户"
                        ) from exc
                    if group_id:
                        conn.execute(
                            """
                            INSERT INTO group_members(group_id, user_id, enabled, updated_at)
                            VALUES (?, ?, 1, ?)
                            ON CONFLICT(group_id, user_id)
                            DO UPDATE SET enabled=1, updated_at=excluded.updated_at
                            """,
                            (str(group_id), str(user_id), _now()),
                        )
                    conn.execute(
                        "DELETE FROM pending_bindings WHERE user_id=? AND platform=?",
                        (str(user_id), platform),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()

        await asyncio.to_thread(_save)

    async def set_user_display_name(self, user_id: str, qq_name: str) -> bool:
        def _update() -> bool:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        "UPDATE accounts SET qq_name=? WHERE user_id=? AND qq_name<>?",
                        (str(qq_name), str(user_id), str(qq_name)),
                    )
                return cur.rowcount > 0
            finally:
                conn.close()

        return await asyncio.to_thread(_update)

    async def remove_binding(self, user_id: str, platform: str) -> bool:
        def _remove() -> bool:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        "DELETE FROM accounts WHERE user_id=? AND platform=?",
                        (str(user_id), str(platform)),
                    )
                return cur.rowcount > 0
            finally:
                conn.close()

        return await asyncio.to_thread(_remove)

    async def remove_binding_atomic(self, user_id: str, platform: str) -> bool:
        """原子解绑：同一事务删除账号与待确认绑定。"""

        def _remove() -> bool:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    cur = conn.execute(
                        "DELETE FROM accounts WHERE user_id=? AND platform=?",
                        (str(user_id), str(platform)),
                    )
                    removed = cur.rowcount > 0
                    conn.execute(
                        "DELETE FROM pending_bindings WHERE user_id=? AND platform=?",
                        (str(user_id), str(platform)),
                    )
                    conn.execute("COMMIT")
                    return removed
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()

        return await asyncio.to_thread(_remove)

    # ------------------------------------------------------------------
    # group_members
    # ------------------------------------------------------------------
    async def set_group_member(
        self,
        group_id: str,
        user_id: str,
        enabled: bool,
        *,
        preserve_opt_out: bool = False,
    ) -> bool:
        def _set() -> bool:
            conn = self._connect()
            try:
                with conn:
                    existing = conn.execute(
                        "SELECT enabled FROM group_members WHERE group_id=? AND user_id=?",
                        (str(group_id), str(user_id)),
                    ).fetchone()
                    if existing is not None:
                        if preserve_opt_out and not bool(existing["enabled"]):
                            return False
                        if bool(existing["enabled"]) == enabled:
                            return False
                    conn.execute(
                        """
                        INSERT INTO group_members(group_id, user_id, enabled, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(group_id, user_id)
                        DO UPDATE SET enabled=excluded.enabled,
                                      updated_at=excluded.updated_at
                        """,
                        (str(group_id), str(user_id), 1 if enabled else 0, _now()),
                    )
                    return True
            finally:
                conn.close()

        return await asyncio.to_thread(_set)

    async def get_group_member_ids(
        self, group_id: str, *, enabled_only: bool = True
    ) -> List[str]:
        sql = (
            "SELECT user_id FROM group_members WHERE group_id=?"
            + (" AND enabled=1" if enabled_only else "")
        )

        def _query() -> List[str]:
            conn = self._connect()
            try:
                rows = conn.execute(sql, (str(group_id),)).fetchall()
                return [str(row["user_id"]) for row in rows]
            finally:
                conn.close()

        return await asyncio.to_thread(_query)

    async def disable_user_in_all_groups(self, user_id: str) -> None:
        await asyncio.to_thread(
            self._execute_sync,
            "UPDATE group_members SET enabled=0, updated_at=? WHERE user_id=?",
            (_now(), str(user_id)),
        )

    # ------------------------------------------------------------------
    # pending_bindings
    # ------------------------------------------------------------------
    async def create_pending(
        self,
        user_id: str,
        platform: str,
        *,
        token_hash: str,
        group_id: str,
        expires_at: float,
        handle: str = "",
        platform_user_id: str = "",
    ) -> None:
        await asyncio.to_thread(
            self._execute_sync,
            """
            INSERT OR REPLACE INTO pending_bindings(
                user_id, platform, token_hash, group_id,
                handle, platform_user_id, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(user_id),
                str(platform),
                str(token_hash),
                str(group_id),
                str(handle),
                str(platform_user_id),
                _now(),
                float(expires_at),
            ),
        )

    async def get_pending(
        self, user_id: str, platform: str
    ) -> Optional[Dict[str, Any]]:
        rows = await asyncio.to_thread(
            self._query_sync,
            "SELECT * FROM pending_bindings WHERE user_id=? AND platform=?",
            (str(user_id), str(platform)),
        )
        if not rows:
            return None
        return rows[0]

    async def clear_pending(self, user_id: str, platform: str) -> None:
        await asyncio.to_thread(
            self._execute_sync,
            "DELETE FROM pending_bindings WHERE user_id=? AND platform=?",
            (str(user_id), str(platform)),
        )

    async def purge_expired_pending(self) -> int:
        def _purge() -> int:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        "DELETE FROM pending_bindings WHERE expires_at < ?",
                        (_now(),),
                    )
                return cur.rowcount
            finally:
                conn.close()

        return await asyncio.to_thread(_purge)

    # ------------------------------------------------------------------
    # rating_history（沿用旧 record_ratings 语义）
    # ------------------------------------------------------------------
    async def record_ratings(
        self, entries: List[Tuple[str, str, Optional[int]]]
    ) -> None:
        if not entries:
            return
        normalized: Dict[Tuple[str, str], int] = {}
        for user_id, platform, rating in entries:
            if rating is None:
                continue
            try:
                normalized[(str(user_id), str(platform))] = int(rating)
            except (TypeError, ValueError):
                continue
        if not normalized:
            return

        def _record() -> None:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    now = _now()
                    for (user_id, platform), value in normalized.items():
                        last = conn.execute(
                            """
                            SELECT rating, ts FROM rating_history
                            WHERE user_id=? AND platform=?
                            ORDER BY ts DESC, seq DESC LIMIT 1
                            """,
                            (user_id, platform),
                        ).fetchone()
                        if last is not None:
                            try:
                                same_value = int(last["rating"]) == value
                                last_time = float(last["ts"])
                            except (TypeError, ValueError):
                                same_value, last_time = False, 0.0
                            if same_value and now - last_time < 15 * 60:
                                continue
                        conn.execute(
                            """
                            INSERT INTO rating_history(user_id, platform, ts, rating)
                            VALUES (?, ?, ?, ?)
                            """,
                            (user_id, platform, now, value),
                        )
                        conn.execute(
                            """
                            DELETE FROM rating_history
                            WHERE user_id=? AND platform=? AND seq NOT IN (
                                SELECT seq FROM rating_history
                                WHERE user_id=? AND platform=?
                                ORDER BY ts DESC, seq DESC LIMIT 90
                            )
                            """,
                            (user_id, platform, user_id, platform),
                        )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()

        await asyncio.to_thread(_record)

    async def get_weekly_deltas(
        self,
        requests: List[Tuple[str, str]],
        *,
        days: int = 7,
        now: Optional[float] = None,
    ) -> Dict[Tuple[str, str], Optional[int]]:
        if not requests:
            return {}
        current = _now() if now is None else float(now)
        cutoff = current - max(1, int(days)) * 86400
        # 保留请求顺序与旧实现一致的键值语义。
        unique = list(dict.fromkeys((str(u), str(p)) for u, p in requests))

        def _query() -> List[Dict[str, Any]]:
            conn = self._connect()
            try:
                result: List[Dict[str, Any]] = []
                for user_id, platform in unique:
                    latest = conn.execute(
                        """
                        SELECT rating FROM rating_history
                        WHERE user_id=? AND platform=?
                        ORDER BY ts DESC, seq DESC LIMIT 1
                        """,
                        (user_id, platform),
                    ).fetchone()
                    if latest is None:
                        result.append({"user_id": user_id, "platform": platform,
                                       "delta": None})
                        continue
                    current_rating = int(latest["rating"])
                    baseline_row = conn.execute(
                        """
                        SELECT rating FROM rating_history
                        WHERE user_id=? AND platform=? AND ts<=?
                        ORDER BY ts DESC, seq DESC LIMIT 1
                        """,
                        (user_id, platform, cutoff),
                    ).fetchone()
                    baseline = (
                        int(baseline_row["rating"])
                        if baseline_row is not None
                        else None
                    )
                    delta = (
                        current_rating - baseline
                        if baseline is not None
                        else None
                    )
                    result.append({"user_id": user_id, "platform": platform,
                                   "delta": delta})
                return result
            finally:
                conn.close()

        rows = await asyncio.to_thread(_query)
        return {
            (str(row["user_id"]), str(row["platform"])): row["delta"]
            for row in rows
        }

    # ------------------------------------------------------------------
    # 迁移 / 统计
    # ------------------------------------------------------------------
    async def migrate_from_kv(
        self, legacy: Dict[str, Any]
    ) -> Dict[str, int]:
        """把旧 KV 快照迁入 SQLite；返回各表统计。"""
        accounts = legacy.get("linked_accounts") or {}
        members = legacy.get("group_rank_members") or {}
        pending = legacy.get("pending_account_bindings") or {}
        snapshots = legacy.get("account_rating_snapshots") or {}
        if not isinstance(accounts, dict):
            accounts = {}
        if not isinstance(members, dict):
            members = {}
        if not isinstance(pending, dict):
            pending = {}
        if not isinstance(snapshots, dict):
            snapshots = {}
        counts = {
            "accounts": 0,
            "members": 0,
            "pending": 0,
            "history": 0,
        }

        def _migrate() -> None:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    marker = conn.execute(
                        "SELECT value FROM meta WHERE key='kv_migrated'"
                    ).fetchone()
                    if marker is not None:
                        raise RuntimeError("账号数据已迁移，拒绝重复执行 migrate_from_kv")
                    # 只清理 KV 迁移对应的源表；不动运行期缓存/排行表。
                    for table in (
                        "accounts",
                        "group_members",
                        "pending_bindings",
                        "rating_history",
                    ):
                        conn.execute(f"DELETE FROM {table}")
                    for user_id, platforms in accounts.items():
                        if not isinstance(platforms, dict):
                            continue
                        for platform, record in platforms.items():
                            if not isinstance(record, dict):
                                continue
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO accounts(
                                    user_id, platform, handle, platform_user_id,
                                    display_name, profile_url, qq_name, verified_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    str(user_id),
                                    str(platform),
                                    str(record.get("handle") or ""),
                                    str(record.get("platform_user_id") or ""),
                                    str(record.get("display_name") or ""),
                                    str(record.get("profile_url") or ""),
                                    str(record.get("qq_name") or ""),
                                    float(record.get("verified_at") or _now()),
                                ),
                            )
                            counts["accounts"] += 1
                    for group_id, users in members.items():
                        if not isinstance(users, dict):
                            continue
                        for user_id, item in users.items():
                            if not isinstance(item, dict):
                                continue
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO group_members(
                                    group_id, user_id, enabled, updated_at
                                ) VALUES (?, ?, ?, ?)
                                """,
                                (
                                    str(group_id),
                                    str(user_id),
                                    1 if bool(item.get("enabled", False)) else 0,
                                    float(item.get("updated_at") or _now()),
                                ),
                            )
                            counts["members"] += 1
                    for key, item in pending.items():
                        if not isinstance(item, dict):
                            continue
                        user_id, sep, platform = str(key).partition(":")
                        if not sep:
                            continue
                        try:
                            expires_at = float(item.get("expires_at") or 0)
                        except (TypeError, ValueError):
                            expires_at = 0.0
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO pending_bindings(
                                user_id, platform, token_hash, group_id,
                                handle, platform_user_id, created_at, expires_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                user_id,
                                platform,
                                str(item.get("token_hash") or ""),
                                str(item.get("group_id") or ""),
                                str(item.get("handle") or ""),
                                str(item.get("platform_user_id") or ""),
                                float(item.get("created_at") or _now()),
                                expires_at,
                            ),
                        )
                        counts["pending"] += 1
                    for user_id, platforms in snapshots.items():
                        if not isinstance(platforms, dict):
                            continue
                        for platform, history in platforms.items():
                            if not isinstance(history, list):
                                continue
                            for entry in history:
                                if not isinstance(entry, dict):
                                    continue
                                try:
                                    ts = float(entry.get("timestamp") or 0)
                                    rating = int(entry.get("rating"))
                                except (TypeError, ValueError):
                                    continue
                                conn.execute(
                                    """
                                    INSERT OR REPLACE INTO rating_history(
                                        user_id, platform, ts, rating
                                    ) VALUES (?, ?, ?, ?)
                                    """,
                                    (str(user_id), str(platform), ts, rating),
                                )
                                counts["history"] += 1
                    # 迁移后同样遵守“每用户每平台最多 90 条”的旧不变量。
                    for user_id, platforms in snapshots.items():
                        if not isinstance(platforms, dict):
                            continue
                        for platform in platforms:
                            conn.execute(
                                """
                                DELETE FROM rating_history
                                WHERE user_id=? AND platform=? AND seq NOT IN (
                                    SELECT seq FROM rating_history
                                    WHERE user_id=? AND platform=?
                                    ORDER BY ts DESC, seq DESC LIMIT 90
                                )
                                """,
                                (
                                    str(user_id),
                                    str(platform),
                                    str(user_id),
                                    str(platform),
                                ),
                            )
                    conn.execute(
                        "INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version', ?)",
                        (str(SCHEMA_VERSION),),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO meta(key,value) VALUES('kv_migrated','1')"
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()

        await asyncio.to_thread(_migrate)
        return counts

    # ------------------------------------------------------------------
    # profile_cache / fetch_failures（持久化资料缓存与负缓存）
    # ------------------------------------------------------------------
    async def load_profile_cache(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(
            self._query_sync,
            "SELECT platform, handle_norm, kind, payload, fetched_at, expires_at "
            "FROM profile_cache WHERE expires_at > ?",
            (_now(),),
        )

    async def upsert_profile_cache(
        self, entries: List[Dict[str, Any]]
    ) -> None:
        if not entries:
            return

        def _upsert() -> None:
            conn = self._connect()
            try:
                with conn:
                    for entry in entries:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO profile_cache(
                                platform, handle_norm, kind, payload,
                                fetched_at, expires_at
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(entry["platform"]),
                                str(entry["handle_norm"]),
                                str(entry["kind"]),
                                str(entry["payload"]),
                                float(entry["fetched_at"]),
                                float(entry["expires_at"]),
                            ),
                        )
            finally:
                conn.close()

        await asyncio.to_thread(_upsert)

    async def delete_expired_profile_cache(self) -> int:
        def _delete() -> int:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        "DELETE FROM profile_cache WHERE expires_at < ?",
                        (_now(),),
                    )
                return cur.rowcount
            finally:
                conn.close()

        return await asyncio.to_thread(_delete)

    async def load_fetch_failures(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(
            self._query_sync,
            "SELECT platform, handle_norm, kind, reason, temporary, expires_at "
            "FROM fetch_failures WHERE expires_at > ?",
            (_now(),),
        )

    async def upsert_fetch_failures(
        self, entries: List[Dict[str, Any]]
    ) -> None:
        if not entries:
            return

        def _upsert() -> None:
            conn = self._connect()
            try:
                with conn:
                    for entry in entries:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO fetch_failures(
                                platform, handle_norm, kind, reason,
                                temporary, expires_at
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(entry["platform"]),
                                str(entry["handle_norm"]),
                                str(entry.get("kind") or ""),
                                str(entry.get("reason") or ""),
                                0 if bool(entry.get("temporary", False)) is False else 1,
                                float(entry["expires_at"]),
                            ),
                        )
            finally:
                conn.close()

        await asyncio.to_thread(_upsert)

    async def delete_fetch_failure(
        self, platform: str, handle_norm: str, kind: str = ""
    ) -> None:
        await asyncio.to_thread(
            self._execute_sync,
            "DELETE FROM fetch_failures WHERE platform=? AND handle_norm=? AND kind=?",
            (str(platform), str(handle_norm), str(kind)),
        )

    async def delete_expired_fetch_failures(self) -> int:
        def _delete() -> int:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        "DELETE FROM fetch_failures WHERE expires_at < ?",
                        (_now(),),
                    )
                return cur.rowcount
            finally:
                conn.close()

        return await asyncio.to_thread(_delete)

    # ------------------------------------------------------------------
    # rank_snapshot / rank_meta（群排行物化读模型）
    # ------------------------------------------------------------------
    async def replace_rank_snapshot(
        self,
        group_id: str,
        platform: str,
        rows: List[Dict[str, Any]],
        *,
        mode: str = "rank",
    ) -> None:
        """事务内整体替换某群某平台的排行快照（mode=rank/progress）。"""
        table, _ = _rank_tables(mode)

        def _replace() -> None:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        f"DELETE FROM {table} WHERE group_id=? AND platform=?",
                        (str(group_id), str(platform)),
                    )
                    now = _now()
                    for row in rows:
                        conn.execute(
                            f"""
                            INSERT OR REPLACE INTO {table}(
                                group_id, platform, user_id, handle, display_name,
                                metric_label, display_value, sort_value, delta,
                                current_metric_label, current_display_value, rating,
                                rating_rank, rating_rank_total, rating_rank_note, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(group_id),
                                str(platform),
                                str(row.get("user_id") or ""),
                                str(row.get("handle") or ""),
                                str(row.get("display_name") or ""),
                                str(row.get("metric_label") or ""),
                                str(row.get("display_value") or ""),
                                int(row.get("sort_value") or 0),
                                (
                                    int(row["delta"])
                                    if row.get("delta") is not None
                                    else None
                                ),
                                str(row.get("current_metric_label") or ""),
                                str(row.get("current_display_value") or ""),
                                (
                                    int(row["rating"])
                                    if row.get("rating") is not None
                                    else None
                                ),
                                (
                                    int(row["rating_rank"])
                                    if row.get("rating_rank") is not None
                                    else None
                                ),
                                (
                                    int(row["rating_rank_total"])
                                    if row.get("rating_rank_total") is not None
                                    else None
                                ),
                                str(row.get("rating_rank_note") or ""),
                                float(row.get("updated_at") or now),
                            ),
                        )
            finally:
                conn.close()

        await asyncio.to_thread(_replace)

    async def get_rank_rows(
        self, group_id: str, platform: str, *, mode: str = "rank"
    ) -> List[Dict[str, Any]]:
        table, _ = _rank_tables(mode)
        rows = await asyncio.to_thread(
            self._query_sync,
            f"""
            SELECT * FROM {table}
            WHERE group_id=? AND platform=?
            ORDER BY sort_value DESC, display_name COLLATE NOCASE ASC, user_id ASC
            """,
            (str(group_id), str(platform)),
        )
        # 与旧 _collect_rank_rows 输出保持同名字段。
        normalized: List[Dict[str, Any]] = []
        for row in rows:
            display_value = str(row["display_value"])
            try:
                numeric_value = int(display_value)
            except (TypeError, ValueError):
                numeric_value = display_value
            normalized.append(
                {
                    "user_id": row["user_id"],
                    "display_name": row["display_name"],
                    "handle": row["handle"],
                    "value": numeric_value,
                    "display_value": display_value,
                    "metric_label": row["metric_label"],
                    "sort_value": row["sort_value"],
                    "delta": row["delta"],
                    "current_metric_label": row.get("current_metric_label") or "",
                    "current_display_value": row.get("current_display_value") or "",
                    "rating": row.get("rating"),
                    "rating_rank": row.get("rating_rank"),
                    "rating_rank_total": row.get("rating_rank_total"),
                    "rating_rank_note": row.get("rating_rank_note"),
                }
            )
        return normalized

    @staticmethod
    def _format_error(item: Any) -> List[Any]:
        """保持旧 (user_id, error) 二元组形态，便于调用方解包。"""
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            return [str(item[0]), str(item[1])]
        return ["", str(item)]

    async def touch_rank_meta(
        self,
        group_id: str,
        platform: str,
        errors: Optional[Sequence[Any]] = None,
        *,
        mode: str = "rank",
    ) -> None:
        _, meta_table = _rank_tables(mode)
        errors_json = json.dumps(
            [self._format_error(item) for item in (errors or [])],
            ensure_ascii=False,
        )
        await asyncio.to_thread(
            self._execute_sync,
            f"""
            INSERT INTO {meta_table}(
                group_id, platform, refreshed_at, in_flight, dirty_at, errors_json
            ) VALUES (?, ?, ?, 0, 0, ?)
            ON CONFLICT(group_id, platform) DO UPDATE SET
                refreshed_at=excluded.refreshed_at,
                in_flight=0,
                dirty_at=0,
                errors_json=excluded.errors_json
            """,
            (str(group_id), str(platform), _now(), errors_json),
        )

    async def touch_rank_meta_preserving_dirty(
        self,
        group_id: str,
        platform: str,
        errors: Optional[Sequence[Any]] = None,
        refresh_started_at: Optional[float] = None,
        *,
        mode: str = "rank",
    ) -> None:
        """刷新成功后更新 refreshed_at，但保留刷新开始之后产生的新脏标记。

        避免“变更前的后台刷新完成，把刚产生的 dirty 清掉”。
        """
        _, meta_table = _rank_tables(mode)
        errors_json = json.dumps(
            [self._format_error(item) for item in (errors or [])],
            ensure_ascii=False,
        )
        started = float(refresh_started_at or _now())
        await asyncio.to_thread(
            self._execute_sync,
            f"""
            INSERT INTO {meta_table}(
                group_id, platform, refreshed_at, in_flight, dirty_at, errors_json
            ) VALUES (?, ?, ?, 0, 0, ?)
            ON CONFLICT(group_id, platform) DO UPDATE SET
                refreshed_at=excluded.refreshed_at,
                in_flight=0,
                dirty_at=CASE
                    WHEN {meta_table}.dirty_at > ? THEN {meta_table}.dirty_at
                    ELSE 0
                END,
                errors_json=excluded.errors_json
            """,
            (str(group_id), str(platform), _now(), errors_json, started),
        )

    async def mark_rank_dirty_with_errors(
        self,
        group_id: str,
        platform: str,
        errors: Optional[Sequence[Any]] = None,
        *,
        mode: str = "rank",
    ) -> None:
        """刷新出现错误时：保留错误摘要并把该快照标记为脏（短周期重试）。"""
        _, meta_table = _rank_tables(mode)
        errors_json = json.dumps(
            [self._format_error(item) for item in (errors or [])],
            ensure_ascii=False,
        )
        now = _now()
        await asyncio.to_thread(
            self._execute_sync,
            f"""
            INSERT INTO {meta_table}(
                group_id, platform, refreshed_at, in_flight, dirty_at, errors_json
            ) VALUES (?, ?, 0, 0, ?, ?)
            ON CONFLICT(group_id, platform) DO UPDATE SET
                in_flight=0,
                dirty_at=excluded.dirty_at,
                errors_json=excluded.errors_json
            """,
            (str(group_id), str(platform), now, errors_json),
        )

    async def mark_rank_dirty(
        self, group_id: str, platform: str, *, mode: str = "rank"
    ) -> None:
        _, meta_table = _rank_tables(mode)
        await asyncio.to_thread(
            self._execute_sync,
            f"""
            INSERT INTO {meta_table}(group_id, platform, refreshed_at, in_flight, dirty_at)
            VALUES (?, ?, 0, 0, ?)
            ON CONFLICT(group_id, platform) DO UPDATE SET dirty_at=excluded.dirty_at
            """,
            (str(group_id), str(platform), _now()),
        )

    async def list_stale_rank_meta(
        self,
        *,
        max_age: float,
        active_platforms: Optional[Sequence[str]] = None,
        mode: str = "rank",
    ) -> List[Dict[str, Any]]:
        """返回刷新时间超过 max_age（秒）或从未刷新的 (group, platform)。"""
        _, meta_table = _rank_tables(mode)

        def _query() -> List[Dict[str, Any]]:
            conn = self._connect()
            try:
                params: List[Any] = [_now() - float(max_age)]
                sql = f"""
                    SELECT group_id, platform, refreshed_at, dirty_at, in_flight
                    FROM {meta_table}
                    WHERE refreshed_at < ? OR dirty_at > refreshed_at
                """
                if active_platforms:
                    placeholders = ",".join("?" for _ in active_platforms)
                    sql += f" AND platform IN ({placeholders})"
                    params.extend(str(p) for p in active_platforms)
                return [dict(r) for r in conn.execute(sql, params).fetchall()]
            finally:
                conn.close()

        return await asyncio.to_thread(_query)

    async def get_rank_meta(
        self, group_id: str, platform: str, *, mode: str = "rank"
    ) -> Optional[Dict[str, Any]]:
        _, meta_table = _rank_tables(mode)
        rows = await asyncio.to_thread(
            self._query_sync,
            f"SELECT group_id, platform, refreshed_at, in_flight, dirty_at, errors_json "
            f"FROM {meta_table} WHERE group_id=? AND platform=?",
            (str(group_id), str(platform)),
        )
        return rows[0] if rows else None

    async def stats(self) -> Dict[str, int]:
        def _stats() -> Dict[str, int]:
            conn = self._connect()
            try:
                result: Dict[str, int] = {}
                for table in (
                    "accounts",
                    "group_members",
                    "pending_bindings",
                    "rating_history",
                    "profile_cache",
                    "fetch_failures",
                    "rank_snapshot",
                    "progress_snapshot",
                ):
                    row = conn.execute(
                        f"SELECT COUNT(*) AS n FROM {table}"
                    ).fetchone()
                    result[table] = int(row["n"]) if row else 0
                return result
            finally:
                conn.close()

        return await asyncio.to_thread(_stats)

    async def close(self) -> None:
        """短连接模型下无需持有连接；保留接口便于将来切换 aiosqlite。"""
        return None


def default_store_path() -> Path:
    """返回 AstrBot 数据目录下的默认 SQLite 路径。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        base = Path(get_astrbot_data_path())
    except Exception:  # noqa: BLE001 - 未运行在 AstrBot 中时回退
        base = Path("data")
    return base / "plugin_data" / "acmer_qq_group_bot" / "acmer_store.db"
