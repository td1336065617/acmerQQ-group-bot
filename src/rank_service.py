"""群排行物化读模型与后台刷新（RankService）。

读路径（rank 与 progress 共用同一套快照机制）：
- SQLite 已启用且快照新鲜 → 直接读 rank_snapshot / progress_snapshot，O(1)；
- 快照过期但有数据 → 先返回旧快照（stale-while-revalidate）并投递后台刷新；
- 无快照 / force → 复用现有 _collect_rank_rows 同步计算并落库。

写路径（后台）：复用 _collect_rank_rows_uncached（含 CF 批量、账号缓存、
负缓存与 Rating 快照写入），结果整体替换对应快照并 touch 对应 meta。

progress（本周进步榜）是 7 天聚合指标，变化比即时 Rating 更慢，
因此使用更长的刷新窗口，避免每次请求都全群重算。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger

from .models import DEFAULT_PLATFORMS

# 排行快照新鲜窗口：Rating 一天变化约 1~2 次，普通查询接受 60 分钟内数据。
RANK_SNAPSHOT_MAX_AGE = 60 * 60
# 本周进步榜是 7 天聚合，对新鲜度更不敏感；放宽到 2 小时以显著减少全群重算。
PROGRESS_SNAPSHOT_MAX_AGE = 2 * 60 * 60


def _mode_of(progress: bool) -> str:
    return "progress" if progress else "rank"


def _max_age_of(mode: str) -> float:
    return (
        PROGRESS_SNAPSHOT_MAX_AGE
        if mode == "progress"
        else RANK_SNAPSHOT_MAX_AGE
    )


class RankService:
    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self._jobs: Dict[tuple, asyncio.Task] = {}
        self._refresh_semaphore = asyncio.Semaphore(2)
        self._closing = False

    def _store(self):
        registry = getattr(self.plugin, "account_registry", None)
        if registry is None:
            return None
        if not getattr(registry, "store_enabled", False):
            return None
        return getattr(registry, "store", None)

    async def _meta_fresh(
        self, store, group_id: str, platform: str, mode: str = "rank"
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        try:
            meta = await store.get_rank_meta(group_id, platform, mode=mode)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 rank_meta 失败: %s", exc)
            return False, None
        if meta is None:
            return False, None
        refreshed = float(meta.get("refreshed_at") or 0)
        dirty_at = float(meta.get("dirty_at") or 0)
        stale = (
            refreshed <= 0
            or time.time() - refreshed > _max_age_of(mode)
            or dirty_at > refreshed
        )
        return not stale, meta

    async def read(
        self,
        group_id: str,
        platform: str,
        *,
        progress: bool = False,
        record_metrics: bool = True,
        allow_stale: bool = True,
        force: bool = False,
    ) -> Tuple[List[Dict[str, Any]], List[Any]]:
        """读取群排行/进步榜；优先命中 SQLite 快照，miss/stale 时才同步计算。"""
        gid = str(group_id)
        mode = _mode_of(progress)
        store = self._store()
        dirty_pending = getattr(self.plugin, "_rank_dirty_pending", None)
        dirty_now = bool(dirty_pending and gid in dirty_pending)
        if force:
            # force 必须绕过内存缓存与 SQLite 快照，直接全量计算。
            started = time.time()
            rows, errors, _ = await self.plugin._collect_rank_rows_uncached(
                gid,
                platform,
                progress=progress,
                record_metrics=record_metrics,
            )
            if store is not None:
                try:
                    await self._persist(
                        store, gid, platform, rows, errors, started, mode=mode
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("排行快照落库失败: %s", exc)
            if dirty_pending is not None:
                dirty_pending.discard(gid)
            return rows, errors

        if store is not None and not dirty_now:
            try:
                fresh, meta = await self._meta_fresh(store, gid, platform, mode)
                snapshot_rows = await store.get_rank_rows(
                    gid, platform, mode=mode
                )
                if fresh:
                    return snapshot_rows, self._meta_errors(meta)
                if snapshot_rows and allow_stale:
                    # 先回旧快照，后台刷新；用户无需等全群抓取。
                    await self.request_refresh(gid, platform, progress=progress)
                    return snapshot_rows, self._meta_errors(meta)
            except Exception as exc:  # noqa: BLE001 - 快照失败回退同步计算
                logger.warning("读取排行快照失败，回退同步计算: %s", exc)

        started = time.time()
        rows, errors = await self.plugin._collect_rank_rows(
            gid,
            platform,
            progress=progress,
            record_metrics=record_metrics,
        )
        if store is not None:
            try:
                await self._persist(
                    store, gid, platform, rows, errors, started, mode=mode
                )
            except Exception as exc:  # noqa: BLE001 - 落库失败不影响本次回复
                logger.warning("排行快照落库失败: %s", exc)
        if progress and store is not None:
            # 前台首次计算为了快速返回只补了“限量”成员的差值，
            # 立即投递一次后台完整刷新，避免不完整快照被冻结整个窗口。
            await self.request_refresh(gid, platform, progress=True)
        if dirty_pending is not None:
            dirty_pending.discard(gid)
        return rows, errors

    async def request_refresh(
        self,
        group_id: str,
        platform: str,
        *,
        progress: bool = False,
    ) -> None:
        """投递一次后台刷新；同 (group, platform) 只保留一个任务。"""
        key = (str(group_id), str(platform), bool(progress))
        task = self._jobs.get(key)
        if task is not None and not task.done():
            return
        task = asyncio.create_task(
            self._refresh(key), name=f"acmer-rank-{key[0]}-{key[1]}"
        )
        self._jobs[key] = task

    async def _refresh(self, key: tuple) -> None:
        if self._closing:
            return
        group_id, platform, progress = key
        mode = _mode_of(bool(progress))
        store = self._store()
        started = time.time()
        try:
            async with self._refresh_semaphore:
                rows, errors, _ = await self.plugin._collect_rank_rows_uncached(
                    group_id,
                    platform,
                    progress=progress,
                    record_metrics=True,
                    # 后台刷新没有人等待：允许补齐全部缺失成员的差值，
                    # 这样前台首次查询的“限量快照”会被完整数据替换。
                    full_detail=True,
                )
                if store is not None:
                    await self._persist(
                        store, group_id, platform, rows, errors, started, mode=mode
                    )
            if errors:
                logger.warning(
                    "群 %s %s %s 后台刷新有 %d 个账号失败",
                    group_id,
                    platform,
                    mode,
                    len(errors),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 后台刷新失败不影响主流程
            logger.warning(
                "群 %s %s %s 后台刷新失败: %s", group_id, platform, mode, exc
            )
        finally:
            self._jobs.pop(key, None)

    @staticmethod
    async def _persist(
        store,
        group_id: str,
        platform: str,
        rows: list,
        errors: list,
        refresh_started_at: float,
        *,
        mode: str = "rank",
    ) -> None:
        """整体替换快照；有错误时标脏（短周期重试），无错误时只清旧脏。"""
        await store.replace_rank_snapshot(group_id, platform, rows, mode=mode)
        if errors:
            await store.mark_rank_dirty_with_errors(
                group_id, platform, errors, mode=mode
            )
        else:
            await store.touch_rank_meta_preserving_dirty(
                group_id, platform, errors, refresh_started_at, mode=mode
            )

    @staticmethod
    def _meta_errors(meta: Optional[dict]) -> list:
        """把存储的错误摘要还原为 [(user_id, error_message), ...] 二元组。"""
        if not isinstance(meta, dict):
            return []
        try:
            value = json.loads(str(meta.get("errors_json") or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(value, list):
            return []
        result = []
        for entry in value:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                result.append((str(entry[0]), str(entry[1])))
            else:
                result.append(("", str(entry)))
        return result

    async def close(self) -> None:
        """插件退出前取消并等待所有后台刷新任务。"""
        self._closing = True
        jobs = list(self._jobs.values())
        for task in jobs:
            task.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        self._jobs.clear()

    async def refresh_stale(self, *, max_age: Optional[float] = None) -> int:
        """供 scheduler 低频调用：扫描过期/脏的 (group, platform) 并后台刷新。

        rank 与 progress 两种快照分别按各自窗口判断。progress 只对“曾被查询过”
        （即已存在 meta 行）的群生效，因此不会为全量群做昂贵的进步榜重算。
        """
        store = self._store()
        if store is None:
            return 0
        planned: List[Tuple[str, str, bool]] = []
        for mode, progress in (("rank", False), ("progress", True)):
            limit = _max_age_of(mode) if max_age is None else max_age
            try:
                stale = await store.list_stale_rank_meta(
                    max_age=limit,
                    active_platforms=list(DEFAULT_PLATFORMS),
                    mode=mode,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("扫描过期 %s 快照失败: %s", mode, exc)
                continue
            planned.extend(
                (str(item["group_id"]), str(item["platform"]), progress)
                for item in stale
            )
        for group_id, platform, progress in planned:
            await self.request_refresh(group_id, platform, progress=progress)
        return len(planned)
