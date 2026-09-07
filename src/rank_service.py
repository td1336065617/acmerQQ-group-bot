"""群排行物化读模型与后台刷新（RankService）。

读路径：
- SQLite 已启用且快照新鲜（默认 60 分钟）→ 直接读 rank_snapshot，O(1)；
- 快照过期但有数据 → 先返回旧快照（stale-while-revalidate）并投递后台刷新；
- 无快照 / force → 复用现有 _collect_rank_rows 同步计算并落库。

写路径（后台）：复用 _collect_rank_rows_uncached（含 CF 批量、账号缓存、
负缓存与 Rating 快照写入），结果整体替换 rank_snapshot 并 touch rank_meta。
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
        self, store, group_id: str, platform: str
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        try:
            meta = await store.get_rank_meta(group_id, platform)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 rank_meta 失败: %s", exc)
            return False, None
        if meta is None:
            return False, None
        refreshed = float(meta.get("refreshed_at") or 0)
        dirty_at = float(meta.get("dirty_at") or 0)
        stale = (
            refreshed <= 0
            or time.time() - refreshed > RANK_SNAPSHOT_MAX_AGE
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
        """读取群排行；优先命中 SQLite 快照，miss/stale 时才同步计算。"""
        gid = str(group_id)
        store = self._store()
        dirty_pending = getattr(self.plugin, "_rank_dirty_pending", None)
        dirty_now = bool(dirty_pending and gid in dirty_pending)
        if force:
            # force 必须绕过 5 分钟内存缓存与 SQLite 快照，直接全量计算。
            started = time.time()
            rows, errors, _ = await self.plugin._collect_rank_rows_uncached(
                gid,
                platform,
                progress=progress,
                record_metrics=record_metrics,
            )
            if store is not None and not progress:
                try:
                    await self._persist(
                        store, gid, platform, rows, errors, started
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("排行快照落库失败: %s", exc)
            if dirty_pending is not None:
                dirty_pending.discard(gid)
            return rows, errors
        # progress（本周进步榜）行没有快照维度（rank_snapshot 无 progress 列），
        # 且该指令低频、只取 Top5 → 始终走原同步计算路径。
        if not progress and store is not None and not dirty_now:
            try:
                fresh, meta = await self._meta_fresh(store, gid, platform)
                snapshot_rows = await store.get_rank_rows(gid, platform)
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
        if store is not None and not progress:
            try:
                await self._persist(store, gid, platform, rows, errors, started)
            except Exception as exc:  # noqa: BLE001 - 落库失败不影响本次回复
                logger.warning("排行快照落库失败: %s", exc)
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
        store = self._store()
        started = time.time()
        try:
            async with self._refresh_semaphore:
                rows, errors, _ = await self.plugin._collect_rank_rows_uncached(
                    group_id,
                    platform,
                    progress=progress,
                    record_metrics=True,
                )
                if store is not None:
                    await self._persist(
                        store, group_id, platform, rows, errors, started
                    )
            if errors:
                logger.warning(
                    "群 %s %s 排行后台刷新有 %d 个账号失败",
                    group_id,
                    platform,
                    len(errors),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 后台刷新失败不影响主流程
            logger.warning("群 %s %s 排行后台刷新失败: %s", group_id, platform, exc)
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
    ) -> None:
        """整体替换快照；有错误时标脏（短周期重试），无错误时只清旧脏。"""
        await store.replace_rank_snapshot(group_id, platform, rows)
        if errors:
            await store.mark_rank_dirty_with_errors(
                group_id, platform, errors
            )
        else:
            await store.touch_rank_meta_preserving_dirty(
                group_id, platform, errors, refresh_started_at
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

    async def refresh_stale(
        self, *, max_age: float = RANK_SNAPSHOT_MAX_AGE
    ) -> int:
        """供 scheduler 低频调用：扫描过期/脏的 (group, platform) 并后台刷新。"""
        store = self._store()
        if store is None:
            return 0
        try:
            stale = await store.list_stale_rank_meta(
                max_age=max_age,
                active_platforms=list(DEFAULT_PLATFORMS),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("扫描过期排行快照失败: %s", exc)
            return 0
        for item in stale:
            await self.request_refresh(
                str(item["group_id"]),
                str(item["platform"]),
                progress=False,
            )
        return len(stale)
