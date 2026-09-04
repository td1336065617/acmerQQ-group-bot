"""定时推送：每日早报 + 赛前 15 分钟提醒 + 新公告转发。"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import List

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain

from src.models import (
    ANNOUNCEMENT_LABELS,
    CN_TZ,
    DEFAULT_ANNOUNCEMENT_SOURCES,
    PLATFORM_LABELS,
    GroupConfig,
)
from src.utils import validate_hhmm

TICK_SECONDS = 30
REMIND_MINUTES = 15
# 公告轮询间隔：公告更新频率低，10 分钟一次足够且不打扰源站
ANNOUNCEMENT_CHECK_SECONDS = 600
# 已转发公告去重记录的上限（超出后保留最近的一半）
ANNOUNCEMENT_SENT_CAP = 2000
ANNOUNCEMENT_SENT_KEEP = 1000


class PushScheduler:
    """后台定时任务：每 30 秒检查一次早报与提醒。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self._task: asyncio.Task | None = None

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
        groups = await self.plugin.get_groups()
        for group in groups:
            if not group.enabled:
                continue
            try:
                await self._maybe_morning_push(group, now)
                if group.reminder_enabled:
                    await self._maybe_remind(group, now)
            except Exception:
                logger.exception("群 %s 定时推送处理失败", group.group_id)
        try:
            await self._maybe_announcements(groups)
        except Exception:
            logger.exception("公告转发任务失败")

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
        if not text:
            logger.info("群 %s 今日无比赛，跳过早报", group.group_id)
            await self.plugin.put_kv_data(sent_key, True)
            logger.info("群 %s 早报处理完成", group.group_id)
            return
        sent = await self.plugin.send_notification(group, text)
        if sent:
            await self.plugin.put_kv_data(sent_key, True)
            logger.info("群 %s 早报处理完成", group.group_id)
        else:
            logger.warning("群 %s 早报发送失败，下个周期重试", group.group_id)

    async def _maybe_remind(self, group: GroupConfig, now: datetime) -> None:
        reminded = set(await self.plugin.get_kv_data("reminded", []) or [])
        for platform in group.push_platforms:
            contests, err = await self.plugin.fetcher.fetch_platform(platform)
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
                    if len(reminded_list) > 2000:
                        reminded_list = reminded_list[-1000:]
                    await self.plugin.put_kv_data("reminded", reminded_list)

    # ------------------------------------------------------------------
    # 公告转发
    # ------------------------------------------------------------------
    async def _maybe_announcements(self, groups: List[GroupConfig]) -> None:
        """按间隔轮询公告源，把新公告转发到已启用的群。"""
        settings = await self.plugin.get_settings()
        if not settings.get("announcement_enabled", True):
            return
        targets = [g for g in groups if g.enabled and g.announcement_enabled]
        if not targets:
            return
        last_check = await self.plugin.get_kv_data("announcement_last_check", 0.0)
        try:
            last_check = float(last_check or 0.0)
        except (TypeError, ValueError):
            last_check = 0.0
        if time.time() - last_check < ANNOUNCEMENT_CHECK_SECONDS:
            return
        # 先记录本次检查时间：抓取失败时也等下一个间隔再重试，避免频繁请求源站
        await self.plugin.put_kv_data("announcement_last_check", time.time())
        for source in DEFAULT_ANNOUNCEMENT_SOURCES:
            await self._push_source_announcements(source, targets)

    async def _push_source_announcements(
        self, source: str, targets: List[GroupConfig]
    ) -> None:
        label = ANNOUNCEMENT_LABELS.get(source, source)
        announcements, err = await self.plugin.announcement_fetcher.fetch_source(
            source
        )
        if err:
            logger.warning("公告抓取失败(%s)：%s", source, err)
            return
        if not announcements:
            return

        sent: List[str] = list(
            await self.plugin.get_kv_data("announcement_sent", []) or []
        )
        sent_keys = set(sent)
        baselined = set(
            await self.plugin.get_kv_data("announcement_baselined", []) or []
        )
        changed = False

        def mark(group_id: str, announcement_id: str) -> None:
            nonlocal changed
            key = f"{group_id}:{source}:{announcement_id}"
            if key not in sent_keys:
                sent_keys.add(key)
                sent.append(key)
                changed = True

        # 首次运行 / 新注册的群：把当前页面上的历史公告全部标记为已知，
        # 只有之后新增的公告才会被转发，避免一次性刷屏 20 条
        for group in targets:
            baseline_key = f"{group.group_id}:{source}"
            if baseline_key in baselined:
                continue
            for item in announcements:
                mark(group.group_id, item.announcement_id)
            baselined.add(baseline_key)
            changed = True
            logger.info(
                "群 %s 的 %s 公告基线已建立（%d 条历史公告不再补发）",
                group.group_id,
                label,
                len(announcements),
            )

        # 页面按新→旧排列，转发时反向遍历让旧公告先发
        for item in reversed(announcements):
            for group in targets:
                key = f"{group.group_id}:{source}:{item.announcement_id}"
                if key in sent_keys:
                    continue
                text = item.format_detail(f"📢 {label} 新公告")
                if await self.plugin.send_notification(group, text):
                    mark(group.group_id, item.announcement_id)
                    logger.info(
                        "已向群 %s 转发 %s 公告：%s",
                        group.group_id,
                        label,
                        item.title,
                    )
                else:
                    logger.warning(
                        "群 %s 公告转发失败，下个周期重试：%s",
                        group.group_id,
                        item.title,
                    )

        if changed:
            if len(sent) > ANNOUNCEMENT_SENT_CAP:
                sent = sent[-ANNOUNCEMENT_SENT_KEEP:]
            await self.plugin.put_kv_data("announcement_sent", sent)
            await self.plugin.put_kv_data("announcement_baselined", sorted(baselined))
