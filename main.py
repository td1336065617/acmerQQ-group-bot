"""acmerQQ群机器人：ACM 竞赛信息查询与定时推送插件。

功能菜单：发送 “acmer群管理插件菜单” 查看全部指令与所需权限。
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

# AstrBot 加载插件时不会自动把插件根目录加入 sys.path，这里手动加入，
# 否则 `from src...` 子包导入会报 No module named 'src'
_PLUGIN_ROOT = str(Path(__file__).resolve().parent)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

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
# 比赛列表最多展示的条数（防止消息过长）
MAX_CONTEST_LIST = 30

MENU_TEXT = (
    "acmer群管理插件菜单\n"
    "【所有人可用】\n"
    "acmer激活 - 首次激活本群主动推送（重启后群内任意消息自动恢复）\n"
    "/nk（牛客）- 牛客全部未开始比赛\n"
    "/最近nk（最近牛客）- 牛客最近一场\n"
    "/cf（codeforces）- Codeforces 全部未开始比赛\n"
    "/最近cf（最近Codeforces）- Codeforces 最近一场\n"
    "/atc（atcoder）- AtCoder 全部未开始比赛\n"
    "/最近atc（最近AtCoder）- AtCoder 最近一场\n"
    "/lg（洛谷）- 洛谷全部未开始比赛\n"
    "/最近lg（最近洛谷）- 洛谷最近一场\n"
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
        # 本次运行期间已收到过消息的群（用于自动重新激活日志）
        self._seen_group_this_run: set = set()
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
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/test-push",
                self._web_test_push,
                ["POST"],
                "向指定群发送测试早报推送",
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
                item = dict(cfg)
                item["group_id"] = str(gid)
                groups.append(GroupConfig(**item))
            except Exception:
                logger.warning("群配置格式异常，已跳过: %s", gid)
        return groups

    async def remember_group(self, group_id: str, platform_id: Optional[str] = None) -> None:
        raw = await self.get_kv_data("groups", {}) or {}
        gid = str(group_id)
        changed = False
        if gid in raw:
            cfg = raw[gid]
            if isinstance(cfg, dict) and platform_id and cfg.get("platform_id") != platform_id:
                cfg["platform_id"] = platform_id
                changed = True
        else:
            raw[gid] = {
                "group_id": gid,
                "platform_id": platform_id or "",
                "activated": False,
                "enabled": True,
                "morning_push_time": DEFAULT_MORNING_TIME,
                "push_platforms": list(DEFAULT_PLATFORMS),
                "reminder_enabled": True,
            }
            changed = True
        if changed:
            await self.put_kv_data("groups", raw)
            logger.info("acmerQQ群机器人 已自动注册群 %s", gid)

    # ------------------------------------------------------------------
    # 主动推送（后台定时任务）
    # ------------------------------------------------------------------
    def _qq_platform_id(self) -> str:
        """获取 qq_official 平台实例的真实 ID（会话 platform_name）。"""
        platform_manager = getattr(self.context, "platform_manager", None)
        for inst in getattr(platform_manager, "platform_insts", []) or []:
            try:
                meta = inst.meta()
            except Exception:
                continue
            if getattr(meta, "name", None) == "qq_official":
                return str(getattr(meta, "id", None) or "qq_official")
        return "qq_official"

    def _group_scene_ready(self, group_id: str) -> bool:
        """QQ 主动推送是否就绪：该群在本次运行期间给机器人发过消息。"""
        platform_manager = getattr(self.context, "platform_manager", None)
        for inst in getattr(platform_manager, "platform_insts", []) or []:
            try:
                meta = inst.meta()
            except Exception:
                continue
            if getattr(meta, "name", None) != "qq_official":
                continue
            scene = getattr(inst, "_session_scene", {}).get(str(group_id))
            if scene == "group":
                return True
        return False

    async def send_notification(self, group: GroupConfig, text: str) -> bool:
        """发送通知；返回是否发送成功。@全体成员 开启且无权限时自动降级。"""
        if not self._group_scene_ready(group.group_id):
            logger.warning(
                "群 %s 主动推送会话未就绪（本次运行该群还没给机器人发过消息），"
                "跳过发送；请先让群内发一条消息",
                group.group_id,
            )
            return False
        settings = await self.get_settings()
        at_all = bool(settings.get("at_all_enabled", False))
        blocked_until = await self.get_kv_data(
            f"at_all_blocked_until_{group.group_id}", 0.0
        )
        if at_all and (blocked_until or 0) < time.time():
            sent = await self._post_to_group(
                group.group_id,
                "<@everyone>\n" + text,
                platform_id=group.platform_id or self._qq_platform_id(),
            )
            if sent:
                logger.info("已向群 %s 发送 @全体成员 通知", group.group_id)
                return True
            logger.warning(
                "群 %s 发送 @全体成员 失败，自动降级为普通通知", group.group_id
            )
            await self.put_kv_data(
                f"at_all_blocked_until_{group.group_id}",
                time.time() + AT_ALL_BLOCK_SECONDS,
            )
        sent = await self._post_to_group(
            group.group_id, text, platform_id=group.platform_id or self._qq_platform_id()
        )
        if sent:
            logger.info("已向群 %s 发送普通通知", group.group_id)
        return sent

    async def _post_to_group(
        self, group_id: str, text: str, platform_id: Optional[str] = None
    ) -> bool:
        session = MessageSesion(
            platform_name=platform_id or self._qq_platform_id(),
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
        now_utc = datetime.now(timezone.utc)
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
                if c.start_cn().date() == today
                and (c.end_time is None or c.end_time > now_utc)
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
    async def _reply_platform(
        self, event: AstrMessageEvent, platform: str, mode: str = "all"
    ):
        label = PLATFORM_LABELS.get(platform, platform)
        contests, err = await self.fetcher.fetch_platform(platform)
        if err:
            yield event.plain_result(err)
            return
        upcoming = [c for c in contests if c.is_upcoming()]
        if not upcoming:
            yield event.plain_result(f"{label} 近期暂无比赛")
            return
        if mode == "nearest":
            yield event.plain_result(upcoming[0].format_detail())
            return
        lines = [f"📋 {label} 未开始比赛（共 {len(upcoming)} 场）"]
        for idx, contest in enumerate(upcoming[:MAX_CONTEST_LIST], start=1):
            duration = (
                f" · {contest.duration_minutes} 分钟" if contest.duration_minutes else ""
            )
            lines.append(
                f"{idx}. {contest.name}\n"
                f"   {contest.start_cn():%m-%d %H:%M}{duration}\n"
                f"   {contest.url}"
            )
        if len(upcoming) > MAX_CONTEST_LIST:
            lines.append(f"…共 {len(upcoming)} 场，仅显示前 {MAX_CONTEST_LIST} 场")
        yield event.plain_result("\n".join(lines))

    @filter.command("nk", alias={"牛客"})
    async def nk(self, event: AstrMessageEvent):
        async for result in self._reply_platform(event, "nowcoder", "all"):
            yield result

    @filter.command("cf", alias={"codeforces"})
    async def cf(self, event: AstrMessageEvent):
        async for result in self._reply_platform(event, "codeforces", "all"):
            yield result

    @filter.command("atc", alias={"atcoder"})
    async def atc(self, event: AstrMessageEvent):
        async for result in self._reply_platform(event, "atcoder", "all"):
            yield result

    @filter.command("lg", alias={"洛谷"})
    async def lg(self, event: AstrMessageEvent):
        async for result in self._reply_platform(event, "luogu", "all"):
            yield result

    @filter.command("最近nk", alias={"最近牛客"})
    async def recent_nk(self, event: AstrMessageEvent):
        async for result in self._reply_platform(event, "nowcoder", "nearest"):
            yield result

    @filter.command("最近cf", alias={"最近Codeforces"})
    async def recent_cf(self, event: AstrMessageEvent):
        async for result in self._reply_platform(event, "codeforces", "nearest"):
            yield result

    @filter.command("最近atc", alias={"最近AtCoder"})
    async def recent_atc(self, event: AstrMessageEvent):
        async for result in self._reply_platform(event, "atcoder", "nearest"):
            yield result

    @filter.command("最近lg", alias={"最近洛谷"})
    async def recent_lg(self, event: AstrMessageEvent):
        async for result in self._reply_platform(event, "luogu", "nearest"):
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
            return
        if message_str.startswith("acmer激活"):
            async for result in self._activate_group(event):
                yield result

    @filter.command("acmer激活", alias={"激活"})
    async def acmer_activate(self, event: AstrMessageEvent):
        async for result in self._activate_group(event):
            yield result

    async def _activate_group(self, event: AstrMessageEvent):
        """重启后激活本群主动推送：本条消息本身会写入 QQ 适配器会话缓存。"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("私聊无需激活，主动推送仅用于群聊")
            return
        await self.remember_group(
            str(group_id),
            platform_id=getattr(event.session, "platform_name", None),
        )
        # 已激活的群：重复发送激活命令不再回复，静默忽略
        current = next(
            (g for g in await self.get_groups() if g.group_id == str(group_id)),
            None,
        )
        if current is not None and current.activated:
            logger.info("群 %s 已处于激活状态，忽略重复激活命令", group_id)
            return
        ready = self._group_scene_ready(str(group_id))
        if ready:
            raw = await self.get_kv_data("groups", {}) or {}
            cfg = raw.get(str(group_id))
            if isinstance(cfg, dict):
                cfg["activated"] = True
                await self.put_kv_data("groups", raw)
            yield event.plain_result(
                "✅ 主动推送已激活！本群已启用每日早报与赛前提醒。"
                "激活状态会持久保存：AstrBot 重启后，群内任意一条消息即可自动恢复，"
                "无需再次发送 acmer激活"
            )
        else:
            # 正常不会走到这里：本条消息已触发适配器缓存；
            # 兜底提示避免用户误以为未激活。
            yield event.plain_result(
                "✅ 已收到激活消息，会话缓存已写入；下一次推送即可正常发送"
            )

    # ------------------------------------------------------------------
    # 群自动注册（只记录，不回复）
    # ------------------------------------------------------------------
    @filter.platform_adapter_type(filter.PlatformAdapterType.QQOFFICIAL)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        if group_id:
            gid = str(group_id)
            first_this_run = gid not in self._seen_group_this_run
            self._seen_group_this_run.add(gid)
            await self.remember_group(
                gid, platform_id=getattr(event.session, "platform_name", None)
            )
            if first_this_run:
                raw = await self.get_kv_data("groups", {}) or {}
                cfg = raw.get(gid)
                if isinstance(cfg, dict) and cfg.get("activated"):
                    logger.info(
                        "群 %s 已自动重新激活（重启后收到首条消息）", gid
                    )

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
                    "platform_id": self._qq_platform_id(),
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

    async def _web_test_push(self):
        """向指定群立即发送一次测试早报（用于验证主动推送链路）。"""
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            return error_response("请求体格式不正确")
        group_id = str(payload.get("group_id") or "").strip()
        if not group_id:
            return error_response("缺少 group_id")
        group = next(
            (g for g in await self.get_groups() if g.group_id == group_id), None
        )
        if group is None:
            return error_response("该群未注册，请先让群内发一条消息")
        if not group.enabled:
            return error_response("该群已停用推送")
        if not self._group_scene_ready(group_id):
            return error_response(
                "QQ 主动推送会话未就绪：请先让该群给机器人发一条消息，再点测试推送"
            )
        text = await self.build_morning_text(group)
        if not text:
            return error_response("今天没有可推送的比赛")
        sent = await self.send_notification(group, text)
        if not sent:
            return error_response("发送失败，请查看 AstrBot 日志")
        return json_response({"status": "success", "data": {"message": "测试推送已发送"}})
