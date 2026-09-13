"""定时推送：每日早报 + 赛前 15 分钟提醒。"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Dict, Set, Tuple

from astrbot.api import logger

from .models import CN_TZ, PLATFORM_LABELS, GroupConfig
from .utils import validate_hhmm

TICK_SECONDS = 30
REMIND_MINUTES = 15
# 比赛数据后台预热窗口：TTL 到期前多少秒开始提前刷新（在线平台 TTL 5 分钟）。
CONTEST_WARM_WINDOW_SECONDS = 90
# 提醒去重表：超过该长度时只保留最近 1000 条（与旧实现一致）。
REMINDED_SOFT_LIMIT = 2000
REMINDED_KEEP = 1000


class PushScheduler:
    """后台定时任务：每 30 秒检查一次早报与提醒。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self._task: asyncio.Task | None = None
        self._prune_counter = 0

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="acmer-push")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("acmerQQ群机器人 定时推送任务异常")
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self) -> None:
        now = datetime.now(CN_TZ)
        # reminded 去重表每个 tick 只读一次，各群共享；仅在真正新增提醒时写回。
        reminded: Set[str] = set(
            await self.plugin.get_kv_data("reminded", []) or []
        )
        # 每个平台每个 tick 只抓一次，多个群复用同一份结果。
        platform_cache: Dict[str, Tuple[list, object]] = {}
        for group in await self.plugin.get_groups():
            if not group.enabled:
                continue
            try:
                await self._maybe_morning_push(group, now)
                if group.reminder_enabled:
                    await self._maybe_remind(group, now, reminded, platform_cache)
            except Exception:
                logger.exception("群 %s 定时推送处理失败", group.group_id)
        # 比赛数据后台预热：在 TTL 到期前 90 秒提前刷新一个平台，
        # 使用户请求不再撞上“缓存过期后同步抓取”的卡顿。
        try:
            warmer = getattr(self.plugin.fetcher, "warm", None)
            if callable(warmer):
                await warmer(
                    window=CONTEST_WARM_WINDOW_SECONDS,
                    max_platforms=1,
                )
        except Exception:  # noqa: BLE001 - 预热失败不影响推送
            logger.warning("比赛数据后台预热失败", exc_info=True)
        # 约每 5 分钟（10 个 tick）淘汰一次账号抓取器的进程内缓存，
        # 并把标脏的资料/负缓存批量落库（重启不冷）。
        self._prune_counter += 1
        if self._prune_counter >= 10:
            self._prune_counter = 0
            try:
                prune = getattr(
                    self.plugin.account_fetcher, "prune_cache", None
                )
                if callable(prune):
                    prune()
                flush = getattr(
                    self.plugin.account_fetcher,
                    "flush_persistent_cache",
                    None,
                )
                if callable(flush):
                    await flush()
                registry = getattr(self.plugin, "account_registry", None)
                if registry is not None and getattr(
                    registry, "store_enabled", False
                ):
                    store = getattr(registry, "store", None)
                    for cleaner in (
                        "purge_expired_pending",
                        "delete_expired_profile_cache",
                        "delete_expired_fetch_failures",
                    ):
                        method = getattr(store, cleaner, None)
                        if callable(method):
                            await method()
                refresh_stale = getattr(
                    self.plugin, "rank_service", None
                )
                if refresh_stale is not None:
                    stale_refresher = getattr(
                        refresh_stale, "refresh_stale", None
                    )
                    if callable(stale_refresher):
                        await stale_refresher()
                # 渲染图片缓存按容量上限（默认 500MB）LRU 清理，
                # 避免 output_cache/account_cards 无限增长。
                for holder in (
                    getattr(self.plugin, "output_renderer", None),
                    getattr(self.plugin, "account_card_renderer", None),
                ):
                    trimmer = getattr(holder, "prune", None)
                    if callable(trimmer):
                        try:
                            await asyncio.to_thread(trimmer)
                        except Exception:  # noqa: BLE001
                            logger.warning("渲染缓存清理失败", exc_info=True)
            except Exception:  # noqa: BLE001 - 缓存清理失败不影响推送
                logger.warning("账号抓取器缓存清理失败", exc_info=True)

    async def _maybe_morning_push(self, group: GroupConfig, now: datetime) -> None:
        try:
            push_time = validate_hhmm(group.morning_push_time)
        except ValueError:
            return
        if now.strftime("%H:%M") != push_time:
            return
        logger.info("群 %s 早报时间到（%s），开始检查今日比赛", group.group_id, push_time)
        date_key = now.strftime("%Y%m%d")
        sent_key = f"morning_{group.group_id}_{date_key}"
        if await self.plugin.get_kv_data(sent_key, False):
            logger.info("群 %s 今日早报已发送过，跳过", group.group_id)
            return
        text = await self.plugin.build_morning_text(group)
        # 周榜（本周进步榜 / 本周退步榜）算早报的一部分：即使今日没有比赛，
        # 只要周榜有数据就照常推送。
        boards = ""
        board_builder = getattr(
            self.plugin, "build_weekly_boards_text", None
        )
        if callable(board_builder):
            try:
                boards = await board_builder(group) or ""
            except Exception:  # noqa: BLE001 - 周榜失败不影响比赛早报
                logger.warning(
                    "群 %s 周榜构建失败，仅推送比赛早报",
                    group.group_id,
                    exc_info=True,
                )
                boards = ""
        parts = [item for item in (text, boards) if item]
        if not parts:
            logger.info(
                "群 %s 今日无比赛且暂无周榜数据，跳过早报", group.group_id
            )
            await self.plugin.put_kv_data(sent_key, True)
            logger.info("群 %s 早报处理完成", group.group_id)
            return
        if not text:
            logger.info("群 %s 今日无比赛，仅推送周榜", group.group_id)
        combined = "\n\n".join(parts)
        sent = await self.plugin.send_notification(group, combined)
        if sent:
            await self.plugin.put_kv_data(sent_key, True)
            logger.info("群 %s 早报处理完成", group.group_id)
        else:
            logger.warning("群 %s 早报发送失败，下个周期重试", group.group_id)

    async def _maybe_remind(
        self,
        group: GroupConfig,
        now: datetime,
        reminded: Set[str],
        platform_cache: Dict[str, Tuple[list, object]],
    ) -> None:
        for platform in group.push_platforms:
            if platform not in platform_cache:
                platform_cache[platform] = await self.plugin.fetcher.fetch_platform(
                    platform
                )
            contests, err = platform_cache[platform]
            if err or not contests:
                continue
            for contest in contests:
                delta = (contest.start_time - now).total_seconds()
                if not 0 < delta <= REMIND_MINUTES * 60:
                    continue
                # 去重键必须带群 ID：同一场比赛每个群都应收到一次提醒，
                # 避免第一个群推送后其他群全部被跳过
                dedupe_key = (
                    f"{group.group_id}:{contest.platform}:{contest.contest_id}"
                )
                if dedupe_key in reminded:
                    continue
                text = (
                    f"⏰ {PLATFORM_LABELS.get(contest.platform, contest.platform)} "
                    "比赛即将开始\n"
                    f"🏷 {contest.name}\n"
                    f"🕐 {contest.start_cn():%Y-%m-%d %H:%M}（北京时间）\n"
                    f"🔗 {contest.url}"
                )
                if await self.plugin.send_notification(group, text):
                    reminded.add(dedupe_key)
                    reminded_list = sorted(reminded)
                    if len(reminded_list) > REMINDED_SOFT_LIMIT:
                        reminded_list = reminded_list[-REMINDED_KEEP:]
                    try:
                        await self.plugin.put_kv_data("reminded", reminded_list)
                    except Exception:  # noqa: BLE001 - 写失败回滚内存，下周期重试
                        reminded.discard(dedupe_key)
                        logger.warning(
                            "赛前提醒去重表写回失败，下个周期重试",
                            exc_info=True,
                        )
