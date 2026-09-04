"""定时推送：每日早报 + 赛前 15 分钟提醒。"""
from __future__ import annotations

import asyncio
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain

from .models import CN_TZ, PLATFORM_LABELS, GroupConfig
from .utils import validate_hhmm

TICK_SECONDS = 30
REMIND_MINUTES = 15


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
        for group in await self.plugin.get_groups():
            if not group.enabled:
                continue
            try:
                await self._maybe_morning_push(group, now)
                if group.reminder_enabled:
                    await self._maybe_remind(group, now)
            except Exception:
                logger.exception("群 %s 定时推送处理失败", group.group_id)

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
