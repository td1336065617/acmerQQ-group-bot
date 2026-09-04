"""acmerQQ群机器人：ACM 竞赛信息查询与定时推送插件。

功能菜单：发送 “acmer群管理插件菜单” 查看全部指令与所需权限。
"""
from __future__ import annotations

import asyncio
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

# AstrBot 按 `data.plugins.<插件目录>.main` 加载插件，使用相对导入可避免
# 不同插件/旧版本之间共享顶层 `src` 模块缓存。
_PLUGIN_ROOT = str(Path(__file__).resolve().parent)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.platform import MessageType
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.platform.message_session import MessageSesion

from .src.contest_fetcher import ContestFetcher
from .src.account_cards import AccountCardRenderer
from .src.account_fetcher import (
    AccountFetcher,
    normalize_account_identifier,
)
from .src.account_models import (
    ACCOUNT_PLATFORMS,
    VERIFICATION_FIELD_LABELS,
    AccountFetchError,
    normalize_platform,
    platform_label,
)
from .src.account_registry import AccountRegistry
from .src.models import (
    CN_TZ,
    DEFAULT_PLATFORMS,
    PLATFORM_LABELS,
    GroupConfig,
)
from .src.output_renderer import AdaptiveOutputRenderer, text_chunks
from .src.scheduler import PushScheduler
from .src.utils import (
    contest_start_utc,
    is_contest_in_recent_window,
    normalize_command,
    validate_hhmm,
)

# 这两个常量在主程序内定义，避免 AstrBot 更新过程中只替换 main.py
# 时因为旧版 contest_fetcher.py 尚未同步而无法加载插件。
OFFLINE_PLATFORM = "offline"
QUERY_PLATFORMS = [*DEFAULT_PLATFORMS, OFFLINE_PLATFORM]

PLUGIN_NAME = "acmer_qq_group_bot"
DEFAULT_MORNING_TIME = "08:00"
# 文字转图片阈值：可在 WebUI 中调整。这里保留默认值，保证旧配置/旧数据库
# 没有新增字段时行为与此前版本一致。
DEFAULT_MAX_PLAIN_TEXT_CHARS = 1800
DEFAULT_MAX_PLAIN_TEXT_LINES = 36
MIN_MAX_PLAIN_TEXT_CHARS = 200
MAX_MAX_PLAIN_TEXT_CHARS = 10000
MIN_MAX_PLAIN_TEXT_LINES = 10
MAX_MAX_PLAIN_TEXT_LINES = 200
# “最近比赛”查询窗口：默认查看未来 7 天内开赛/仍在进行的比赛。
DEFAULT_RECENT_CONTEST_DAYS = 7
MIN_RECENT_CONTEST_DAYS = 1
MAX_RECENT_CONTEST_DAYS = 30
# @全体成员 尝试失败后，对该群暂缓重试的时间（秒）
AT_ALL_BLOCK_SECONDS = 6 * 3600
# 比赛列表最多展示的条数（防止消息过长）
MAX_CONTEST_LIST = 30


def _format_signed_number(value: object) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{int(value):+d}"
    except (TypeError, ValueError):
        return str(value)

ACCOUNT_BIND_RE = re.compile(
    r"^(?:绑定|bind)\s*(cf|codeforces|nk|牛客|nowcoder|lg|洛谷|luogu|"
    r"atc|atcoder)\s+(.+?)\s*$",
    re.I,
)
ACCOUNT_CONFIRM_RE = re.compile(
    r"^(?:确认绑定|confirm\s*bind)\s*(cf|codeforces|nk|牛客|nowcoder|"
    r"lg|洛谷|luogu|atc|atcoder)(?:\s+(.+?))?\s*$",
    re.I,
)
ACCOUNT_UNBIND_RE = re.compile(
    r"^(?:解绑|unbind)\s*(cf|codeforces|nk|牛客|nowcoder|lg|洛谷|"
    r"luogu|atc|atcoder)\s*$",
    re.I,
)
MY_PLATFORM_COMMANDS = {
    normalize_command("我的cf"): "codeforces",
    normalize_command("我的codeforces"): "codeforces",
    normalize_command("我的牛客"): "nowcoder",
    normalize_command("我的nk"): "nowcoder",
    normalize_command("我的nowcoder"): "nowcoder",
    normalize_command("我的洛谷"): "luogu",
    normalize_command("我的lg"): "luogu",
    normalize_command("我的luogu"): "luogu",
    normalize_command("我的atcoder"): "atcoder",
    normalize_command("我的atc"): "atcoder",
}
MY_ACCOUNT_COMMANDS = {
    normalize_command("我的账号"),
    normalize_command("我的战绩"),
    normalize_command("刷新我的战绩"),
}
JOIN_RANK_COMMANDS = {
    normalize_command("加入群排行"),
    normalize_command("加入排行"),
}
LEAVE_RANK_COMMANDS = {
    normalize_command("退出群排行"),
    normalize_command("退出排行"),
}
GROUP_RANK_COMMANDS = {
    normalize_command("群排行"): None,
    normalize_command("本周进步榜"): "progress",
    normalize_command("群cf排行"): "codeforces",
    normalize_command("群codeforces排行"): "codeforces",
    normalize_command("群牛客排行"): "nowcoder",
    normalize_command("群nk排行"): "nowcoder",
    normalize_command("群洛谷排行"): "luogu",
    normalize_command("群lg排行"): "luogu",
    normalize_command("群atcoder排行"): "atcoder",
    normalize_command("群atc排行"): "atcoder",
}

