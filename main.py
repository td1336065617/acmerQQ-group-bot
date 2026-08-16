"""acmerQQ群机器人：ACM 竞赛信息查询与定时推送插件。

功能菜单：发送 “acmer群管理插件菜单” 查看全部指令与所需权限。
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import List, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.platform import MessageType
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.platform.message_session import MessageSesion

from src.contest_fetcher import ContestFetcher
from src.models import CN_TZ, DEFAULT_PLATFORMS, PLATFORM_LABELS, GroupConfig
from src.scheduler import PushScheduler
from src.utils import validate_hhmm

PLUGIN_NAME = "acmer_qq_group_bot"
DEFAULT_MORNING_TIME = "08:00"
# @全体成员 尝试失败后，对该群暂缓重试的时间（秒）
AT_ALL_BLOCK_SECONDS = 6 * 3600

MENU_TEXT = (
    "acmer群管理插件菜单\n"
    "【所有人可用】\n"
    "/nc（牛客）- 牛客最近比赛\n"
    "/cf（codeforces）- Codeforces 最近比赛\n"
    "/atc（atcoder）- AtCoder 最近比赛\n"
    "/lg（洛谷）- 洛谷最近比赛\n"
    "acmer群管理插件菜单 - 显示本菜单\n"
    "【仅管理员】\n"
    "/update（刷新比赛）- 手动刷新全部比赛数据\n"
    "推送配置（早报/提醒/@全体） - 请在 AstrBot WebUI "
    "插件页面的“acmerQQ群机器人”页操作"
)


class AcmerGroupBot(Star):
    def __init__(self, context: Context, config: Optional[dict] = None) -> None:
        super().__init__(context, config)
        self.config = config if isinstance(config, dict) else {}
        self.fetcher = ContestFetcher()
        self.scheduler = PushScheduler(self)
        try:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/config",
                self._web_config_get,
                ["GET"],
                "获取 acmerQQ群机器人 配置",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/config",
                self._web_config_set,
                ["POST"],
                "保存 acmerQQ群机器人 配置",
            )
        except Exception as exc:
            logger.error("注册 Web API 失败: %s", exc)

    async def initialize(self) -> None:
        await self.fetcher.initialize()
        await self.scheduler.start()
        logger.info("acmerQQ群机器人 已启动")

    async def terminate(self) -> None:
        await self.scheduler.stop()
        await self.fetcher.close()
        logger.info("acmerQQ群机器人 已停止")

    # ------------------------------------------------------------------
    # 配置存取（AstrBot 插件 KV 存储，数据落在 AstrBot 数据库）
    # ------------------------------------------------------------------
    async def _get_admins(self) -> List[str]:
        admins = await self.get_kv_data("admin_users", []) or []
        return [str(a).strip() for a in admins if str(a).strip()]

    async def _is_admin(self, event: AstrMessageEvent) -> bool:
        return event.get_sender_id() in await self._get_admins()

    async def get_settings(self) -> dict:
        raw = await self.get_kv_data("settings", {}) or {}
        try:
            morning = validate_hhmm(
                raw.get("morning_push_time", DEFAULT_MORNING_TIME)
            )
        except ValueError:
            morning = DEFAULT_MORNING_TIME
        platforms = raw.get("push_platforms") or list(DEFAULT_PLATFORMS)
        platforms = [p for p in DEFAULT_PLATFORMS if p in platforms]
        return {
            "morning_push_time": morning,
            "push_platforms": platforms or list(DEFAULT_PLATFORMS),
            "reminder_enabled": bool(raw.get("reminder_enabled", True)),
            "at_all_enabled": bool(raw.get("at_all_enabled", False)),
        }

    async def get_groups(self) -> List[GroupConfig]:
        raw = await self.get_kv_data("groups", {}) or {}
        groups: List[GroupConfig] = []
        for gid, cfg in raw.items():
            if not isinstance(cfg, dict):
                continue
            try:
                groups.append(GroupConfig(group_id=str(gid), **cfg))
            except Exception:
                logger.warning("群配置格式异常，已跳过: %s", gid)
        return groups

    async def remember_group(self, group_id: str) -> None:
        raw = await self.get_kv_data("groups", {}) or {}
        gid = str(group_id)
        if gid in raw:
            return
        raw[gid] = {
            "group_id": gid,
            "enabled": True,
            "morning_push_time": DEFAULT_MORNING_TIME,
            "push_platforms": list(DEFAULT_PLATFORMS),
            "reminder_enabled": True,
        }
        await self.put_kv_data("groups", raw)
        logger.info("acmerQQ群机器人 已自动注册群 %s", gid)

    # ------------------------------------------------------------------
    # 主动推送（后台定时任务）
    # ------------------------------------------------------------------
    async def send_notification(self, group: GroupConfig, text: str) -> None:
        """发送通知；若开启 @全体成员 且有权限则带 @，否则自动降级为普通通知。"""
        settings = await self.get_settings()
        at_all = bool(settings.get("at_all_enabled", False))
        blocked_until = await self.get_kv_data(
            f"at_all_blocked_until_{group.group_id}", 0.0
        )
        if at_all and (blocked_until or 0) < time.time():
            sent = await self._post_to_group(
                group.group_id, "<@everyone>\n" + text
            )
            if sent:
                return
            logger.warning(
                "群 %s 发送 @全体成员 失败，自动降级为普通通知", group.group_id
            )
            await self.put_kv_data(
                f"at_all_blocked_until_{group.group_id}",
                time.time() + AT_ALL_BLOCK_SECONDS,
            )
        await self._post_to_group(group.group_id, text)

    async def _post_to_group(self, group_id: str, text: str) -> bool:
        session = MessageSesion(
            platform_name="qq_official",
            message_type=MessageType.GROUP_MESSAGE,
            session_id=str(group_id),
        )
        try:
            ok = await self.context.send_message(
                session, MessageChain([Plain(text)])
            )
            if not ok:
                logger.warning("发送到群 %s 失败：未找到匹配平台", group_id)
                return False
            return True
        except Exception as exc:
            logger.error("发送到群 %s 失败: %s", group_id, exc)
            return False

    async def build_morning_text(self, group: GroupConfig) -> Optional[str]:
        settings = await self.get_settings()
        platforms = [p for p in settings["push_platforms"] if p in group.push_platforms]
        if not platforms:
            platforms = list(DEFAULT_PLATFORMS)
        today = datetime.now(CN_TZ).date()
        lines = ["🌅 今日比赛早报"]
        found = False
        for platform in platforms:
            contests, err = await self.fetcher.fetch_platform(platform)
            label = PLATFORM_LABELS.get(platform, platform)
            if err:
                lines.append(f"{label}：{err}")
                continue
            todays = [
                c
                for c in contests
                if c.is_upcoming() and c.start_cn().date() == today
            ]
            if not todays:
                continue
            found = True
            lines.append(f"— {label} —")
            for contest in todays:
                lines.append(f"{contest.start_cn():%H:%M} {contest.name}")
        if not found:
            return None
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 指令：比赛查询
    # ------------------------------------------------------------------
    async def _reply_platform(self, event: AstrMessageEvent, platform: str):
        label = PLATFORM_LABELS.get(platform, platform)
        contests, err = await self.fetcher.fetch_platform(platform)
        if err:
            yield event.plain_result(err)
            return
        upcoming = [c for c in contests if c.is_upcoming()]
        if not upcoming:
            yield event.plain_result(f"{label} 近期暂无比赛")
            return
        yield event.plain_result(upcoming[0].format_detail())

    @filter.command("nc", alias={"牛客"})
    async def nc(self, event: AstrMessageEvent):
        async for result in self._reply_platform(event, "nowcoder"):
            yield result

    @filter.command("cf", alias={"codeforces"})
    async def cf(self, event: AstrMessageEvent):
        async for result in self._reply_platform(event, "codeforces"):
            yield result

    @filter.command("atc", alias={"atcoder"})
    async def atc(self, event: AstrMessageEvent):
        async for result in self._reply_platform(event, "atcoder"):
            yield result

    @filter.command("lg", alias={"洛谷"})
    async def lg(self, event: AstrMessageEvent):
        async for result in self._reply_platform(event, "luogu"):
            yield result

    @filter.command("update", alias={"刷新比赛"})
    async def update(self, event: AstrMessageEvent):
        if not await self._is_admin(event):
            yield event.plain_result("此指令仅限管理员")
            return
        parts = ["🔄 比赛数据刷新完成"]
        for platform in DEFAULT_PLATFORMS:
            contests, err = await self.fetcher.fetch_platform(platform, force=True)
            label = PLATFORM_LABELS.get(platform, platform)
            if err:
                parts.append(f"{label}：{err}")
            else:
                parts.append(
                    f"{label}：{len([c for c in contests if c.is_upcoming()])} 场"
                )
        yield event.plain_result("\n".join(parts))

    @filter.command("acm", alias={"比赛帮助"})
    async def acm_help(self, event: AstrMessageEvent):
        yield event.plain_result(MENU_TEXT)

    @filter.platform_adapter_type(filter.PlatformAdapterType.QQOFFICIAL)
    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE
        | filter.EventMessageType.PRIVATE_MESSAGE
    )
    async def on_message(self, event: AstrMessageEvent):
        """功能菜单：发送 “acmer群管理插件菜单” 即可查看（无需 @机器人）。"""
        message_str = (event.message_str or "").strip()
        if message_str.startswith("acmer群管理插件菜单"):
            yield event.plain_result(MENU_TEXT)

    # ------------------------------------------------------------------
    # 群自动注册（只记录，不回复）
    # ------------------------------------------------------------------
    @filter.platform_adapter_type(filter.PlatformAdapterType.QQOFFICIAL)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        if group_id:
            await self.remember_group(group_id)

    # ------------------------------------------------------------------
    # WebUI 配置
    # ------------------------------------------------------------------
    async def _web_config_get(self):
        return json_response(
            {
                "status": "success",
                "data": {
                    "admin_users": await self._get_admins(),
                    "settings": await self.get_settings(),
                    "groups": [g.model_dump() for g in await self.get_groups()],
                },
            }
        )

    async def _web_config_set(self):
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            return error_response("请求体格式不正确")
        try:
            if "admin_users" in payload:
                admins = payload["admin_users"]
                if not isinstance(admins, list):
                    raise ValueError("admin_users 必须是列表")
                normalized = [str(a).strip() for a in admins if str(a).strip()]
                await self.put_kv_data("admin_users", normalized)
            if "settings" in payload:
                settings = payload["settings"]
                if not isinstance(settings, dict):
                    raise ValueError("settings 必须是对象")
                morning = settings.get(
                    "morning_push_time", DEFAULT_MORNING_TIME
                )
                validate_hhmm(morning)
                platforms = settings.get("push_platforms") or list(DEFAULT_PLATFORMS)
                if not isinstance(platforms, list):
                    raise ValueError("push_platforms 必须是列表")
                platforms = [p for p in DEFAULT_PLATFORMS if p in platforms]
                current = await self.get_settings()
                await self.put_kv_data(
                    "settings",
                    {
                        "morning_push_time": morning,
                        "push_platforms": platforms or list(DEFAULT_PLATFORMS),
                        "reminder_enabled": bool(
                            settings.get(
                                "reminder_enabled", current["reminder_enabled"]
                            )
                        ),
                        "at_all_enabled": bool(
                            settings.get(
                                "at_all_enabled", current["at_all_enabled"]
                            )
                        ),
                    },
                )
            if "groups" in payload:
                groups = payload["groups"]
                if not isinstance(groups, list):
                    raise ValueError("groups 必须是列表")
                raw = {}
                for item in groups:
                    if not isinstance(item, dict) or not item.get("group_id"):
                        continue
                    gid = str(item["group_id"])
                    try:
                        cfg = GroupConfig(group_id=gid, **item)
                    except Exception as exc:
                        raise ValueError(f"群 {gid} 配置不合法：{exc}") from exc
                    raw[gid] = cfg.model_dump()
                await self.put_kv_data("groups", raw)
        except ValueError as exc:
            return error_response(str(exc))
        except Exception as exc:
            logger.error("保存 acmerQQ群机器人 配置失败: %s", exc, exc_info=True)
            return error_response(f"保存失败：{exc}")
        return json_response({"status": "success", "data": {"message": "配置已保存并生效"}})
