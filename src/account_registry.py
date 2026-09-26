"""账号绑定、群排行成员和 Rating 快照的持久化管理。

存储模式：
- 默认（store 未启用）：沿用 AstrBot KV，行为与旧版完全一致；
- 启用 SQLite（AccountStore）后：读写走本地库，并对 KV 做“尽力双写”
  （dual_write_kv=True），SQLite 故障时自动回退 KV，便于灰度迁移与回滚。
数据迁移：initialize() 发现 KV 有历史数据而 SQLite 为空时自动迁移，并在
SQLite 同目录写一份 JSON 备份；回滚可通过备份 JSON 恢复 KV。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger

from .account_models import AccountProfile
from .account_store import AccountStore, default_store_path

ACCOUNTS_KEY = "linked_accounts"
PENDING_BINDINGS_KEY = "pending_account_bindings"
GROUP_RANK_KEY = "group_rank_members"
#: 后台「强制加入排行」名单（KV 回退用）
RANK_MEMBER_KEY = "rank_member_overrides"
RATING_SNAPSHOTS_KEY = "account_rating_snapshots"

BINDING_TTL = 10 * 60
TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
TOKEN_RE = re.compile(r"ACM-[A-Z0-9]{8}", re.I)


def create_binding_token() -> str:
    return "ACM-" + "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(8))


def token_hash(token: str) -> str:
    return hashlib.sha256(str(token).strip().upper().encode("utf-8")).hexdigest()


class AccountRegistry:
    """账号/群成员/快照的存储门面：KV 或 SQLite（AccountStore）。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.store: Optional[AccountStore] = None
        self.store_enabled = False
        self.dual_write_kv = True

    # ------------------------------------------------------------------
    # KV 基础
    # ------------------------------------------------------------------
    async def _get(self, key: str, default):
        value = await self.plugin.get_kv_data(key, default)
        return value if value is not None else default

    async def _put(self, key: str, value) -> None:
        await self.plugin.put_kv_data(key, value)

    # ------------------------------------------------------------------
    # 初始化 / 迁移
    # ------------------------------------------------------------------
    async def initialize(
        self,
        *,
        enable: bool = True,
        db_path: Optional[str | Path] = None,
        dual_write_kv: bool = True,
    ) -> None:
        """启用 SQLite 存储；必要时从 KV 自动迁移。

        enable=False 时保持旧 KV 模式（供测试/回滚）。
        """
        self.dual_write_kv = bool(dual_write_kv)
        self.store_enabled = False
        if not enable:
            self.store = None
            return
        path = Path(db_path).expanduser().resolve() if db_path else default_store_path()
        try:
            store = AccountStore(path)
            await store.initialize()
            legacy = {
                ACCOUNTS_KEY: await self._get(ACCOUNTS_KEY, {}) or {},
                GROUP_RANK_KEY: await self._get(GROUP_RANK_KEY, {}) or {},
                PENDING_BINDINGS_KEY: await self._get(PENDING_BINDINGS_KEY, {}) or {},
                RATING_SNAPSHOTS_KEY: await self._get(RATING_SNAPSHOTS_KEY, {}) or {},
            }
            has_legacy = any(bool(value) for value in legacy.values())
            migrated = await store.is_kv_migrated()
            if has_legacy and not migrated:
                backup_path = path.with_name(
                    f"kv_backup_{time.strftime('%Y%m%d_%H%M%S')}_"
                    f"{os.getpid()}_{int(time.time() * 1000) % 1000000}.json"
                )
                try:
                    tmp_path = backup_path.with_suffix(".tmp")
                    tmp_path.write_text(
                        json.dumps(legacy, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    os.replace(tmp_path, backup_path)
                except OSError as exc:  # noqa: BLE001 - 备份失败不应阻断迁移
                    logger.warning("KV 备份写入失败（继续迁移）: %s", exc)
                counts = await store.migrate_from_kv(legacy)
                logger.info(
                    "acmerQQ群机器人 账号数据已从 KV 迁入 SQLite: %s", counts
                )
            elif has_legacy and migrated:
                logger.info(
                    "acmerQQ群机器人 SQLite 已迁移过，跳过 KV 迁移（KV 仍保留）"
                )
            self.store = store
            self.store_enabled = True
            logger.info("acmerQQ群机器人 AccountRegistry 使用 SQLite 存储")
        except Exception as exc:  # noqa: BLE001 - 存储故障不能拖垮插件启动
            logger.error(
                "AccountRegistry SQLite 初始化失败，回退 KV 存储: %s",
                exc,
                exc_info=True,
            )
            self.store = None
            self.store_enabled = False

    def _need_store(self) -> bool:
        return bool(self.store_enabled and self.store is not None)

    async def close(self) -> None:
        """关闭 SQLite 存储；短连接模型下为空操作，保留给未来 aiosqlite。"""
        if self.store is not None:
            try:
                await self.store.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("AccountRegistry 关闭存储失败: %s", exc)
        self.store = None
        self.store_enabled = False

    async def restore_kv_from_backup(self, path: str | Path) -> Dict[str, int]:
        """从迁移 JSON 备份恢复 4 个 KV key（回滚入口，需先停用 SQLite）。"""
        backup = Path(path).expanduser().resolve()
        data = json.loads(backup.read_text(encoding="utf-8"))
        counts: Dict[str, int] = {}
        for key in (
            ACCOUNTS_KEY,
            GROUP_RANK_KEY,
            PENDING_BINDINGS_KEY,
            RATING_SNAPSHOTS_KEY,
        ):
            value = data.get(key)
            await self._put(key, value if isinstance(value, (dict, list)) else {})
            counts[key] = (
                len(value) if isinstance(value, (dict, list)) else 0
            )
        return counts

    async def _dual(self, coro) -> None:
        """Store 写成功后，尽力同步到旧 KV；失败只告警。"""
        if not self.dual_write_kv:
            return
        try:
            await coro
        except Exception as exc:  # noqa: BLE001 - 双写失败不影响主存储
            logger.warning("账号数据 KV 双写失败: %s", exc)

    # ------------------------------------------------------------------
    # accounts
    # ------------------------------------------------------------------
    async def get_user_accounts(self, user_id: str) -> Dict[str, Dict[str, Any]]:
        if self._need_store():
            try:
                return await self.store.get_user_accounts(str(user_id))
            except Exception as exc:  # noqa: BLE001
                logger.warning("SQLite 读取账号失败，回退 KV: %s", exc)
        return await self._kv_get_user_accounts(str(user_id))

    async def get_all_accounts(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        if self._need_store():
            try:
                return await self.store.get_all_accounts()
            except Exception as exc:  # noqa: BLE001
                logger.warning("SQLite 读取全量账号失败，回退 KV: %s", exc)
        return await self._kv_get_all_accounts()

    async def save_binding(
        self,
        user_id: str,
        platform: str,
        profile: AccountProfile,
        *,
        group_id: Optional[str] = None,
        qq_name: str = "",
    ) -> None:
        user_key = str(user_id)
        account = {
            "platform": platform,
            "handle": profile.handle,
            "platform_user_id": profile.platform_user_id,
            "display_name": profile.display_name or profile.handle,
            "profile_url": profile.profile_url,
            "verified_at": time.time(),
            "qq_name": str(qq_name or "").strip(),
        }
        if self._need_store():
            try:
                await self.store.save_binding_atomic(
                    user_key,
                    platform,
                    account,
                    group_id=str(group_id) if group_id else None,
                )
            except ValueError:
                raise
            except Exception as exc:  # noqa: BLE001 - 存储故障回退 KV
                logger.warning("SQLite 保存绑定失败，回退 KV: %s", exc)
                await self._kv_save_binding(
                    user_key, platform, profile, group_id=group_id, qq_name=qq_name
                )
                return
            await self._dual(
                self._kv_save_binding(
                    user_key, platform, profile, group_id=group_id, qq_name=qq_name
                )
            )
            return
        await self._kv_save_binding(
            user_key, platform, profile, group_id=group_id, qq_name=qq_name
        )

    async def set_user_display_name(self, user_id: str, qq_name: str) -> bool:
        name = str(qq_name or "").strip()
        if not name:
            return False
        user_key = str(user_id)
        if self._need_store():
            try:
                changed = await self.store.set_user_display_name(user_key, name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("SQLite 更新昵称失败，回退 KV: %s", exc)
                changed = await self._kv_set_user_display_name(user_key, name)
            if changed:
                await self._dual(self._kv_set_user_display_name(user_key, name))
            return changed
        return await self._kv_set_user_display_name(user_key, name)

    async def remove_binding(self, user_id: str, platform: str) -> bool:
        user_key = str(user_id)
        if self._need_store():
            try:
                removed = await self.store.remove_binding_atomic(user_key, platform)
            except Exception as exc:  # noqa: BLE001
                logger.warning("SQLite 解绑失败，回退 KV: %s", exc)
                removed = await self._kv_remove_binding(user_key, platform)
            if removed:
                await self._dual(
                    self._kv_remove_binding(user_key, platform)
                )
                await self._dual(self._kv_clear_pending(user_key, platform))
            return removed
        return await self._kv_remove_binding(user_key, platform)

    # ------------------------------------------------------------------
    # pending bindings
    # ------------------------------------------------------------------
    async def create_pending(
        self,
        user_id: str,
        platform: str,
        profile: AccountProfile,
        *,
        group_id: Optional[str] = None,
    ) -> str:
        token = create_binding_token()
        user_key = str(user_id)
        if self._need_store():
            try:
                await self.store.create_pending(
                    user_key,
                    platform,
                    token_hash=token_hash(token),
                    group_id=str(group_id) if group_id else "",
                    expires_at=time.time() + BINDING_TTL,
                    handle=profile.handle,
                    platform_user_id=profile.platform_user_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("SQLite 创建待确认绑定失败，回退 KV: %s", exc)
                await self._kv_create_pending(
                    user_key, platform, profile, group_id=group_id, token=token
                )
                return token
            await self._dual(
                self._kv_create_pending(
                    user_key, platform, profile, group_id=group_id, token=token
                )
            )
            return token
        await self._kv_create_pending(
            user_key, platform, profile, group_id=group_id, token=token
        )
        return token

    async def get_pending(
        self, user_id: str, platform: str
    ) -> Optional[Dict[str, Any]]:
        user_key = str(user_id)
        if self._need_store():
            try:
                item = await self.store.get_pending(user_key, platform)
            except Exception as exc:  # noqa: BLE001
                logger.warning("SQLite 读取待确认绑定失败，回退 KV: %s", exc)
                return await self._kv_get_pending(user_key, platform)
            if item is None:
                return None
            try:
                expires_at = float(item.get("expires_at") or 0)
            except (TypeError, ValueError):
                expires_at = 0.0
            if expires_at < time.time():
                try:
                    await self.store.clear_pending(user_key, platform)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("SQLite 清理过期待确认绑定失败: %s", exc)
                await self._dual(self._kv_clear_pending(user_key, platform))
                return None
            return dict(item)
        return await self._kv_get_pending(user_key, platform)

    async def clear_pending(self, user_id: str, platform: str) -> None:
        user_key = str(user_id)
        if self._need_store():
            try:
                await self.store.clear_pending(user_key, platform)
            except Exception as exc:  # noqa: BLE001
                logger.warning("SQLite 清理待确认绑定失败，回退 KV: %s", exc)
                await self._kv_clear_pending(user_key, platform)
                return
            await self._dual(self._kv_clear_pending(user_key, platform))
            return
        await self._kv_clear_pending(user_key, platform)

    @staticmethod
    def token_matches(value: str, expected_hash: str) -> bool:
        """校验验证码是否追加在公开资料字段中。"""
        return any(
            token_hash(candidate) == expected_hash
            for candidate in TOKEN_RE.findall(str(value or ""))
        )

    # ------------------------------------------------------------------
    # group membership
    # ------------------------------------------------------------------
    async def set_group_member(
        self,
        group_id: str,
        user_id: str,
        enabled: bool,
        *,
        preserve_opt_out: bool = False,
    ) -> bool:
        gid = str(group_id)
        uid = str(user_id)
        if self._need_store():
            try:
                changed = await self.store.set_group_member(
                    gid, uid, enabled, preserve_opt_out=preserve_opt_out
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("SQLite 更新群成员失败，回退 KV: %s", exc)
                return await self._kv_set_group_member(
                    gid, uid, enabled, preserve_opt_out=preserve_opt_out
                )
            if changed:
                await self._dual(
                    self._kv_set_group_member(
                        gid, uid, enabled, preserve_opt_out=preserve_opt_out
                    )
                )
            return changed
        return await self._kv_set_group_member(
            gid, uid, enabled, preserve_opt_out=preserve_opt_out
        )

    async def get_group_member_ids(self, group_id: str) -> List[str]:
        gid = str(group_id)
        if self._need_store():
            try:
                return await self.store.get_group_member_ids(gid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("SQLite 读取群成员失败，回退 KV: %s", exc)
        return await self._kv_get_group_member_ids(gid)

    # ------------------------------------------------------------------
    # 后台「强制加入排行」
    # ------------------------------------------------------------------
    async def add_rank_member(
        self, group_id: str, user_id: str, *, added_by: str = "", note: str = ""
    ) -> Dict[str, Any]:
        """把用户强制加入某群排行。

        与用户在群里自己「加入排行」的区别：名单单独落库（rank_member_overrides），
        读取成员时 UNION 进来，因此用户之后发「退出排行」或退群都不会掉，
        只有管理员在后台移除才会消失。
        """
        gid = str(group_id)
        uid = str(user_id)
        preexisting = False
        if self._need_store():
            try:
                preexisting = await self.store.is_group_member(gid, uid)
                await self.store.add_rank_member_override(gid, uid, added_by, note)
                await self._dual(self._kv_add_rank_member(gid, uid, added_by, note))
            except Exception as exc:  # noqa: BLE001
                logger.warning("写入强制加入名单失败，回退 KV：%s", exc)
                await self._kv_add_rank_member(gid, uid, added_by, note)
        else:
            await self._kv_add_rank_member(gid, uid, added_by, note)
        # 同步置为启用成员：让既有查询与 KV 模式立即生效
        await self._kv_set_group_member(gid, uid, True)
        if self._need_store():
            try:
                await self.store.set_group_member(gid, uid, True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("SQLite 强制加入群成员失败：%s", exc)
        return {"group_id": gid, "user_id": uid, "preexisting": preexisting}

    async def remove_rank_member(self, group_id: str, user_id: str) -> Dict[str, Any]:
        """移除后台强制加入；原本就在排行里的成员只去掉强制标记。"""
        gid = str(group_id)
        uid = str(user_id)
        override: Dict[str, Any] | None = None
        if self._need_store():
            try:
                override = await self.store.get_rank_member_override(gid, uid)
                await self.store.remove_rank_member_override(gid, uid)
                await self._dual(self._kv_remove_rank_member(gid, uid))
            except Exception as exc:  # noqa: BLE001
                logger.warning("移除强制加入名单失败，回退 KV：%s", exc)
                await self._kv_remove_rank_member(gid, uid)
        else:
            override = (await self._kv_get_rank_members(gid)).get(uid)
            await self._kv_remove_rank_member(gid, uid)
        preexisting = bool((override or {}).get("preexisting"))
        if not preexisting:
            await self._kv_set_group_member(gid, uid, False)
            if self._need_store():
                try:
                    await self.store.set_group_member(gid, uid, False)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("SQLite 移除群成员失败：%s", exc)
        return {"group_id": gid, "user_id": uid, "restored": preexisting}

    async def list_rank_members(self, group_id: str) -> List[Dict[str, Any]]:
        """列出某群排行成员及其来源（manual= 后台强制加入）。"""
        gid = str(group_id)
        overrides: Dict[str, Dict[str, Any]] = {}
        if self._need_store():
            try:
                for row in await self.store.list_rank_member_overrides(gid):
                    overrides[str(row.get("user_id") or "")] = row
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取强制加入名单失败：%s", exc)
        if not overrides:
            overrides = await self._kv_get_rank_members(gid)
        members = await self.get_group_member_ids(gid)
        out: List[Dict[str, Any]] = []
        for uid in members:
            override = overrides.get(str(uid)) or {}
            out.append(
                {
                    "user_id": str(uid),
                    "manual": bool(override),
                    "added_by": str(override.get("added_by") or ""),
                    "added_at": float(override.get("added_at") or 0),
                    "note": str(override.get("note") or ""),
                    "preexisting": bool(override.get("preexisting")),
                }
            )
        return out

    async def _kv_add_rank_member(
        self, group_id: str, user_id: str, added_by: str = "", note: str = ""
    ) -> None:
        data = await self._get(RANK_MEMBER_KEY, {})
        if not isinstance(data, dict):
            data = {}
        data[f"{group_id}|{user_id}"] = {
            "group_id": str(group_id),
            "user_id": str(user_id),
            "added_by": str(added_by or ""),
            "note": str(note or ""),
            "added_at": time.time(),
        }
        await self._put(RANK_MEMBER_KEY, data)

    async def _kv_remove_rank_member(self, group_id: str, user_id: str) -> None:
        data = await self._get(RANK_MEMBER_KEY, {})
        if not isinstance(data, dict):
            data = {}
        if data.pop(f"{group_id}|{user_id}", None) is not None:
            await self._put(RANK_MEMBER_KEY, data)

    async def _kv_get_rank_members(self, group_id: str) -> Dict[str, Dict[str, Any]]:
        data = await self._get(RANK_MEMBER_KEY, {})
        if not isinstance(data, dict):
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for item in data.values():
            if isinstance(item, dict) and str(item.get("group_id")) == str(group_id):
                out[str(item.get("user_id"))] = item
        return out

    async def remove_user_from_all_groups(self, user_id: str) -> None:
        uid = str(user_id)
        if self._need_store():
            try:
                await self.store.disable_user_in_all_groups(uid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("SQLite 退出全部群失败，回退 KV: %s", exc)
                await self._kv_remove_user_from_all_groups(uid)
                return
            await self._dual(self._kv_remove_user_from_all_groups(uid))
            return
        await self._kv_remove_user_from_all_groups(uid)

    # ------------------------------------------------------------------
    # rating snapshots
    # ------------------------------------------------------------------
    async def record_rating(
        self, user_id: str, platform: str, rating: Optional[int]
    ) -> None:
        await self.record_ratings([(user_id, platform, rating)])

    async def record_ratings(
        self,
        entries: List[Tuple[str, str, Optional[int]]],
    ) -> None:
        if self._need_store():
            try:
                await self.store.record_ratings(entries)
            except Exception as exc:  # noqa: BLE001
                logger.warning("SQLite 写入 Rating 快照失败，回退 KV: %s", exc)
                await self._kv_record_ratings(entries)
                return
            await self._dual(self._kv_record_ratings(entries))
            return
        await self._kv_record_ratings(entries)

    async def weekly_delta(
        self,
        user_id: str,
        platform: str,
        *,
        days: int = 7,
        now: Optional[float] = None,
    ) -> Optional[int]:
        return (
            await self.get_weekly_deltas(
                [(str(user_id), str(platform))],
                days=days,
                now=now,
            )
        ).get((str(user_id), str(platform)))

    async def get_weekly_deltas(
        self,
        requests: List[Tuple[str, str]],
        *,
        days: int = 7,
        now: Optional[float] = None,
    ) -> Dict[Tuple[str, str], Optional[int]]:
        if self._need_store():
            try:
                return await self.store.get_weekly_deltas(requests, days=days, now=now)
            except Exception as exc:  # noqa: BLE001
                logger.warning("SQLite 计算周变化失败，回退 KV: %s", exc)
        return await self._kv_get_weekly_deltas(requests, days=days, now=now)

    # ==================================================================
    # 旧 KV 实现（保持原语义；Store 双写与回退时复用）
    # ==================================================================
    async def _kv_get_user_accounts(self, user_id: str) -> Dict[str, Dict[str, Any]]:
        data = await self._get(ACCOUNTS_KEY, {})
        if not isinstance(data, dict):
            return {}
        value = data.get(str(user_id), {})
        return dict(value) if isinstance(value, dict) else {}

    async def _kv_get_all_accounts(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        data = await self._get(ACCOUNTS_KEY, {})
        if not isinstance(data, dict):
            return {}
        return {
            str(user_id): dict(accounts)
            for user_id, accounts in data.items()
            if isinstance(accounts, dict)
        }

    async def _kv_save_binding(
        self,
        user_id: str,
        platform: str,
        profile: AccountProfile,
        *,
        group_id: Optional[str] = None,
        qq_name: str = "",
    ) -> None:
        accounts = await self._kv_get_all_accounts()
        user_key = str(user_id)
        for other_user, user_accounts in accounts.items():
            if other_user == user_key:
                continue
            existing = user_accounts.get(platform)
            if not isinstance(existing, dict):
                continue
            existing_id = str(
                existing.get("platform_user_id") or existing.get("handle") or ""
            ).casefold()
            profile_id = str(
                profile.platform_user_id or profile.handle or ""
            ).casefold()
            if existing_id and existing_id == profile_id:
                raise ValueError("这个平台账号已经绑定到其他 QQ 用户")

        user_accounts = accounts.setdefault(user_key, {})
        user_accounts[platform] = {
            "platform": platform,
            "handle": profile.handle,
            "platform_user_id": profile.platform_user_id,
            "display_name": profile.display_name or profile.handle,
            "profile_url": profile.profile_url,
            "verified_at": time.time(),
            "qq_name": str(qq_name or "").strip(),
        }
        await self._put(ACCOUNTS_KEY, accounts)
        if group_id:
            await self._kv_set_group_member(str(group_id), user_key, True)
        await self._kv_clear_pending(user_key, platform)

    async def _kv_set_user_display_name(self, user_id: str, qq_name: str) -> bool:
        name = str(qq_name or "").strip()
        if not name:
            return False
        accounts = await self._kv_get_all_accounts()
        user_accounts = accounts.get(str(user_id))
        if not isinstance(user_accounts, dict):
            return False
        changed = False
        for item in user_accounts.values():
            if isinstance(item, dict) and item.get("qq_name") != name:
                item["qq_name"] = name
                changed = True
        if changed:
            await self._put(ACCOUNTS_KEY, accounts)
        return changed

    async def _kv_remove_binding(self, user_id: str, platform: str) -> bool:
        accounts = await self._kv_get_all_accounts()
        user_key = str(user_id)
        user_accounts = accounts.get(user_key)
        if not isinstance(user_accounts, dict) or platform not in user_accounts:
            return False
        user_accounts.pop(platform, None)
        if user_accounts:
            accounts[user_key] = user_accounts
        else:
            accounts.pop(user_key, None)
        await self._put(ACCOUNTS_KEY, accounts)
        await self._kv_clear_pending(user_id, platform)
        return True

    async def _kv_create_pending(
        self,
        user_id: str,
        platform: str,
        profile: AccountProfile,
        *,
        group_id: Optional[str] = None,
        token: str,
    ) -> str:
        pending = await self._get(PENDING_BINDINGS_KEY, {})
        if not isinstance(pending, dict):
            pending = {}
        pending[f"{user_id}:{platform}"] = {
            "platform": platform,
            "handle": profile.handle,
            "platform_user_id": profile.platform_user_id,
            "token_hash": token_hash(token),
            "created_at": time.time(),
            "expires_at": time.time() + BINDING_TTL,
            "group_id": str(group_id) if group_id else "",
        }
        await self._put(PENDING_BINDINGS_KEY, pending)
        return token

    async def _kv_get_pending(
        self, user_id: str, platform: str
    ) -> Optional[Dict[str, Any]]:
        pending = await self._get(PENDING_BINDINGS_KEY, {})
        if not isinstance(pending, dict):
            return None
        item = pending.get(f"{user_id}:{platform}")
        if not isinstance(item, dict):
            return None
        try:
            expires_at = float(item.get("expires_at", 0) or 0)
        except (TypeError, ValueError):
            await self._kv_clear_pending(user_id, platform)
            return None
        if expires_at < time.time():
            await self._kv_clear_pending(user_id, platform)
            return None
        return dict(item)

    async def _kv_clear_pending(self, user_id: str, platform: str) -> None:
        pending = await self._get(PENDING_BINDINGS_KEY, {})
        if not isinstance(pending, dict):
            return
        pending.pop(f"{user_id}:{platform}", None)
        await self._put(PENDING_BINDINGS_KEY, pending)

    async def _kv_set_group_member(
        self,
        group_id: str,
        user_id: str,
        enabled: bool,
        *,
        preserve_opt_out: bool = False,
    ) -> bool:
        data = await self._get(GROUP_RANK_KEY, {})
        if not isinstance(data, dict):
            data = {}
        group = data.setdefault(str(group_id), {})
        if not isinstance(group, dict):
            group = {}
            data[str(group_id)] = group
        key = str(user_id)
        existing = group.get(key)
        if (
            preserve_opt_out
            and isinstance(existing, dict)
            and not bool(existing.get("enabled", False))
        ):
            return False
        enabled_value = bool(enabled)
        if (
            isinstance(existing, dict)
            and bool(existing.get("enabled", False)) == enabled_value
        ):
            return False
        group[key] = {
            "enabled": enabled_value,
            "updated_at": time.time(),
        }
        await self._put(GROUP_RANK_KEY, data)
        return True

    async def _kv_get_group_member_ids(self, group_id: str) -> List[str]:
        data = await self._get(GROUP_RANK_KEY, {})
        if not isinstance(data, dict):
            return []
        group = data.get(str(group_id), {})
        if not isinstance(group, dict):
            return []
        return [
            str(user_id)
            for user_id, item in group.items()
            if isinstance(item, dict) and bool(item.get("enabled", False))
        ]

    async def _kv_record_ratings(
        self,
        entries: List[Tuple[str, str, Optional[int]]],
    ) -> None:
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
        data = await self._get(RATING_SNAPSHOTS_KEY, {})
        if not isinstance(data, dict):
            data = {}
        now = time.time()
        changed = False
        for (user_id, platform), value in normalized.items():
            user = data.setdefault(user_id, {})
            if not isinstance(user, dict):
                user = {}
                data[user_id] = user
            history = user.setdefault(platform, [])
            if not isinstance(history, list):
                history = []
                user[platform] = history
            if history and isinstance(history[-1], dict):
                last_value = history[-1].get("rating")
                try:
                    last_time = float(history[-1].get("timestamp", 0) or 0)
                except (TypeError, ValueError):
                    last_time = 0.0
                if last_value == value and now - last_time < 15 * 60:
                    continue
            history.append({"timestamp": now, "rating": value})
            user[platform] = history[-90:]
            changed = True
        if changed:
            await self._put(RATING_SNAPSHOTS_KEY, data)

    async def _kv_get_weekly_deltas(
        self,
        requests: List[Tuple[str, str]],
        *,
        days: int = 7,
        now: Optional[float] = None,
    ) -> Dict[Tuple[str, str], Optional[int]]:
        data = await self._get(RATING_SNAPSHOTS_KEY, {})
        if not isinstance(data, dict):
            return {
                (str(user_id), str(platform)): None
                for user_id, platform in requests
            }
        current_time = time.time() if now is None else now
        cutoff = current_time - max(1, int(days)) * 86400
        result: Dict[Tuple[str, str], Optional[int]] = {}
        for user_id, platform in requests:
            key = (str(user_id), str(platform))
            if key in result:
                continue
            user = data.get(key[0], {})
            history = user.get(key[1], []) if isinstance(user, dict) else []
            if not isinstance(history, list) or not history:
                result[key] = None
                continue
            entries = []
            for item in history:
                if not isinstance(item, dict):
                    continue
                try:
                    timestamp = float(item.get("timestamp", 0) or 0)
                    rating = int(item.get("rating"))
                except (TypeError, ValueError):
                    continue
                entries.append((timestamp, rating))
            if not entries:
                result[key] = None
                continue
            entries.sort(key=lambda item: item[0])
            current_rating = entries[-1][1]
            baseline = next(
                (rating for timestamp, rating in reversed(entries)
                 if timestamp <= cutoff),
                None,
            )
            result[key] = (
                current_rating - baseline if baseline is not None else None
            )
        return result

    async def _kv_remove_user_from_all_groups(self, user_id: str) -> None:
        data = await self._get(GROUP_RANK_KEY, {})
        if not isinstance(data, dict):
            return
        key = str(user_id)
        changed = False
        for group in data.values():
            if isinstance(group, dict) and isinstance(group.get(key), dict):
                group[key]["enabled"] = False
                group[key]["updated_at"] = time.time()
                changed = True
        if changed:
            await self._put(GROUP_RANK_KEY, data)