MENU_TEXT = (
    "🏆 acmer群管理插件菜单\n"
    "━━━━━━━━━━━━\n"
    "👥 所有人可用\n"
    "• acmer激活 ─ 首次激活本群主动推送（重启后群内任意消息自动恢复）\n"
    "• 绑定cf/绑定牛客/绑定洛谷/绑定atcoder ─ 绑定个人竞赛账号\n"
    "• 确认绑定/解绑 ─ 完成或解除平台账号绑定\n"
    "• 我的战绩/我的账号 ─ 查看四平台个人战绩卡\n"
    "• 我的cf/我的牛客/我的洛谷/我的atcoder ─ 查看单个平台战绩卡\n"
    "• 群排行 ─ 查看四个平台排行总览\n"
    "• 群cf排行/群牛客排行/群洛谷排行/群atcoder排行 ─ 查看平台排行\n"
    "• 本周进步榜 ─ 查看各平台本周 Rating 变化\n"
    "• 加入群排行/退出群排行 ─ 管理当前群的排行展示\n"
    "• 最近比赛 ─ 汇总所有平台未来 N 天内及进行中的比赛（N 可在 WebUI 设置）\n"
    "• nk比赛 / 牛客比赛 ─ 牛客全部未开始比赛\n"
    "• 最近nk比赛 / 最近牛客比赛 ─ 牛客最近一场比赛\n"
    "• cf比赛 / Codeforces比赛 ─ Codeforces 全部未开始比赛\n"
    "• 最近cf比赛 / 最近Codeforces比赛 ─ Codeforces 最近一场比赛\n"
    "• atc比赛 / AtCoder比赛 ─ AtCoder 全部未开始比赛\n"
    "• 最近atc比赛 / 最近AtCoder比赛 ─ AtCoder 最近一场比赛\n"
    "• lg比赛 / 洛谷比赛 ─ 洛谷全部未开始比赛\n"
    "• 最近lg比赛 / 最近洛谷比赛 ─ 洛谷最近一场比赛\n"
    "• 线下赛 ─ XCPC Link 线下比赛赛程\n"
    "• acm菜单 / acmer群管理插件菜单 ─ 显示本菜单\n"
    "━━━━━━━━━━━━\n"
    "🔑 仅管理员\n"
    "• update / 刷新比赛 ─ 强制刷新全部比赛数据\n"
    "━━━━━━━━━━━━\n"
    "提示：指令为全匹配，发送完整指令才会触发；也可带 / 前缀（如 /nk比赛）\n"
    "⚙️ 推送配置（早报/提醒/@全体/长消息转图）：WebUI acmerQQ群机器人 页\n"
    "🔗 开源：https://github.com/td1336065617/acmerQQ-group-bot"
)

# 全匹配指令表：消息必须与指令完全一致才会触发（避免误伤聊天内容）
QUERY_COMMANDS = {
    "nk比赛": ("nowcoder", "all"),
    "牛客比赛": ("nowcoder", "all"),
    "cf比赛": ("codeforces", "all"),
    "codeforces比赛": ("codeforces", "all"),
    "atc比赛": ("atcoder", "all"),
    "atcoder比赛": ("atcoder", "all"),
    "lg比赛": ("luogu", "all"),
    "洛谷比赛": ("luogu", "all"),
    "最近nk比赛": ("nowcoder", "nearest"),
    "最近牛客比赛": ("nowcoder", "nearest"),
    "最近cf比赛": ("codeforces", "nearest"),
    "最近codeforces比赛": ("codeforces", "nearest"),
    "最近atc比赛": ("atcoder", "nearest"),
    "最近atcoder比赛": ("atcoder", "nearest"),
    "最近lg比赛": ("luogu", "nearest"),
    "最近洛谷比赛": ("luogu", "nearest"),
}
QUERY_COMMANDS = {
    normalize_command(command): value for command, value in QUERY_COMMANDS.items()
}
MENU_COMMANDS = {
    normalize_command(command)
    for command in ("acmer群管理插件菜单", "acm菜单", "比赛帮助")
}
OFFLINE_COMMANDS = {
    normalize_command(command)
    for command in ("线下赛", "线下比赛", "XCPC线下赛")
}
ACTIVATE_COMMAND = normalize_command("acmer激活")
UPDATE_COMMANDS = {
    normalize_command(command) for command in ("update", "刷新比赛")
}
RECENT_ALL_COMMANDS = {
    normalize_command(command) for command in ("最近比赛", "近期比赛")
}


class AcmerGroupBot(Star):
    def __init__(self, context: Context, config: Optional[dict] = None) -> None:
        super().__init__(context, config)
        self.config = config if isinstance(config, dict) else {}
        self.fetcher = ContestFetcher()
        self.account_fetcher = AccountFetcher()
        self.account_registry = AccountRegistry(self)
        self.account_card_renderer = AccountCardRenderer(
            cache_dir=Path(__file__).resolve().parent / "data" / "account_cards"
        )
        self.output_renderer = AdaptiveOutputRenderer(
            cache_dir=Path(__file__).resolve().parent / "data" / "output_cache"
        )
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
        await self.account_fetcher.initialize(self.fetcher.session)
        await self.scheduler.start()
        logger.info(
            "acmerQQ群机器人 已启动；若消息指令无响应，请检查 AstrBot "
            "设置→插件配置→可用插件（plugin_set）是否包含本插件（或设为全部）"
        )

    async def terminate(self) -> None:
        await self.scheduler.stop()
        await self.account_fetcher.close()
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

    @staticmethod
    def _read_bounded_int(
        value: object, default: int, minimum: int, maximum: int
    ) -> int:
        """读取后台整数配置；缺失、类型错误或越界时使用默认值。"""
        if isinstance(value, bool):
            return default
        if isinstance(value, float) and not value.is_integer():
            return default
        try:
            parsed = int(value)
        except (OverflowError, TypeError, ValueError):
            return default
        return parsed if minimum <= parsed <= maximum else default

    @staticmethod
    def _validate_bounded_int(
        value: object,
        field_name: str,
        minimum: int,
        maximum: int,
    ) -> int:
        """严格校验 WebUI 提交的整数配置，并返回规范化整数。"""
        if isinstance(value, bool) or (
            isinstance(value, float) and not value.is_integer()
        ):
            raise ValueError(f"{field_name} 必须是整数")
        try:
            parsed = int(value)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} 必须是整数") from exc
        if not minimum <= parsed <= maximum:
            raise ValueError(
                f"{field_name} 应在 {minimum} 到 {maximum} 之间"
            )
        return parsed

    def _configure_output_renderer(self, settings: dict) -> None:
        renderer = getattr(self, "output_renderer", None)
        if renderer is None:
            return
        max_chars = settings["max_plain_text_chars"]
        max_lines = settings["max_plain_text_lines"]
        configure = getattr(renderer, "configure", None)
        if callable(configure):
            configure(max_chars, max_lines)
        else:
            # 兼容插件文件分批更新时暂时加载到的旧版渲染器。
            renderer.max_chars = max(1, int(max_chars))
            renderer.max_lines = max(1, int(max_lines))

    async def get_settings(self) -> dict:
        raw = await self.get_kv_data("settings", {}) or {}
        if not isinstance(raw, dict):
            raw = {}
        try:
            morning = validate_hhmm(
                raw.get("morning_push_time", DEFAULT_MORNING_TIME)
            )
        except ValueError:
            morning = DEFAULT_MORNING_TIME
        raw_platforms = raw.get("push_platforms")
        if not isinstance(raw_platforms, list):
            raw_platforms = list(DEFAULT_PLATFORMS)
        platforms = raw_platforms or list(DEFAULT_PLATFORMS)
        platforms = [p for p in DEFAULT_PLATFORMS if p in platforms]
        settings = {
            "morning_push_time": morning,
            "push_platforms": platforms or list(DEFAULT_PLATFORMS),
            "reminder_enabled": bool(raw.get("reminder_enabled", True)),
            "at_all_enabled": bool(raw.get("at_all_enabled", False)),
            "max_plain_text_chars": self._read_bounded_int(
                raw.get("max_plain_text_chars"),
                DEFAULT_MAX_PLAIN_TEXT_CHARS,
                MIN_MAX_PLAIN_TEXT_CHARS,
                MAX_MAX_PLAIN_TEXT_CHARS,
            ),
            "max_plain_text_lines": self._read_bounded_int(
                raw.get("max_plain_text_lines"),
                DEFAULT_MAX_PLAIN_TEXT_LINES,
                MIN_MAX_PLAIN_TEXT_LINES,
                MAX_MAX_PLAIN_TEXT_LINES,
            ),
            "recent_contest_days": self._read_bounded_int(
                raw.get("recent_contest_days"),
                DEFAULT_RECENT_CONTEST_DAYS,
                MIN_RECENT_CONTEST_DAYS,
                MAX_RECENT_CONTEST_DAYS,
            ),
        }
        # 每次读取配置时同步一次，兼容管理员从其他入口修改 KV 或热更新配置。
        self._configure_output_renderer(settings)
        return settings

    @staticmethod
    def _event_display_name(event: AstrMessageEvent) -> str:
        """尽量获取 QQ 昵称；适配器未提供时回退到 sender_id。"""
        for method_name in ("get_sender_name", "get_sender_nickname"):
            method = getattr(event, method_name, None)
            if callable(method):
                try:
                    value = method()
                    if value:
                        return str(value).strip()
                except Exception:
                    pass
        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None)
        for attr in ("nickname", "card", "name"):
            value = getattr(sender, attr, None)
            if value:
                return str(value).strip()
        return str(event.get_sender_id() or "QQ用户")

    @staticmethod
    def _account_platform_help() -> str:
        return (
            "用法：绑定cf/绑定牛客/绑定洛谷/绑定atcoder <用户名、UID或主页链接>\n"
            "验证字段：CF 姓氏、牛客个性签名、洛谷个人介绍、"
            "AtCoder Affiliation（所属）"
        )

    @staticmethod
    def _account_error_text(platform: str, exc: Exception) -> str:
        message = str(exc).strip() or "平台暂时无法访问，请稍后重试"
        if platform == "luogu" and (
            "暂时无法绑定" in message or "个人资料暂时无法读取" in message
        ):
            return "⚠️ 洛谷个人介绍暂时无法读取，暂时无法绑定"
        return f"⚠️ {platform_label(platform)}：{message}"

    async def _load_bound_profiles(
        self,
        user_id: str,
        *,
        detail: bool = True,
        force: bool = False,
    ):
        """读取用户已绑定账号；单个平台失败不会影响其他平台。"""
        accounts = await self.account_registry.get_user_accounts(user_id)
        tasks = []
        for platform in ACCOUNT_PLATFORMS:
            record = accounts.get(platform)
            if not isinstance(record, dict):
                continue
            identifier = str(
                record.get("platform_user_id") or record.get("handle") or ""
            ).strip()
            if not identifier:
                continue
            tasks.append(
                (
                    platform,
                    record,
                    asyncio.create_task(
                        self.account_fetcher.get_profile(
                            platform,
                            identifier,
                            detail=detail,
                            force=force,
                            include_submissions=False,
                        )
                    ),
                )
            )
        profiles = []
        errors = []
        if not tasks:
            return accounts, profiles, errors
        results = await asyncio.gather(
            *(task for _, _, task in tasks),
            return_exceptions=True,
        )
        for (platform, record, _), result in zip(tasks, results):
            if isinstance(result, Exception):
                errors.append((platform, result))
                continue
            profiles.append(result)
            await self._record_profile_metric(user_id, result)
        return accounts, profiles, errors

    @staticmethod
    def _profile_metric(profile) -> Optional[dict]:
        """返回排行/快照使用的统一指标；洛谷没有 Elo 时按公开排名排行。"""
        if profile.rating is not None:
            return {
                "snapshot_key": profile.platform,
                "value": int(profile.rating),
                "display_value": str(profile.rating),
                "metric_label": "Rating",
                "sort_value": int(profile.rating),
            }
        if (
            profile.platform == "luogu"
            and profile.rating_rank is not None
        ):
            rank = int(profile.rating_rank)
            return {
                "snapshot_key": "luogu_rank",
                "value": -rank,
                "display_value": f"#{rank}",
                "metric_label": "平台排名",
                "sort_value": -rank,
            }
        return None

    async def _record_profile_metric(self, user_id: str, profile) -> None:
        metric = self._profile_metric(profile)
        if metric is None:
            return
        await self.account_registry.record_rating(
            user_id,
            metric["snapshot_key"],
            metric["value"],
        )

    async def _profile_weekly_delta(self, user_id: str, profile) -> Optional[int]:
        calculator = getattr(
            self.account_fetcher, "rating_delta_for_period", None
        )
        direct = (
            calculator(profile, days=7)
            if callable(calculator)
            else None
        )
        if direct is not None:
            return direct
        metric = self._profile_metric(profile)
        if metric is None:
            return None
        return await self.account_registry.weekly_delta(
            user_id,
            metric["snapshot_key"],
            days=7,
        )

    @classmethod
    def _format_account_text(
        cls,
        profiles,
        errors,
        weekly_changes=None,
        *,
        title: str = "📊 我的竞赛战绩",
    ) -> str:
        weekly_changes = weekly_changes or {}
        lines = [title]
        for profile in profiles:
            label = platform_label(profile.platform)
            metric = cls._profile_metric(profile)
            metric_value = (
                metric["display_value"] if metric is not None else "未评级"
            )
            metric_label = metric["metric_label"] if metric is not None else "Rating"
            rank = profile.rating_rank or profile.rank_text or "—"
            delta = weekly_changes.get(profile.platform)
            if delta is None:
                delta = profile.recent_delta
            lines.append(
                f"【{label}】{profile.handle}｜{metric_label}：{metric_value}"
                f"｜排名：{rank}"
            )
            lines.append(
                f"  最高：{profile.max_rating or '—'}｜"
                f"参赛：{profile.contest_count or '—'}｜"
                f"本周变化：{_format_signed_number(delta)}"
            )
            if profile.profile_url:
                lines.append(f"  {profile.profile_url}")
        for platform, exc in errors:
            lines.append(
                f"⚠️ {platform_label(platform)}同步失败：{str(exc)}"
            )
        return "\n".join(lines)

    async def _reply_account_bind(
        self,
        event: AstrMessageEvent,
        platform: str,
        identifier: str,
    ):
        user_id = str(event.get_sender_id() or "").strip()
        if not user_id:
            yield event.plain_result("无法识别 QQ 用户，请稍后重试")
            return
        try:
            profile = await self.account_fetcher.get_profile(
                platform, identifier, detail=False, force=True
            )
            # 洛谷个人介绍不可读时，直接按用户约定返回明确反馈。
            if platform == "luogu" and not profile.verification_value.strip():
                yield event.plain_result(
                    "⚠️ 洛谷个人介绍暂时无法读取，暂时无法绑定"
                )
                return
            token = await self.account_registry.create_pending(
                user_id,
                platform,
                profile,
                group_id=str(event.get_group_id() or ""),
            )
        except AccountFetchError as exc:
            yield event.plain_result(self._account_error_text(platform, exc))
            return
        except Exception as exc:
            logger.warning("创建%s绑定挑战失败：%s", platform_label(platform), exc)
            yield event.plain_result(self._account_error_text(platform, exc))
            return

        field = VERIFICATION_FIELD_LABELS.get(platform, "公开资料字段")
        group_hint = (
            "\n建议在机器人私聊中完成绑定，避免验证码出现在群消息里。"
            if event.get_group_id()
            else ""
        )
        yield event.plain_result(
            f"✅ 已找到 {platform_label(platform)} 账号：{profile.handle}\n"
            f"请在该账号的【{field}】中追加：{token}\n"
            f"修改完成后发送：确认绑定{platform}{group_hint}\n"
            "验证码 10 分钟内有效，验证成功后可以删除。"
        )

    async def _reply_account_confirm(
        self,
        event: AstrMessageEvent,
        platform: str,
        identifier: str = "",
    ):
        user_id = str(event.get_sender_id() or "").strip()
        pending = await self.account_registry.get_pending(user_id, platform)
        if pending is None:
            yield event.plain_result(
                f"没有找到待确认的{platform_label(platform)}绑定请求，"
                f"请先发送：绑定{platform}<账号>"
            )
            return
        pending_identifier = str(
            pending.get("platform_user_id") or pending.get("handle") or ""
        )
        if identifier:
            normalized = normalize_account_identifier(
                platform, identifier
            )
            if normalized and normalized.casefold() != pending_identifier.casefold():
                yield event.plain_result(
                    f"待确认账号是 {pending.get('handle') or pending_identifier}，"
                    "如需更换请重新发送绑定指令"
                )
                return
        try:
            profile = await self.account_fetcher.get_profile(
                platform,
                pending_identifier,
                detail=False,
                force=True,
            )
        except AccountFetchError as exc:
            yield event.plain_result(self._account_error_text(platform, exc))
            return
        except Exception as exc:
            yield event.plain_result(self._account_error_text(platform, exc))
            return

        if platform == "luogu" and not profile.verification_value.strip():
            await self.account_registry.clear_pending(user_id, platform)
            yield event.plain_result(
                "⚠️ 洛谷个人介绍暂时无法读取，暂时无法绑定"
            )
            return
        expected_hash = str(pending.get("token_hash") or "")
        if not self.account_registry.token_matches(
            profile.verification_value, expected_hash
        ):
            field = VERIFICATION_FIELD_LABELS.get(platform, "公开资料字段")
            yield event.plain_result(
                f"暂未在{field}中找到验证码，请确认已经追加正确验证码，"
                "然后再次发送确认绑定指令"
            )
            return

        group_id = str(event.get_group_id() or "").strip() or str(
            pending.get("group_id") or ""
        ).strip()
        try:
            await self.account_registry.save_binding(
                user_id,
                platform,
                profile,
                group_id=group_id,
                qq_name=self._event_display_name(event),
            )
            await self._record_profile_metric(user_id, profile)
        except ValueError as exc:
            yield event.plain_result(f"⚠️ 绑定失败：{exc}")
            return
        except Exception as exc:
            logger.error("保存%s绑定失败：%s", platform_label(platform), exc, exc_info=True)
            yield event.plain_result("⚠️ 绑定保存失败，请稍后重试")
            return
        yield event.plain_result(
            f"🎉 {platform_label(platform)} 账号 {profile.handle} 绑定成功！\n"
            "已自动加入当前群的竞赛排行（如在私聊绑定，请在目标群发送一次“我的战绩”）。"
        )

    async def _reply_account_unbind(
        self, event: AstrMessageEvent, platform: str
    ):
        user_id = str(event.get_sender_id() or "").strip()
        removed = await self.account_registry.remove_binding(user_id, platform)
        if removed:
            remaining = await self.account_registry.get_user_accounts(user_id)
            if not remaining:
                await self.account_registry.remove_user_from_all_groups(user_id)
                suffix = "，并退出所有群排行"
            else:
                suffix = ""
            yield event.plain_result(f"✅ 已解绑 {platform_label(platform)}{suffix}")
        else:
            yield event.plain_result(f"你还没有绑定{platform_label(platform)}账号")

    async def _reply_my_account(
        self,
        event: AstrMessageEvent,
        *,
        platform: Optional[str] = None,
        force: bool = False,
    ):
        user_id = str(event.get_sender_id() or "").strip()
        await self.account_registry.set_user_display_name(
            user_id, self._event_display_name(event)
        )
        if platform:
            accounts = await self.account_registry.get_user_accounts(user_id)
            record = accounts.get(platform)
            if not isinstance(record, dict):
                yield event.plain_result(
                    f"你还没有绑定{platform_label(platform)}账号\n"
                    f"发送：绑定{platform}<账号>"
                )
                return
            identifier = str(
                record.get("platform_user_id") or record.get("handle") or ""
            )
            try:
                profile = await self.account_fetcher.get_profile(
                    platform,
                    identifier,
                    detail=True,
                    force=force,
                    include_submissions=False,
                )
                await self._record_profile_metric(user_id, profile)
                delta = await self._profile_weekly_delta(user_id, profile)
                image_path = await asyncio.to_thread(
                    self.account_card_renderer.render_profile,
                    [profile],
                    display_name=self._event_display_name(event),
                    weekly_changes={platform: delta},
                )
                if image_path is not None and image_path.is_file():
                    yield event.image_result(str(image_path))
                else:
                    async for result in self._adaptive_results(
                        event,
                        self._format_account_text(
                            [profile], [], {platform: delta},
                            title=f"📊 {platform_label(platform)}战绩",
                        ),
                    ):
                        yield result
            except Exception as exc:
                yield event.plain_result(self._account_error_text(platform, exc))
            return

        accounts, profiles, errors = await self._load_bound_profiles(
            user_id, detail=True, force=force
        )
        if not accounts:
            yield event.plain_result(
                "你还没有绑定竞赛平台账号。\n" + self._account_platform_help()
            )
            return
        if not profiles:
            message = self._format_account_text(
                profiles,
                errors,
                title="📊 我的竞赛战绩",
            )
            yield event.plain_result(
                message
                or "暂时无法读取已绑定账号资料，请稍后重试"
            )
            return
        group_id = str(event.get_group_id() or "").strip()
        if group_id:
            await self.account_registry.set_group_member(
                group_id,
                user_id,
                True,
                preserve_opt_out=True,
            )
        weekly = {
            profile.platform: await self._profile_weekly_delta(user_id, profile)
            for profile in profiles
        }
        image_path = await asyncio.to_thread(
            self.account_card_renderer.render_profile,
            profiles,
            display_name=self._event_display_name(event),
            weekly_changes=weekly,
        )
        if image_path is not None and image_path.is_file():
            yield event.image_result(str(image_path))
            if errors:
                yield event.plain_result(
                    "⚠️ 部分平台同步失败："
                    + "、".join(platform_label(p) for p, _ in errors)
                )
            return
        async for result in self._adaptive_results(
            event,
            self._format_account_text(profiles, errors, weekly),
        ):
            yield result

    async def _collect_rank_rows(
        self,
        group_id: str,
        platform: str,
        *,
        progress: bool = False,
    ):
        member_ids = await self.account_registry.get_group_member_ids(group_id)
        accounts = await self.account_registry.get_all_accounts()
        records = []
        for user_id in member_ids:
            record = accounts.get(user_id, {}).get(platform)
            if not isinstance(record, dict):
                continue
            identifier = str(
                record.get("platform_user_id") or record.get("handle") or ""
            )
            normalized = normalize_account_identifier(platform, identifier)
            if not normalized:
                continue
            records.append((user_id, record, normalized))

        resolved = []
        bulk_getter = getattr(self.account_fetcher, "get_profiles", None)
        if (
            platform == "codeforces"
            and not progress
            and records
            and callable(bulk_getter)
        ):
            try:
                profiles = await bulk_getter(
                    platform,
                    [identifier for _, _, identifier in records],
                )
                resolved = [
                    (
                        user_id,
                        record,
                        profiles.get(identifier.casefold())
                        or AccountFetchError("未找到该 Codeforces 用户"),
                    )
                    for user_id, record, identifier in records
                ]
            except Exception as exc:
                # 一个失效的 CF 账号不应让整个群排行失效，退回逐账号查询。
                logger.warning("Codeforces 批量读取失败，改为逐账号读取：%s", exc)

        tasks = []
        if not resolved:
            for user_id, record, identifier in records:
                if not identifier:
                    continue
                tasks.append(
                    (
                        user_id,
                        record,
                        asyncio.create_task(
                            self.account_fetcher.get_profile(
                                platform,
                                identifier,
                                detail=progress,
                                include_submissions=False,
                            )
                        ),
                    )
                )
            results = await asyncio.gather(
                *(task for _, _, task in tasks),
                return_exceptions=True,
            ) if tasks else []
            resolved = [
                (item[0], item[1], result)
                for item, result in zip(tasks, results)
            ]

        rows = []
        errors = []
        for user_id, record, result in resolved:
            if isinstance(result, Exception):
                errors.append((user_id, result))
                continue
            await self._record_profile_metric(user_id, result)
            delta = await self._profile_weekly_delta(user_id, result)
            metric = self._profile_metric(result)
            if metric is None:
                continue
            if progress:
                value = delta
                display_value = _format_signed_number(delta)
                sort_value = delta
                metric_label = "本周变化"
            else:
                value = metric["value"]
                display_value = metric["display_value"]
                sort_value = metric["sort_value"]
                metric_label = metric["metric_label"]
            if value is None:
                continue
            rows.append(
                {
                    "user_id": user_id,
                    "display_name": str(
                        record.get("qq_name")
                        or record.get("display_name")
                        or user_id
                    ),
                    "handle": result.handle,
                    "value": value,
                    "display_value": display_value,
                    "metric_label": metric_label,
                    "sort_value": sort_value,
                    "delta": delta,
                    "rating": result.rating,
                }
            )
        rows.sort(
            key=lambda row: (
                -(
                    int(row["sort_value"])
                    if isinstance(row["sort_value"], int)
                    else 0
                ),
                str(row.get("display_name") or "").casefold(),
            )
        )
        return rows, errors

    async def _reply_set_rank_membership(
        self, event: AstrMessageEvent, enabled: bool
    ):
        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            yield event.plain_result("群排行设置只能在群聊中使用")
            return
        user_id = str(event.get_sender_id() or "").strip()
        await self.account_registry.set_user_display_name(
            user_id, self._event_display_name(event)
        )
        accounts = await self.account_registry.get_user_accounts(user_id)
        if enabled and not accounts:
            yield event.plain_result(
                "你还没有绑定竞赛平台账号，绑定后会自动加入群排行"
            )
            return
        await self.account_registry.set_group_member(
            group_id, user_id, enabled
        )
        if enabled:
            yield event.plain_result(
                "✅ 已加入本群排行；已绑定的平台会出现在对应榜单中"
            )
        else:
            yield event.plain_result("✅ 已退出本群排行")

    async def _reply_group_rank(
        self,
        event: AstrMessageEvent,
        mode: Optional[str],
    ):
        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            yield event.plain_result("群排行只能在群聊中使用")
            return

        platforms = (
            [mode]
            if mode in ACCOUNT_PLATFORMS
            else list(ACCOUNT_PLATFORMS)
        )
        progress = mode == "progress"
        sections = {}
        errors = []
        for platform in platforms:
            rows, row_errors = await self._collect_rank_rows(
                group_id, platform, progress=progress
            )
            sections[platform] = rows
            errors.extend((platform, error) for _, error in row_errors)

        if mode in ACCOUNT_PLATFORMS:
            rows = sections.get(mode, [])
            if not rows:
                yield event.plain_result(
                    f"当前群还没有加入{platform_label(mode)}排行的成员"
                )
                return
            title = f"本群 {platform_label(mode)} 排行"
            metric = (
                rows[0].get("metric_label")
                if rows
                else "Rating"
            ) or "Rating"
            note = (
                "洛谷没有公开 Elo 时按平台公开排名排序"
                if mode == "luogu"
                else ""
            )
            image_path = await asyncio.to_thread(
                self.account_card_renderer.render_ranking,
                rows[:MAX_CONTEST_LIST],
                title=title,
                subtitle=f"共 {len(rows)} 名成员 · 公开资料排行",
                metric_label=metric,
                note=note,
            )
            fallback = "\n".join(
                [
                    title,
                    *(
                        f"{i}. {row['display_name']}（{row['handle']}）"
                        f" {row.get('display_value', row['value'])}"
                        for i, row in enumerate(rows[:MAX_CONTEST_LIST], 1)
                    ),
                ]
            )
        else:
            if not any(sections.values()):
                yield event.plain_result("当前群还没有加入排行的成员")
                return
            title = "本群竞赛排行总览" if not progress else "本群本周进步榜"
            metric = "Rating" if not progress else "本周变化"
            note = (
                "各平台分开排行，不直接比较不同平台 Rating"
                if not progress
                else "暂无完整一周快照的成员会暂不计入"
            )
            image_path = await asyncio.to_thread(
                self.account_card_renderer.render_overview_ranking,
                sections,
                title=title,
                subtitle="四平台公开战绩矩阵",
                metric_label=metric,
                note=note,
            )
            fallback_lines = [title]
            for platform, rows in sections.items():
                fallback_lines.append(f"【{platform_label(platform)}】")
                for i, row in enumerate(rows[:5], 1):
                    value = (
                        row.get("display_value", row["value"])
                    )
                    fallback_lines.append(
                        f"{i}. {row['display_name']}（{row['handle']}） {value}"
                    )
            fallback = "\n".join(fallback_lines)
        if image_path is not None and image_path.is_file():
            yield event.image_result(str(image_path))
        else:
            async for result in self._adaptive_results(event, fallback):
                yield result
        if errors:
            yield event.plain_result(
                "⚠️ 部分账号同步失败，排行可能不完整："
                + "、".join(platform_label(p) for p, _ in errors)
            )

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
                logger.info(
                    "已向群 %s 提交 @全体成员 标记（QQ 官方群聊实际不生效，"
                    "仅兼容尝试）",
                    group.group_id,
                )
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
            value = str(text or "").strip()
            await self.get_settings()
            should_render_as_image = self.output_renderer.needs_image(value)
            chain = MessageChain([Plain(value)])
            rendered_as_image = False
            if should_render_as_image:
                # @everyone 必须保留为独立文本组件，避免被绘制进图片后失去
                # 平台识别机会；其余长内容作为图片发送。
                mention_prefix = "<@everyone>\n"
                render_value = value
                components = []
                if value.startswith(mention_prefix):
                    components.append(Plain(mention_prefix))
                    render_value = value[len(mention_prefix) :].strip()
                try:
                    image_path = await asyncio.to_thread(
                        self.output_renderer.render, render_value
                    )
                    if image_path is not None and image_path.is_file():
                        components.append(Image.fromFileSystem(str(image_path)))
                        chain = MessageChain(components)
                        rendered_as_image = True
                except Exception as exc:  # noqa: BLE001 - 长推送必须有文字兜底
                    logger.warning("群 %s 长通知转图片失败：%s", group_id, exc)

            if should_render_as_image and not rendered_as_image:
                # 转图不可用时按安全长度拆分，避免把超长原文直接交给 QQ。
                ok = await self._send_text_chunks(session, value)
            else:
                try:
                    ok = await self.context.send_message(session, chain)
                except Exception as exc:
                    if not rendered_as_image:
                        raise
                    logger.warning(
                        "群 %s 图片通知发送异常，回退为文字：%s", group_id, exc
                    )
                    ok = False

            if not ok and rendered_as_image:
                # 个别适配器可能不接受本地图片组件；回退为原始文字，
                # 保证主动推送仍然有可见结果。
                logger.warning("群 %s 图片通知发送失败，回退为文字", group_id)
                ok = await self._send_text_chunks(session, value)
            if not ok:
                logger.warning("发送到群 %s 失败：未找到匹配平台", group_id)
                return False
            return True
        except Exception as exc:
            logger.error("发送到群 %s 失败: %s", group_id, exc)
            return False

    async def _send_text_chunks(self, session, text: str) -> bool:
        """按安全长度发送文字分片，返回所有分片是否发送成功。"""
        for piece in text_chunks(text):
            if not await self.context.send_message(
                session, MessageChain([Plain(piece)])
            ):
                return False
        return True

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

    async def build_test_text(self, group: GroupConfig) -> str:
        """测试推送内容：优先今日早报，今日无比赛时展示最近一场。"""
        morning = await self.build_morning_text(group)
        if morning:
            return morning
        settings = await self.get_settings()
        platforms = [
            p for p in settings["push_platforms"] if p in group.push_platforms
        ] or list(DEFAULT_PLATFORMS)
        best = None
        for platform in platforms:
            contests, err = await self.fetcher.fetch_platform(platform)
            if err or not contests:
                continue
            for contest in contests:
                if not contest.is_upcoming():
                    continue
                if best is None or contest.start_time < best.start_time:
                    best = contest
        lines = ["🧪 测试推送（今日无比赛，展示最近一场）"]
        if best is not None:
            lines.append(best.format_detail())
        else:
            lines.append("（当前没有查到未开始的比赛）")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 指令：比赛查询
    # ------------------------------------------------------------------
    async def _adaptive_results(
        self, event: AstrMessageEvent, text: str
    ):
        """短结果发文字，长结果转 PNG；渲染失败时拆分为多条文字。"""
        value = str(text or "").strip()
        if not value:
            return
        await self.get_settings()
        if not self.output_renderer.needs_image(value):
            yield event.plain_result(value)
            return

        # HTML/浏览器调用是阻塞操作，放到线程中，避免卡住 AstrBot 事件循环。
        try:
            image_path = await asyncio.to_thread(self.output_renderer.render, value)
        except Exception as exc:  # noqa: BLE001 - 转图失败时必须保证文字兜底
            logger.error("acmerQQ群机器人 长消息转图片异常：%s", exc, exc_info=True)
            image_path = None
        if image_path is not None and image_path.is_file():
            logger.info(
                "acmerQQ群机器人 长消息已转图片发送（%d 字符，%d 行）",
                len(value),
                len(value.splitlines()),
            )
            yield event.image_result(str(image_path))
            return

        logger.warning(
            "acmerQQ群机器人 长消息转图片失败，改用纯文本分片（%d 字符）",
            len(value),
        )
        for piece in text_chunks(value):
            yield event.plain_result(piece)

    async def _reply_platform(
        self, event: AstrMessageEvent, platform: str, mode: str = "all"
    ):
        label = PLATFORM_LABELS.get(platform, platform)
        contests, err = await self.fetcher.fetch_platform(platform)
        if err:
            async for result in self._adaptive_results(event, err):
                yield result
            return
        upcoming = [c for c in contests if c.is_upcoming()]
        if not upcoming:
            async for result in self._adaptive_results(
                event, f"{label} 近期暂无比赛"
            ):
                yield result
            return
        if mode == "nearest":
            async for result in self._adaptive_results(
                event, upcoming[0].format_detail()
            ):
                yield result
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
        async for result in self._adaptive_results(event, "\n".join(lines)):
            yield result

    async def _reply_offline(self, event: AstrMessageEvent):
        """查询 XCPC Link 线下赛程，并明确展示数据源。"""
        if not hasattr(self.fetcher, "_fetch_offline"):
            async for result in self._adaptive_results(
                event,
                "⚠️ 线下赛功能文件未完整更新，请在 AstrBot 中完整重装本插件后重试",
            ):
                yield result
            return
        contests, err = await self.fetcher.fetch_platform(OFFLINE_PLATFORM)
        source_text = self.fetcher.source_text(OFFLINE_PLATFORM)
        if err:
            async for result in self._adaptive_results(
                event, f"{err}\n📚 数据源：XCPC Link（{source_text}）"
            ):
                yield result
            return
        upcoming = [contest for contest in contests if contest.is_upcoming()]
        if not upcoming:
            async for result in self._adaptive_results(
                event,
                "🏟 线下赛近期暂无已收录赛事\n"
                f"📚 数据源：XCPC Link（{source_text}）",
            ):
                yield result
            return

        lines = [
            f"🏟 线下赛（共 {len(upcoming)} 场）",
            f"📚 数据源：XCPC Link（{source_text}）",
        ]
        for index, contest in enumerate(upcoming[:MAX_CONTEST_LIST], start=1):
            lines.append(f"{index}. {contest.name}")
            lines.append(f"   日期：{contest.date_text()}")
            if contest.venue:
                lines.append(f"   赛站/地点：{contest.venue}")
            if contest.organizer:
                lines.append(f"   主办方：{contest.organizer}")
            if contest.official_url:
                lines.append(f"   官方通知：{contest.official_url}")
        if len(upcoming) > MAX_CONTEST_LIST:
            lines.append(f"…共 {len(upcoming)} 场，仅显示前 {MAX_CONTEST_LIST} 场")
        async for result in self._adaptive_results(event, "\n".join(lines)):
            yield result

    @staticmethod
    def _format_recent_contest(
        index: int, platform: str, contest: object
    ) -> List[str]:
        """格式化跨平台近期比赛的一条记录。"""
        label = PLATFORM_LABELS.get(platform, platform)
        name = str(getattr(contest, "name", "") or "未命名比赛")
        lines = [f"{index}. [{label}] {name}"]

        date_text = getattr(contest, "date_text", None)
        if callable(date_text):
            try:
                lines.append(f"   日期：{date_text()}")
            except Exception:
                pass
        else:
            start = contest_start_utc(contest)
            if start is not None:
                lines.append(
                    f"   时间：{start.astimezone(CN_TZ):%Y-%m-%d %H:%M}"
                    "（北京时间）"
                )
            duration = getattr(contest, "duration_minutes", 0) or 0
            try:
                duration = int(duration)
            except (TypeError, ValueError):
                duration = 0
            if duration > 0:
                lines.append(f"   时长：{duration} 分钟")
            url = str(getattr(contest, "url", "") or "").strip()
            if url:
                lines.append(f"   链接：{url}")

        venue = str(getattr(contest, "venue", "") or "").strip()
        if venue:
            lines.append(f"   赛站/地点：{venue}")
        organizer = str(getattr(contest, "organizer", "") or "").strip()
        if organizer:
            lines.append(f"   主办方：{organizer}")
        official_url = str(getattr(contest, "official_url", "") or "").strip()
        if official_url:
            lines.append(f"   官方通知：{official_url}")
        return lines

    async def _reply_recent_all(self, event: AstrMessageEvent):
        """汇总所有平台未来指定天数内开赛或仍在进行的比赛。"""
        settings = await self.get_settings()
        days = settings["recent_contest_days"]
        now = datetime.now(timezone.utc)
        platforms = [
            platform
            for platform in QUERY_PLATFORMS
            if platform != OFFLINE_PLATFORM
            or hasattr(self.fetcher, "_fetch_offline")
        ]

        async def fetch_one(platform: str):
            try:
                result = await self.fetcher.fetch_platform(platform)
                if (
                    not isinstance(result, tuple)
                    or len(result) != 2
                    or not isinstance(result[0], list)
                ):
                    raise ValueError("抓取结果格式异常")
                return result
            except Exception as exc:  # noqa: BLE001 - 单个平台失败不影响汇总
                label = PLATFORM_LABELS.get(platform, platform)
                logger.warning("汇总近期比赛获取%s失败：%s", label, exc)
                return [], f"获取失败：{exc}"

        results = await asyncio.gather(
            *(fetch_one(platform) for platform in platforms)
        )
        entries = []
        errors = []
        offline_included = OFFLINE_PLATFORM in platforms
        for platform, (contests, error) in zip(platforms, results):
            label = PLATFORM_LABELS.get(platform, platform)
            if error:
                errors.append(f"{label}：{error}")
            for contest in contests:
                start = contest_start_utc(contest)
                if start is None or not is_contest_in_recent_window(
                    contest, now, days
                ):
                    continue
                entries.append((start, platform, contest))

        entries.sort(
            key=lambda item: (
                item[0],
                PLATFORM_LABELS.get(item[1], item[1]),
                str(getattr(item[2], "name", "") or ""),
            )
        )
        lines = [
            f"📅 最近比赛（未来 {days} 天内及进行中，共 {len(entries)} 场）"
        ]
        if offline_included:
            source_getter = getattr(self.fetcher, "source_text", None)
            try:
                source = (
                    source_getter(OFFLINE_PLATFORM)
                    if callable(source_getter)
                    else ""
                )
            except Exception:
                source = ""
            source = source or "https://www.xcpc.link/（备用：https://www.xcpc.ink/）"
            lines.append(f"📚 线下赛数据源：XCPC Link（{source}）")
        if errors:
            lines.append("⚠️ 部分平台获取失败：" + "；".join(errors))
        if not entries:
            lines.append("（当前时间范围内暂无比赛）")
        else:
            for index, (_, platform, contest) in enumerate(
                entries[:MAX_CONTEST_LIST], start=1
            ):
                lines.extend(self._format_recent_contest(index, platform, contest))
        if len(entries) > MAX_CONTEST_LIST:
            lines.append(f"…共 {len(entries)} 场，仅显示前 {MAX_CONTEST_LIST} 场")
        async for result in self._adaptive_results(event, "\n".join(lines)):
            yield result

    async def _update(self, event: AstrMessageEvent):
        if not await self._is_admin(event):
            async for result in self._adaptive_results(event, "此指令仅限管理员"):
                yield result
            return
        parts = ["🔄 比赛数据刷新完成"]
        platforms = list(QUERY_PLATFORMS)
        if not hasattr(self.fetcher, "_fetch_offline"):
            platforms.remove(OFFLINE_PLATFORM)
            parts.append("线下赛：插件文件未完整更新，请完整重装后再刷新")
        for platform in platforms:
            contests, err = await self.fetcher.fetch_platform(platform, force=True)
            label = PLATFORM_LABELS.get(platform, platform)
            if err:
                parts.append(f"{label}：{err}")
            else:
                parts.append(
                    f"{label}：{len([c for c in contests if c.is_upcoming()])} 场"
                )
        async for result in self._adaptive_results(event, "\n".join(parts)):
            yield result

    @filter.platform_adapter_type(filter.PlatformAdapterType.QQOFFICIAL)
    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE
        | filter.EventMessageType.PRIVATE_MESSAGE
    )
    async def on_message(self, event: AstrMessageEvent):
        """全匹配指令分发：无需 @机器人 也能直接触发，且不会误伤聊天内容。"""
        raw_message = str(event.message_str or "").strip()
        # QQ 官方指令面板可能自动补上“/”；统一去掉一个前缀后再匹配。
        if raw_message.startswith("/"):
            raw_message = raw_message[1:].lstrip()
        message_str = normalize_command(raw_message)
        if not message_str:
            return
        bind_match = ACCOUNT_BIND_RE.match(raw_message)
        if bind_match:
            platform = normalize_platform(bind_match.group(1))
            if platform:
                async for result in self._reply_account_bind(
                    event, platform, bind_match.group(2)
                ):
                    yield result
            return
        confirm_match = ACCOUNT_CONFIRM_RE.match(raw_message)
        if confirm_match:
            platform = normalize_platform(confirm_match.group(1))
            if platform:
                async for result in self._reply_account_confirm(
                    event, platform, confirm_match.group(2) or ""
                ):
                    yield result
            return
        unbind_match = ACCOUNT_UNBIND_RE.match(raw_message)
        if unbind_match:
            platform = normalize_platform(unbind_match.group(1))
            if platform:
                async for result in self._reply_account_unbind(event, platform):
                    yield result
            return
        if message_str in MY_PLATFORM_COMMANDS:
            async for result in self._reply_my_account(
                event, platform=MY_PLATFORM_COMMANDS[message_str]
            ):
                yield result
            return
        if message_str in MY_ACCOUNT_COMMANDS:
            async for result in self._reply_my_account(
                event, force=message_str == normalize_command("刷新我的战绩")
            ):
                yield result
            return
        if message_str in JOIN_RANK_COMMANDS:
            async for result in self._reply_set_rank_membership(event, True):
                yield result
            return
        if message_str in LEAVE_RANK_COMMANDS:
            async for result in self._reply_set_rank_membership(event, False):
                yield result
            return
        if message_str in GROUP_RANK_COMMANDS:
            async for result in self._reply_group_rank(
                event, GROUP_RANK_COMMANDS[message_str]
            ):
                yield result
            return
        if message_str in MENU_COMMANDS:
            async for result in self._adaptive_results(event, MENU_TEXT):
                yield result
            return
        if message_str in RECENT_ALL_COMMANDS:
            async for result in self._reply_recent_all(event):
                yield result
            return
        if message_str == ACTIVATE_COMMAND:
            async for result in self._activate_group(event):
                yield result
            return
        query = QUERY_COMMANDS.get(message_str)
        if query is not None:
            platform, mode = query
            async for result in self._reply_platform(event, platform, mode):
                yield result
            return
        if message_str in OFFLINE_COMMANDS:
            async for result in self._reply_offline(event):
                yield result
            return
        if message_str in UPDATE_COMMANDS:
            async for result in self._update(event):
                yield result
            return

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
                current = await self.get_settings()
                morning = settings.get(
                    "morning_push_time", current["morning_push_time"]
                )
                morning = validate_hhmm(morning)
                raw_platforms = settings.get(
                    "push_platforms", current["push_platforms"]
                )
                platforms = raw_platforms or list(DEFAULT_PLATFORMS)
                if not isinstance(platforms, list):
                    raise ValueError("push_platforms 必须是列表")
                platforms = [p for p in DEFAULT_PLATFORMS if p in platforms]
                max_plain_text_chars = self._validate_bounded_int(
                    settings.get(
                        "max_plain_text_chars", current["max_plain_text_chars"]
                    ),
                    "文字转图片最大字符数",
                    MIN_MAX_PLAIN_TEXT_CHARS,
                    MAX_MAX_PLAIN_TEXT_CHARS,
                )
                max_plain_text_lines = self._validate_bounded_int(
                    settings.get(
                        "max_plain_text_lines", current["max_plain_text_lines"]
                    ),
                    "文字转图片最大行数",
                    MIN_MAX_PLAIN_TEXT_LINES,
                    MAX_MAX_PLAIN_TEXT_LINES,
                )
                recent_contest_days = self._validate_bounded_int(
                    settings.get(
                        "recent_contest_days", current["recent_contest_days"]
                    ),
                    "最近比赛查询天数",
                    MIN_RECENT_CONTEST_DAYS,
                    MAX_RECENT_CONTEST_DAYS,
                )
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
                        "max_plain_text_chars": max_plain_text_chars,
                        "max_plain_text_lines": max_plain_text_lines,
                        "recent_contest_days": recent_contest_days,
                    },
                )
                # 保存成功后立即更新当前实例，无需等待下一次消息或重启插件。
                self._configure_output_renderer(
                    {
                        "max_plain_text_chars": max_plain_text_chars,
                        "max_plain_text_lines": max_plain_text_lines,
                    }
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
        text = await self.build_test_text(group)
        sent = await self.send_notification(group, text)
        if not sent:
            return error_response("发送失败，请查看 AstrBot 日志")
        return json_response({"status": "success", "data": {"message": "测试推送已发送"}})
