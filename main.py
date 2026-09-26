"""acmerQQ群机器人：ACM 竞赛信息查询与定时推送插件。

功能菜单：发送 “acmer群管理插件菜单” 查看全部指令与所需权限。
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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

from . import platform_compat

from .src.contest_fetcher import (
    NOWCODER_SCOPE_ALL,
    NOWCODER_SCOPE_SERIES_ONLY,
    NOWCODER_SCOPES,
    ContestFetcher,
)
from .src.plugin_paths import migrate_known_caches, plugin_cache_dir
from .src.rank_service import RankService
from .src.group_names import event_group_name, fetch_group_name
from .src.settlement import SETTLE_PLATFORMS, SettlementService
from .src.problem_service import ProblemService, weak_tags_from_analysis
from .src.account_cards import (
    _platform_rank_text,
    AccountCardRenderer,
    current_metric_header,
    progress_metric_header,
    rank_metric_label_for_rows,
)
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
from .src.output_renderer import (
    MAX_TEXT_CHUNK,
    AdaptiveOutputRenderer,
    text_chunks,
)
from .src.scheduler import PushScheduler
from .src.weekly_stats import (
    WEEKLY_WINDOW_DAYS,
    collect_weekly_activity,
)
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
# 每日一题 / 推荐补题：题目池默认取牛客（题库索引已在 1.12.0 落地，零新增抓取）。
DEFAULT_DAILY_PROBLEM_PLATFORM = "nowcoder"
DAILY_PROBLEM_PLATFORMS = ("nowcoder", "codeforces", "atcoder", "luogu")
DEFAULT_DAILY_PROBLEM_COUNT = 1
MIN_DAILY_PROBLEM_COUNT = 1
MAX_DAILY_PROBLEM_COUNT = 3
RECOMMEND_PROBLEM_LIMIT = 3
# 赛后赛果推送：比赛结束后 delay 分钟一次性推送"名次版"（不含 Rating 变化——
# CF 要等系统重测、牛客固定次日 00:00 才更新评分，等不起；名次与通过题数
# 在赛后即可从平台公开数据拿到）。窗口用于"重启后补推"，超出即放弃。
DEFAULT_SETTLE_DELAY_MINUTES = 10
MIN_SETTLE_DELAY_MINUTES = 1
MAX_SETTLE_DELAY_MINUTES = 60
SETTLE_WINDOW_HOURS = 2
DEFAULT_SETTLE_MIN_PARTICIPANTS = 1
MIN_SETTLE_MIN_PARTICIPANTS = 1
MAX_SETTLE_MIN_PARTICIPANTS = 10
# 群训练周报（A3）：默认每周一 20:00 推一次，ISO 周幂等（同一周每群只推一次）。
DEFAULT_WEEKLY_REPORT_ENABLED = True
DEFAULT_WEEKLY_REPORT_WEEKDAY = 1
MIN_WEEKLY_REPORT_WEEKDAY = 1
MAX_WEEKLY_REPORT_WEEKDAY = 7
DEFAULT_WEEKLY_REPORT_TIME = "20:00"
# 报名截止提醒（A4）：牛客比赛报名截止前的两个档位（小时）。
# 幂等键是**全局键**（signup_<contestID>_<24h|2h>），报名截止对所有群一样，
# 避免每个群各推一次造成重复打扰。
SIGNUP_REMINDER_TIERS = (("24h", 24.0), ("2h", 2.0))
# 牛客赛事口径：all = 日历里全部牛客赛事（含高校校赛/新生赛同步赛/自主创建赛）；
# series_only = 仅比赛名含“牛客”的系列赛（旧口径）。抓取始终取全量，
# 只在返回时按口径过滤，因此切换后立即生效。
DEFAULT_NOWCODER_SCOPE = NOWCODER_SCOPE_ALL
NOWCODER_SCOPE_LABELS = {
    NOWCODER_SCOPE_ALL: "全部牛客赛事",
    NOWCODER_SCOPE_SERIES_ONLY: "仅牛客系列赛",
}
# @全体成员 尝试失败后，对该群暂缓重试的时间（秒）
AT_ALL_BLOCK_SECONDS = 6 * 3600
# 后台「运行状态」用的推送日志：KV 环形保留最近 N 条，避免无限增长。
PUSH_LOG_KEY = "push_log"
PUSH_LOG_MAX = 200
# 「立即试跑」支持的类型（早报 / 训练周报 / 赛后赛果 / 报名提醒）。
RUN_NOW_KINDS = ("morning", "weekly", "settle", "signup")
# 比赛列表最多展示的条数（防止消息过长）
MAX_CONTEST_LIST = 30
# 群排行按平台分页，避免 2000 人群一次生成超长图片/消息。
RANK_PAGE_SIZE = 30
RANK_OVERVIEW_SIZE = 5
RANK_CACHE_TTL = 5 * 60
RANK_CACHE_MAX_ENTRIES = 64
RANK_FETCH_BATCH_SIZE = 100
RANK_FETCH_CONCURRENCY = 8
# 每日早报里附带的周榜，每个平台只列前几名，保持推送可读。
WEEKLY_BOARD_PUSH_SIZE = 3
# 本周进步榜的差值优先用本地 Rating 历史计算（一次批量查询，零网络）。
# 仅当本地缺少 7 天前基线时，才对“限量”成员回退拉取详细资料；
# 这样首次计算的网络开销被限制在 K × (平台限速) 而非全群逐个拉取。
PROGRESS_DETAIL_FALLBACK_LIMIT = 8
# 配置/群快照的内存缓存时长：普通查询不需要每次都读 AstrBot KV；
# WebUI 保存成功后会主动失效，外部直接改 KV 最多延迟该时长后生效。
SETTINGS_CACHE_TTL_SECONDS = 2.0
GROUPS_CACHE_TTL_SECONDS = 30.0
# 渲染并发上限：4C4G 上避免长消息/资料卡/排行卡同时起多个 Chromium/Pillow。
RENDER_MAX_CONCURRENCY = 1
#: 适配器拿不到真实用户 ID 时会塞的占位符（不是真实成员，不能当作被 @ 目标）。
#: 典型：QQ 官方群消息里机器人自己被 @ 时，self_id / At 里是 "qq_official"。
PLACEHOLDER_USER_IDS = {
    "",
    "qq_official",
    "qq_official_webhook",
    "unknown_selfid",
    "unknown",
    "self",
    "bot",
    "all",
    "everyone",
}
#: 任意形态的 @ 记号（<@id> / <@!id> / CQ at / [At:...]），用于把残留的
#: 机器人占位符 @（如 <@qq_official>）一并清掉，保证指令本体可被匹配。
ANY_MENTION_TOKEN_RE = re.compile(
    r"<@!?[^>]+>|\[CQ:at,qq=[^\]]+\]|\[At:[^\]]+\]",
    re.I,
)


def _format_signed_number(value: object) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{int(value):+d}"
    except (TypeError, ValueError):
        return str(value)


def _rank_fallback_row(index: int, row: dict, metric_header: str) -> str:
    """排行文字兜底的单行内容：账号 +（有数据时）平台排名 + 指标 + 变化。"""
    parts = [f"账号：{row['handle']}"]
    rank_text = _platform_rank_text(row)
    if rank_text:
        parts.append(f"平台排名 {rank_text}")
    parts.append(f"{metric_header}：{row.get('display_value', row['value'])}")
    parts.append(f"近7日变化：{_format_signed_number(row.get('delta'))}")
    return f"{index}. {row['display_name']}\n    " + " · ".join(parts)


def _solved_count_label(profile: object) -> str:
    extra = getattr(profile, "extra", {}) or {}
    if not isinstance(extra, dict):
        return "通过题数"
    try:
        if (
            extra.get("difficulty_scan_limit")
            and extra.get("difficulty_scanned_submissions")
            and int(extra["difficulty_scanned_submissions"])
            >= int(extra["difficulty_scan_limit"])
        ):
            return "已统计题数"
    except (TypeError, ValueError):
        pass
    return "通过题数"


ACCOUNT_BIND_RE = re.compile(
    r"^(?:绑定|bind)\s*(cf|codeforces|nk|牛客|nowcoder|lg|洛谷|luogu|"
    r"atc|atcoder)\s+(.+?)\s*$",
    re.I,
)
ACCOUNT_BIND_USAGE_RE = re.compile(
    r"^(?:绑定|bind)\s*(cf|codeforces|nk|牛客|nowcoder|lg|洛谷|luogu|"
    r"atc|atcoder)\s*$",
    re.I,
)
#: 管理员 @代绑定：@ 一个成员 + 绑定指令，跳过验证码直接写入目标名下。
#: 允许显式“代”前缀（@某人 代绑定cf xxx），也允许与自助绑定同词
#: （@某人 绑定cf xxx）——靠“恰好一个非机器人被 @ + 发送者是后台管理员”区分。
ADMIN_BIND_RE = re.compile(
    r"^(?:代\s*)?(?:绑定|bind)\s*(cf|codeforces|nk|牛客|nowcoder|lg|洛谷|luogu|"
    r"atc|atcoder)\s+(.+?)\s*$",
    re.I,
)
ADMIN_BIND_USAGE_RE = re.compile(
    r"^(?:代\s*)?(?:绑定|bind)\s*(cf|codeforces|nk|牛客|nowcoder|lg|洛谷|luogu|"
    r"atc|atcoder)\s*$",
    re.I,
)
ACCOUNT_BIND_USAGE_HINTS = {
    "codeforces": ("绑定cf", "<Codeforces用户名>", "姓氏（Last name）"),
    "nowcoder": ("绑定牛客", "<牛客用户ID>", "个性签名"),
    "luogu": ("绑定洛谷", "<洛谷UID>", "个人介绍"),
    "atcoder": ("绑定atcoder", "<AtCoder用户名>", "Affiliation（所属）"),
}
#: 绑定/查询指令示例：提示文案里给一个具体例子，降低"该怎么写"的门槛
ACCOUNT_BIND_EXAMPLES = {
    "codeforces": "绑定cf jiangly",
    "nowcoder": "绑定牛客 12345678",
    "luogu": "绑定洛谷 123456",
    "atcoder": "绑定atcoder tourist",
}
ACCOUNT_LOOKUP_EXAMPLES = {
    "codeforces": "查询cf jiangly",
    "nowcoder": "查询牛客 12345678",
    "luogu": "查询洛谷 123456",
    "atcoder": "查询atcoder tourist",
}
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
#: 只发「确认绑定」/「解绑」不带平台时的用法提示（菜单里就是这么列的，
#: 之前这两条完全没回复，等于菜单里的死项）
ACCOUNT_CONFIRM_USAGE_RE = re.compile(
    r"^(?:确认绑定|confirm\s*bind)\s*$", re.I
)
ACCOUNT_UNBIND_USAGE_RE = re.compile(r"^(?:解绑|unbind)\s*$", re.I)
# 未绑定用户战绩查询：查询cf <用户名/UID/主页链接>
# 平台别名与绑定指令保持同一套；标识由 normalize_account_identifier 归一化，
# 自动兼容用户名、数字 UID 与主页链接三种形态。
_ACCOUNT_LOOKUP_PLATFORM = (
    r"(cf|codeforces|nk|牛客|nowcoder|lg|洛谷|luogu|atc|atcoder)"
)
ACCOUNT_LOOKUP_RE = re.compile(
    rf"^(?:查询|查|lookup)\s*{_ACCOUNT_LOOKUP_PLATFORM}\s+(.+?)\s*$",
    re.I,
)
ACCOUNT_LOOKUP_USAGE_RE = re.compile(
    rf"^(?:查询|查|lookup)\s*{_ACCOUNT_LOOKUP_PLATFORM}\s*$",
    re.I,
)
# “详细”后缀：默认只出摘要卡，加后缀才抓难度分布/分析（更慢）。
ACCOUNT_LOOKUP_DETAIL_RE = re.compile(
    r"^(?P<target>.+?)\s+(?:详细|详情|detail)$",
    re.I,
)
ACCOUNT_LOOKUP_USAGE_HINTS = {
    "codeforces": "查询cf <Codeforces用户名>",
    "atcoder": "查询atcoder <AtCoder用户名>",
    "nowcoder": "查询牛客 <牛客数字UID>",
    "luogu": "查询洛谷 <洛谷数字UID>",
}
# 这两个平台只能按数字 UID / 主页链接查询（公开接口无“用户名→UID”）。
ACCOUNT_LOOKUP_UID_ONLY = {"nowcoder", "luogu"}
# 防滥用：每用户冷却 + 每群每分钟次数上限；命中资料缓存时同样受限，
# 但阈值足够宽松，正常使用不会触发。
LOOKUP_USER_COOLDOWN_SECONDS = 30
LOOKUP_GROUP_WINDOW_SECONDS = 60
LOOKUP_GROUP_MAX_PER_WINDOW = 10
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
#: @某人 时可用于"查这个人的资料卡"的说法。
#: 注意**不能**包含"我的战绩/我的账号"：这两个词永远指自己，而 QQ 群里几乎每条
#: 指令都会 @ 机器人，若把它们算作"查他人"，@机器人 发"我的战绩"就会被当成
#: 查询机器人自己的资料卡（线上真实故障：回"该成员还没有绑定竞赛平台账号"）。
MENTION_PROFILE_COMMANDS = {
    normalize_command("战绩"),
    normalize_command("查询战绩"),
    normalize_command("战绩卡"),
    normalize_command("资料卡"),
    normalize_command("账号"),
}
#: 永远指"自己"的说法：即使 @ 了别人也只查自己的卡
SELF_ONLY_PROFILE_COMMANDS = {
    normalize_command("我的战绩"),
    normalize_command("我的账号"),
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
    normalize_command("本周退步榜"): "regress",
    normalize_command("本周掉分榜"): "regress",
    normalize_command("退步榜"): "regress",
    normalize_command("群cf排行"): "codeforces",
    normalize_command("群codeforces排行"): "codeforces",
    normalize_command("群牛客排行"): "nowcoder",
    normalize_command("群nk排行"): "nowcoder",
    normalize_command("群洛谷排行"): "luogu",
    normalize_command("群lg排行"): "luogu",
    normalize_command("群atcoder排行"): "atcoder",
    normalize_command("群atc排行"): "atcoder",
}
RANK_PAGE_COMMANDS = {
    "codeforces": "群cf排行",
    "nowcoder": "群牛客排行",
    "luogu": "群洛谷排行",
    "atcoder": "群atcoder排行",
}

MENU_TEXT = (
    "🌸 PINK PEARL ACM 菜单\n"
    "╭──────────────╮\n"
    "👥 所有人可用\n"
    "• acmer激活 ─ 首次激活本群主动推送（重启后群内任意消息自动恢复）\n"
    "• 绑定cf/绑定牛客/绑定洛谷/绑定atcoder ─ 绑定个人竞赛账号"
    "（例如：绑定cf jiangly）\n"
    "• 确认绑定/解绑 ─ 完成或解除平台账号绑定\n"
    "• 我的战绩/我的账号 ─ 查看四平台个人战绩卡（群聊显示本群排行）\n"
    "• 我的cf/我的牛客/我的洛谷/我的atcoder ─ 查看单个平台战绩卡（群聊显示本群排行）\n"
    "• @某人 战绩 / @某人 查询战绩 ─ 查询该成员的竞赛战绩卡（群聊只读）\n"
    "• 查询cf/查询atcoder <用户名> ─ 查询未绑定用户战绩卡（牛客/洛谷用数字UID）\n"
    "• 群排行 ─ 查看四个平台排行总览\n"
    "• 群cf排行/群牛客排行/群洛谷排行/群atcoder排行 ─ 查看平台排行（每页30人，可加页码）\n"
    "• 本周进步榜 ─ 查看各平台本周 Rating 变化\n"
    "• 本周退步榜 ─ 查看各平台本周 Rating 下降最多的成员\n"
    "• 加入群排行/退出群排行 ─ 管理当前群的排行展示\n"
    "• 最近比赛 ─ 汇总所有平台未来 N 天内及进行中的比赛（N 可在 WebUI 设置）\n"
    "• 每日一题 ─ 查看今天的每日一题（与早报同源同一题）\n"
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
    "╰──────────────╯\n"
    "🌙 仅管理员\n"
    "• update / 刷新比赛 ─ 强制刷新全部比赛数据\n"
    "• @某人 绑定cf/牛客/洛谷/atcoder <账号> ─ 管理员代绑定"
    "（跳过验证码，仅后台管理员）\n"
    "╭──────────────╮\n"
    "提示：指令为全匹配，发送完整指令才会触发；也可带 / 前缀（如 /nk比赛）\n"
    "⚙️ 推送配置（早报/提醒/@全体/长消息转图）：WebUI acmerQQ群机器人 页\n"
    "🔗 开源：https://github.com/td1336065617/acmerQQ-group-bot\n"
    "╰──────────────╯"
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
DAILY_PROBLEM_COMMANDS = {
    normalize_command(command)
    for command in ("每日一题", "今日一题", "今日题目", "每日题目")
}
GROUP_RANK_PAGE_RE = re.compile(
    r"^(?P<command>.+?)(?:\s*第\s*)?(?P<page>\d+)\s*页?$",
    re.I,
)


def parse_group_rank_command(
    value: str,
) -> Optional[tuple[Optional[str], int]]:
    """解析群排行及其页码；不带页码时默认第一页。"""
    normalized = normalize_command(value)
    if normalized in GROUP_RANK_COMMANDS:
        return GROUP_RANK_COMMANDS[normalized], 1
    match = GROUP_RANK_PAGE_RE.fullmatch(normalized)
    if not match:
        return None
    command = normalize_command(match.group("command"))
    if command not in GROUP_RANK_COMMANDS:
        return None
    return GROUP_RANK_COMMANDS[command], int(match.group("page"))


class AcmerGroupBot(Star):
    def __init__(self, context: Context, config: Optional[dict] = None) -> None:
        super().__init__(context, config)
        self.config = config if isinstance(config, dict) else {}
        self.fetcher = ContestFetcher()
        self.account_fetcher = AccountFetcher()
        self.account_registry = AccountRegistry(self)
        self.account_card_renderer = AccountCardRenderer(
            cache_dir=plugin_cache_dir("account_cards")
        )
        self.output_renderer = AdaptiveOutputRenderer(
            cache_dir=plugin_cache_dir("output_cache")
        )
        self.scheduler = PushScheduler(self)
        self.rank_service = RankService(self)
        self.settlement = SettlementService(self.account_fetcher)
        self.problem_service = ProblemService(self)
        # 本次运行期间已收到过消息的群（用于自动重新激活日志）
        self._seen_group_this_run: set = set()
        # settings/groups 内存缓存：避免每次消息/回复/tick 都整读 KV。
        self._settings_cache: Optional[tuple[float, dict]] = None
        self._groups_cache: Optional[tuple[float, dict]] = None
        # 群排行结果短缓存：同一群多人同时查看时只执行一次资料汇总。
        self._rank_cache = {}
        self._rank_cache_locks = {}
        # 进程内“本次失效、尚未读快照”的群集合，避免 bind/unbind 后立即查询
        # 仍命中 60 分钟旧快照。
        self._rank_dirty_pending: set = set()
        self._rank_fetch_semaphore = asyncio.Semaphore(
            RANK_FETCH_CONCURRENCY
        )
        # 渲染全局并发上限：长消息/资料卡/排行卡共用，防多浏览器同时启动。
        self._render_semaphore = asyncio.Semaphore(RENDER_MAX_CONCURRENCY)
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
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/overview",
                self._web_overview,
                ["GET"],
                "后台概览与运行状态",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/push-log",
                self._web_push_log,
                ["GET"],
                "定时推送日志",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/groups/refresh-names",
                self._web_refresh_group_names,
                ["POST"],
                "刷新群名称",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/bindings",
                self._web_bindings_list,
                ["GET"],
                "账号绑定列表",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/bindings",
                self._web_bindings_write,
                ["POST"],
                "账号绑定增删改",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/rank",
                self._web_rank,
                ["GET"],
                "群排行只读查看",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/rank/members",
                self._web_rank_members,
                ["GET"],
                "群排行成员列表",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/rank/members",
                self._web_rank_members_write,
                ["POST"],
                "强制加入/移除群排行成员",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/run-now",
                self._web_run_now,
                ["POST"],
                "立即试跑定时推送",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/import",
                self._web_import,
                ["POST"],
                "导入后台配置",
            )
        except Exception as exc:
            logger.error("注册 Web API 失败: %s", exc)

    async def initialize(self) -> None:
        migrated = migrate_known_caches()
        if migrated:
            logger.info("已把 %d 项旧缓存迁移到插件数据目录", migrated)
        await self.fetcher.initialize()
        # 后台预热 CF 全站榜单：全量榜单约 115 秒，不能放在用户查询路径里等。
        async def _warm_cf_rank_list() -> None:
            try:
                ratings = await self.account_fetcher._codeforces_rated_ratings()
                logger.info("CF 全站榜单预热完成：%d 人", len(ratings))
            except Exception as exc:  # noqa: BLE001 - 预热失败不影响启动
                logger.warning("预热 CF 全站榜单失败：%s", exc)

        self._cf_warm_task = asyncio.create_task(_warm_cf_rank_list())
        # 重载/升级后作废旧排行快照：快照新鲜期 1 小时，不作废的话
        # 升级后最长要等一小时才能看到新的排行数据。
        try:
            store = getattr(self.account_registry, "store", None)
            marker = getattr(store, "mark_all_rank_dirty", None)
            if callable(marker):
                count = await marker()
                logger.info("启动时已作废旧排行快照：%d 条", count)
        except Exception as exc:  # noqa: BLE001 - 作废失败不影响启动
            logger.warning("作废旧排行快照失败：%s", exc)
        # 赛程接口只返回"未开始"的比赛，结束后会从列表消失；重启后要能继续
        # 结算"结束前已见过"的比赛，因此把最近记录从磁盘恢复。
        try:
            self.settlement.load_recent_contests()
        except Exception as exc:  # noqa: BLE001 - 记录损坏不影响启动
            logger.warning("加载最近比赛记录失败：%s", exc)
        # 存储后端：默认 SQLite（含 KV 自动迁移与双写）；设置环境变量
        # ACMER_STORE_BACKEND=kv 可回到纯 KV 模式用于回滚验证。
        backend = os.environ.get("ACMER_STORE_BACKEND", "sqlite").strip().lower()
        dual_raw = os.environ.get("ACMER_DUAL_WRITE_KV", "1").strip().lower()
        dual_write = dual_raw not in {"0", "false", "no", "off"}
        store_dir = os.environ.get("ACMER_STORE_DIR") or None
        await self.account_registry.initialize(
            enable=backend != "kv",
            db_path=store_dir,
            dual_write_kv=dual_write,
        )
        cache_store = (
            self.account_registry.store
            if self.account_registry.store_enabled
            else None
        )
        await self.account_fetcher.initialize(
            self.fetcher.session, cache_store=cache_store
        )
        # 启动时预热一次配置：确保渲染阈值（是否转图）在首个菜单/查询请求前
        # 就是管理员配置的值，而不是渲染器默认值。
        try:
            await self.get_settings()
        except Exception as exc:  # noqa: BLE001 - 配置读取失败不影响启动
            logger.warning("启动预读配置失败: %s", exc)
        await self.scheduler.start()
        logger.info(
            "acmerQQ群机器人 已启动；若消息指令无响应，请检查 AstrBot "
            "设置→插件配置→可用插件（plugin_set）是否包含本插件（或设为全部）"
        )

    async def terminate(self) -> None:
        await self.scheduler.stop()
        rank_service = getattr(self, "rank_service", None)
        if rank_service is not None:
            closer = getattr(rank_service, "close", None)
            if callable(closer):
                try:
                    await closer()
                except Exception:  # noqa: BLE001
                    logger.warning("关闭排行后台任务失败", exc_info=True)
        flush = getattr(self.account_fetcher, "flush_persistent_cache", None)
        if callable(flush):
            try:
                await flush()
            except Exception:  # noqa: BLE001 - 退出前尽力落盘
                logger.warning("退出前持久化缓存 flush 失败", exc_info=True)
        await self.account_registry.close()
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
        admins = await self._get_admins()
        sender_id = str(event.get_sender_id() or "")
        platform_id = str(event.get_platform_id() or "")
        return any(
            platform_compat.admin_entry_matches(entry, platform_id, sender_id)
            for entry in admins
        )

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

    def _apply_cached_renderer_settings(self) -> None:
        """在不能 await 的热路径上，用缓存里的 settings 同步刷新渲染阈值。

        `_adaptive_results` 必须在 yield 纯文本前避免 await（否则被动回复会丢
        换行），因此不能在它内部调用 `get_settings()`。这里改为读取已有的
        settings 缓存来应用阈值，保证 needs_image 判断始终用管理员的配置值，
        而不是渲染器的默认值。
        """
        cached = getattr(self, "_settings_cache", None)
        if not cached:
            return
        try:
            self._configure_output_renderer(cached[1])
        except Exception as exc:  # noqa: BLE001 - 阈值应用失败不影响本次回复
            logger.warning("应用渲染阈值失败: %s", exc)

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
        """读取全局设置；命中短 TTL 内存缓存时不再整读 AstrBot KV。

        返回的是副本，调用方修改返回字典不会污染缓存。
        """
        now = time.monotonic()
        cached = self._settings_cache
        if cached is not None and now - cached[0] < SETTINGS_CACHE_TTL_SECONDS:
            return self._copy_settings(cached[1])
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
            "nowcoder_scope": self._read_nowcoder_scope(
                raw.get("nowcoder_scope")
            ),
            "settle_push_enabled": bool(raw.get("settle_push_enabled", True)),
            "settle_delay_minutes": self._read_bounded_int(
                raw.get("settle_delay_minutes"),
                DEFAULT_SETTLE_DELAY_MINUTES,
                MIN_SETTLE_DELAY_MINUTES,
                MAX_SETTLE_DELAY_MINUTES,
            ),
            "settle_min_participants": self._read_bounded_int(
                raw.get("settle_min_participants"),
                DEFAULT_SETTLE_MIN_PARTICIPANTS,
                MIN_SETTLE_MIN_PARTICIPANTS,
                MAX_SETTLE_MIN_PARTICIPANTS,
            ),
            "settle_show_unsolved": bool(raw.get("settle_show_unsolved", True)),
            "daily_problem_enabled": bool(raw.get("daily_problem_enabled", True)),
            "daily_problem_platform": self._read_daily_problem_platform(
                raw.get("daily_problem_platform")
            ),
            "daily_problem_count": self._read_bounded_int(
                raw.get("daily_problem_count"),
                DEFAULT_DAILY_PROBLEM_COUNT,
                MIN_DAILY_PROBLEM_COUNT,
                MAX_DAILY_PROBLEM_COUNT,
            ),
            "recommend_enabled": bool(raw.get("recommend_enabled", True)),
            "weekly_report_enabled": bool(
                raw.get("weekly_report_enabled", DEFAULT_WEEKLY_REPORT_ENABLED)
            ),
            "weekly_report_weekday": self._read_bounded_int(
                raw.get("weekly_report_weekday"),
                DEFAULT_WEEKLY_REPORT_WEEKDAY,
                MIN_WEEKLY_REPORT_WEEKDAY,
                MAX_WEEKLY_REPORT_WEEKDAY,
            ),
            "weekly_report_time": self._read_hhmm(
                raw.get("weekly_report_time"), DEFAULT_WEEKLY_REPORT_TIME
            ),
        }
        self._settings_cache = (time.monotonic(), settings)
        # 每次刷新配置时同步一次，兼容管理员从其他入口修改 KV 或热更新配置。
        self._configure_output_renderer(settings)
        self._configure_contest_fetcher(settings)
        return self._copy_settings(settings)

    @staticmethod
    def _read_nowcoder_scope(value: object) -> str:
        """校验牛客赛事口径，非法值回退到默认（全部牛客赛事）。"""
        scope = str(value or "").strip().lower()
        return scope if scope in NOWCODER_SCOPES else DEFAULT_NOWCODER_SCOPE

    @staticmethod
    def _read_daily_problem_platform(value: object) -> str:
        platform = str(value or "").strip().lower()
        if platform in DAILY_PROBLEM_PLATFORMS:
            return platform
        return DEFAULT_DAILY_PROBLEM_PLATFORM

    @staticmethod
    def _read_hhmm(value: object, default: str) -> str:
        """校验 HH:MM 时间，非法值回退默认（读取旧配置时的容错）。"""
        try:
            return validate_hhmm(str(value or default))
        except ValueError:
            return default

    def _configure_contest_fetcher(self, settings: dict) -> None:
        """把抓取相关设置同步给 ContestFetcher（牛客口径切换立即生效）。"""
        fetcher = getattr(self, "fetcher", None)
        if fetcher is None:
            return
        scope = self._read_nowcoder_scope(settings.get("nowcoder_scope"))
        if getattr(fetcher, "nowcoder_scope", None) != scope:
            fetcher.nowcoder_scope = scope
            logger.info(
                "牛客赛事口径切换为：%s（%s）",
                scope,
                NOWCODER_SCOPE_LABELS.get(scope, scope),
            )

    @staticmethod
    def _copy_settings(settings: dict) -> dict:
        """返回 settings 的浅拷贝，列表成员也复制，避免调用方污染缓存。"""
        return {
            **settings,
            "push_platforms": list(settings.get("push_platforms") or []),
        }

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
    def _mention_user_id(mention: object) -> str:
        """兼容 QQ 官方 mentions 和 AstrBot At 组件的用户 ID 字段。"""
        for attr in (
            "member_openid",
            "user_openid",
            "user_id",
            "id",
            "qq",
        ):
            value = getattr(mention, attr, None)
            if value is None:
                continue
            text = str(value).strip()
            if text and text.casefold() not in {"all", "everyone"}:
                return text
        if isinstance(mention, dict):
            for key in (
                "member_openid",
                "user_openid",
                "user_id",
                "id",
                "qq",
            ):
                value = mention.get(key)
                if value is None:
                    continue
                text = str(value).strip()
                if text and text.casefold() not in {"all", "everyone"}:
                    return text
        return ""

    @staticmethod
    def _mention_display_name(mention: object) -> str:
        """读取被 @ 用户的可见昵称，缺失时交给调用方回退。"""
        for attr in (
            "nickname",
            "nick",
            "username",
            "name",
            "display_name",
        ):
            value = getattr(mention, attr, None)
            if value is None and isinstance(mention, dict):
                value = mention.get(attr)
            text = str(value or "").strip()
            if text:
                return text
        return ""

    @staticmethod
    def _is_placeholder_user(user_id: object) -> bool:
        """占位符（如 qq_official）不是真实成员，不能作为被 @ 目标。"""
        return str(user_id or "").strip().casefold() in PLACEHOLDER_USER_IDS

    @classmethod
    def _strip_any_mention_tokens(cls, text: str) -> str:
        """清掉任意形态的 @ 记号（含机器人占位符 @），只留下指令本体。"""
        cleaned = ANY_MENTION_TOKEN_RE.sub(" ", str(text or ""))
        return re.sub(r"\s+", " ", cleaned).strip()

    @classmethod
    def _collect_mention_targets(
        cls,
        event: AstrMessageEvent,
        raw_message: str = "",
    ) -> List[dict]:
        """收集群内被 @ 的成员（去重、排除机器人、排除 @全体）；私聊返回 []。"""
        if not str(event.get_group_id() or "").strip():
            return []

        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "raw_message", None)
        mentions = getattr(raw, "mentions", None)
        if isinstance(mentions, dict):
            mentions = list(mentions.values())
        elif not isinstance(mentions, (list, tuple)):
            mentions = []

        targets: List[dict] = []
        seen_ids = set()
        self_ids = cls._self_id_candidates(event, message_obj, raw)
        for mention in mentions:
            if bool(getattr(mention, "is_you", False)):
                continue
            user_id = cls._mention_user_id(mention)
            if not user_id or user_id in seen_ids:
                continue
            if cls._is_placeholder_user(user_id):
                continue
            if user_id.casefold() in self_ids:
                continue
            seen_ids.add(user_id)
            targets.append(
                {
                    "user_id": user_id,
                    "display_name": cls._mention_display_name(mention),
                }
            )

        # 某些适配器不把普通 At 放进 raw_message.mentions，兼容 AstrBot
        # 消息链和常见的 <@id>/CQ at 文本格式。
        if not targets:
            get_messages = getattr(event, "get_messages", None)
            components = get_messages() if callable(get_messages) else []
            for component in components or []:
                component_type = str(
                    getattr(component, "type", "")
                ).casefold()
                if (
                    component.__class__.__name__.casefold() != "at"
                    and component_type not in {"at", "componenttype.at"}
                ):
                    continue
                user_id = cls._mention_user_id(component)
                if not user_id or user_id in seen_ids:
                    continue
                # 官方通道的 @机器人 会以占位符出现（qq_official），必须排除
                if cls._is_placeholder_user(user_id):
                    continue
                # 消息链里的 @机器人 有时不带 is_you，需要靠自身 id 识别
                if bool(getattr(component, "is_you", False)):
                    continue
                if user_id.casefold() in self_ids:
                    continue
                seen_ids.add(user_id)
                targets.append(
                    {
                        "user_id": user_id,
                        "display_name": cls._mention_display_name(component),
                    }
                )

        if not targets:
            content = str(getattr(raw, "content", "") or "")
            content = content or str(raw_message or "")
            for user_id in re.findall(
                r"<@!?([^>]+)>|\[CQ:at,qq=([^,\]]+)",
                content,
                flags=re.I,
            ):
                target_id = next(
                    (str(value).strip() for value in user_id if value),
                    "",
                )
                if not target_id or target_id in seen_ids:
                    continue
                if target_id.casefold() in self_ids:
                    continue
                if cls._is_placeholder_user(target_id):
                    continue
                seen_ids.add(target_id)
                targets.append(
                    {"user_id": target_id, "display_name": ""}
                )

        return targets

    @staticmethod
    def _strip_mention_text(raw_message: str, target: dict) -> str:
        """剥离被 @ 目标的各通道表示，返回剩余文本（保留大小写）。"""
        remaining = str(raw_message or "")
        user_id = str(target.get("user_id") or "")
        if user_id:
            remaining = re.sub(
                rf"<@!?{re.escape(user_id)}>",
                " ",
                remaining,
                flags=re.I,
            )
            remaining = re.sub(
                rf"\[CQ:at,qq={re.escape(user_id)}(?:,[^\]]*)?\]",
                " ",
                remaining,
                flags=re.I,
            )
        display_name = str(target.get("display_name") or "").strip()
        if display_name:
            remaining = re.sub(
                rf"@{re.escape(display_name)}",
                " ",
                remaining,
                flags=re.I,
            )
        remaining = re.sub(r"\[At:[^\]]+\]", " ", remaining, flags=re.I)
        return re.sub(r"\s+", " ", remaining).strip()

    @classmethod
    def _mentioned_profile_target(
        cls,
        event: AstrMessageEvent,
        raw_message: str,
    ) -> Optional[dict]:
        """识别“@某人”资料卡查询，要求只有一个非机器人的被 @ 用户。"""
        targets = cls._collect_mention_targets(event, raw_message)
        if len(targets) != 1:
            return None
        command = normalize_command(
            cls._strip_any_mention_tokens(
                cls._strip_mention_text(raw_message, targets[0])
            )
        )
        # "我的战绩/我的账号"即使 @ 了别人也只查自己，不能走"查他人"分支
        if command in SELF_ONLY_PROFILE_COMMANDS:
            return None
        if command not in MENTION_PROFILE_COMMANDS:
            return None
        return targets[0]

    @classmethod
    def _mentioned_admin_bind(
        cls,
        event: AstrMessageEvent,
        raw_message: str,
    ) -> Optional[dict]:
        """识别“@某人 绑定/代绑定<平台> <账号>”；非该形态返回 None。"""
        targets = cls._collect_mention_targets(event, raw_message)
        if not targets:
            return None

        if len(targets) > 1:
            # 多个被 @：无法确定要绑定谁。若这条确实是绑定指令，必须明确提示，
            # 绝不能返回 None 让上层退回「给发送者自助绑定」——那会把被 @ 成员的
            # 账号绑到发送者自己名下。
            remaining = str(raw_message or "")
            for target in targets:
                remaining = cls._strip_mention_text(remaining, target)
            remaining = cls._strip_any_mention_tokens(remaining)
            if ADMIN_BIND_RE.match(remaining) or ADMIN_BIND_USAGE_RE.match(
                remaining
            ):
                return {"ambiguous": True, "count": len(targets)}
            return None

        remaining = cls._strip_any_mention_tokens(
            cls._strip_mention_text(raw_message, targets[0])
        )

        match = ADMIN_BIND_RE.match(remaining)
        if match:
            platform = normalize_platform(match.group(1))
            if not platform:
                return None
            return {
                "user_id": targets[0]["user_id"],
                "display_name": targets[0]["display_name"],
                "platform": platform,
                "identifier": match.group(2).strip(),
            }

        usage = ADMIN_BIND_USAGE_RE.match(remaining)
        if usage:
            platform = normalize_platform(usage.group(1))
            if not platform:
                return None
            return {
                "user_id": targets[0]["user_id"],
                "display_name": targets[0]["display_name"],
                "platform": platform,
                "identifier": "",
            }
        return None

    @classmethod
    def _literal_at_admin_bind(
        cls,
        event: AstrMessageEvent,
        raw_message: str,
    ) -> Optional[dict]:
        """识别“手打 @昵称 + 绑定指令”。

        手打的 @ 只是普通文字，QQ 不会给出被 @ 人的 openid，因此无法确定绑定目标。
        这里单独识别，用于**给出提示**，避免管理员以为指令没生效（此前表现为完全静默）。
        """
        if not str(event.get_group_id() or "").strip():
            return None
        text = str(raw_message or "").strip()
        if not text.startswith("@"):
            return None
        # 有真正的 @ 时交给 _mentioned_admin_bind 正常处理
        if cls._collect_mention_targets(event, raw_message):
            return None
        match = re.match(r"^@(\S+)\s+(.+?)\s*$", text, flags=re.S)
        if not match:
            return None
        bind = ADMIN_BIND_RE.match(match.group(2).strip())
        if not bind:
            return None
        platform = normalize_platform(bind.group(1))
        if not platform:
            return None
        return {
            "literal": match.group(1),
            "platform": platform,
            "identifier": bind.group(2).strip(),
        }

    @staticmethod
    def _self_id_candidates(event, message_obj, raw) -> set:
        """收集"机器人自己"的所有可能标识（小写化）。

        QQ 官方适配器有时把 `get_self_id()` 报成占位符（如 `qq_official`），
        只靠它会把"@机器人"误判成"@了某个成员"，因此多取几个来源并丢掉占位符。
        """
        candidates = set()
        for value in (
            getattr(event, "get_self_id", lambda: "")(),
            getattr(message_obj, "self_id", ""),
            getattr(raw, "self_id", ""),
        ):
            text = str(value or "").strip()
            if text and text.casefold() not in PLACEHOLDER_USER_IDS:
                candidates.add(text.casefold())
        return candidates

    @staticmethod
    def _account_platform_help() -> str:
        return (
            "用法：绑定cf/绑定牛客/绑定洛谷/绑定atcoder <用户名、UID或主页链接>\n"
            "例如：绑定cf jiangly · 绑定牛客 12345678 · "
            "绑定洛谷 123456 · 绑定atcoder tourist\n"
            "验证字段：CF 姓氏、牛客个性签名、洛谷个人介绍、"
            "AtCoder Affiliation（所属）"
        )

    @staticmethod
    def _account_bind_command(platform: str) -> str:
        return ACCOUNT_BIND_USAGE_HINTS.get(
            platform,
            (f"绑定{platform}", "<账号>", "对应公开资料字段"),
        )[0]

    async def _account_verification_value(
        self,
        platform: str,
        identifier: str,
        profile,
    ) -> str:
        """读取绑定校验字段；洛谷由 .com 用户页提供个人介绍。"""
        getter = getattr(
            self.account_fetcher,
            "get_verification_value",
            None,
        )
        if callable(getter):
            return str(
                await getter(
                    platform,
                    identifier,
                    profile=profile,
                    force=True,
                )
                or ""
            )
        # 兼容旧版抓取器短暂未同步的情况；完整更新后洛谷会走 .com。
        return str(getattr(profile, "verification_value", "") or "")

    @staticmethod
    def _luogu_verification_empty_text(
        profile,
        *,
        confirmation: bool = False,
    ) -> str:
        """区分洛谷个人介绍为空、字段缺失和资料读取失败。"""
        extra = getattr(profile, "extra", {}) or {}
        state = extra.get("verification_field_state") if isinstance(extra, dict) else ""
        if state == "empty":
            if confirmation:
                return (
                    "⚠️ 洛谷个人介绍为空，请先填写个人介绍并追加验证码，"
                    "然后再次发送确认绑定指令"
                )
            return (
                "⚠️ 洛谷个人介绍为空，请先填写个人介绍，"
                "然后重新发送绑定洛谷指令"
            )
        if state == "missing":
            return "⚠️ 洛谷个人介绍字段不存在，暂时无法绑定"
        return "⚠️ 洛谷个人介绍暂时无法读取，暂时无法绑定"

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
        record_metrics: bool = True,
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
                            include_difficulty=detail,
                            include_analysis=detail,
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
            if record_metrics:
                await self._record_profile_metric(user_id, result)
        return accounts, profiles, errors

    @staticmethod
    def _profile_metric(profile) -> Optional[dict]:
        """返回排行/快照使用的统一指标；洛谷没有 Elo 时按公开排名排行。"""
        if profile.rating is not None:
            metric_label = (
                "Elo"
                if profile.platform == "luogu"
                else "Rating"
            )
            return {
                "snapshot_key": profile.platform,
                "value": int(profile.rating),
                "display_value": str(profile.rating),
                "metric_label": metric_label,
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
                "display_value": _platform_rank_text(profile) or f"#{rank}",
                "metric_label": "平台排名",
                "sort_value": -rank,
            }
        return None

    async def _record_profile_metric(self, user_id: str, profile) -> None:
        metric = self._profile_metric(profile)
        if metric is None:
            return
        try:
            await self.account_registry.record_rating(
                user_id,
                metric["snapshot_key"],
                metric["value"],
            )
        except Exception as exc:  # noqa: BLE001 - 快照失败不能阻断资料展示
            logger.warning(
                "记录 %s 的 %s Rating 快照失败：%s",
                user_id,
                platform_label(profile.platform),
                exc,
            )

    async def _profile_weekly_delta(self, user_id: str, profile) -> Optional[int]:
        calculator = getattr(
            self.account_fetcher, "rating_delta_for_period", None
        )
        try:
            direct = (
                calculator(profile, days=7)
                if callable(calculator)
                else None
            )
        except Exception as exc:  # noqa: BLE001 - 变化值是可选展示项
            logger.warning(
                "计算 %s 的 %s 本周变化失败：%s",
                user_id,
                platform_label(profile.platform),
                exc,
            )
            direct = None
        if direct is not None:
            return direct
        metric = self._profile_metric(profile)
        if metric is None:
            return None
        try:
            return await self.account_registry.weekly_delta(
                user_id,
                metric["snapshot_key"],
                days=7,
            )
        except Exception as exc:  # noqa: BLE001 - 快照失败不影响资料卡
            logger.warning(
                "读取 %s 的 %s 本周变化失败：%s",
                user_id,
                platform_label(profile.platform),
                exc,
            )
            return None

    async def _run_render(self, func, *args, **kwargs):
        """在全局渲染信号量内执行阻塞渲染，避免多浏览器/大画布并发。"""
        async with self._render_semaphore:
            return await asyncio.to_thread(func, *args, **kwargs)

    async def _render_profile_card(
        self,
        profiles,
        *,
        display_name: str,
        weekly_changes,
        group_ranks,
    ):
        """渲染个人资料卡；任何 UI/浏览器异常都回退到文字。"""
        try:
            return await self._run_render(
                self.account_card_renderer.render_profile,
                profiles,
                display_name=display_name,
                weekly_changes=weekly_changes,
                group_ranks=group_ranks,
            )
        except Exception as exc:  # noqa: BLE001 - UI 失败不能阻断账号查询
            logger.error("个人资料卡渲染失败，改用文字：%s", exc, exc_info=True)
            return None

    async def _render_ranking_card(
        self,
        rows,
        *,
        title: str,
        subtitle: str,
        metric_label: str,
        note: str,
        platform: str = "",
    ):
        """渲染平台排行卡；失败时交给调用方发送文字。"""
        try:
            return await self._run_render(
                self.account_card_renderer.render_ranking,
                rows,
                title=title,
                subtitle=subtitle,
                metric_label=metric_label,
                note=note,
                platform=platform,
            )
        except Exception as exc:  # noqa: BLE001 - UI 失败不能阻断排行查询
            logger.error("平台排行卡渲染失败，改用文字：%s", exc, exc_info=True)
            return None

    async def _render_overview_card(
        self,
        sections,
        *,
        title: str,
        subtitle: str,
        metric_label: str,
        note: str,
        secondary_label: str = "近7日变化",
        secondary_value_key: str = "delta",
    ):
        """渲染排行总览卡；失败时交给调用方发送文字。"""
        try:
            return await self._run_render(
                self.account_card_renderer.render_overview_ranking,
                sections,
                title=title,
                subtitle=subtitle,
                metric_label=metric_label,
                note=note,
                secondary_label=secondary_label,
                secondary_value_key=secondary_value_key,
            )
        except Exception as exc:  # noqa: BLE001 - UI 失败不能阻断排行查询
            logger.error("排行总览卡渲染失败，改用文字：%s", exc, exc_info=True)
            return None

    async def _load_group_rank_summary(
        self,
        group_id: str,
        user_id: str,
        platforms,
        *,
        read_only: bool = False,
        force: bool = False,
    ) -> dict:
        """读取当前用户在群内各平台的名次；复用群排行短缓存。"""
        gid = str(group_id or "").strip()
        uid = str(user_id or "").strip()
        platform_list = list(
            dict.fromkeys(
                platform
                for platform in platforms
                if platform in ACCOUNT_PLATFORMS
            )
        )
        if not gid or not uid or not platform_list:
            return {}

        results = await asyncio.gather(
            *(
                self.rank_service.read(
                    gid,
                    platform,
                    progress=False,
                    record_metrics=not read_only,
                    allow_stale=not force,
                    force=force,
                )
                for platform in platform_list
            ),
            return_exceptions=True,
        )
        summary = {}
        for platform, result in zip(platform_list, results):
            if isinstance(result, Exception):
                summary[platform] = {"unavailable": True}
                continue
            rows, errors = result
            rank = next(
                (
                    index
                    for index, row in enumerate(rows, start=1)
                    if str(row.get("user_id") or "") == uid
                ),
                None,
            )
            summary[platform] = {
                "rank": rank,
                "total": len(rows),
                "unavailable": bool(errors) and rank is None,
            }
        return summary

    @classmethod
    def _format_account_text(
        cls,
        profiles,
        errors,
        weekly_changes=None,
        *,
        title: str = "📊 我的竞赛战绩",
        group_ranks=None,
    ) -> str:
        weekly_changes = weekly_changes or {}
        group_ranks = group_ranks or {}
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
                "  "
                + "｜".join(
                    value
                    for value in (
                        (
                            f"最高 Rating：{profile.max_rating}"
                            if profile.max_rating is not None
                            else ""
                        ),
                        (
                            f"参赛：{profile.contest_count}"
                            if profile.contest_count is not None
                            else ""
                        ),
                        (
                            f"{_solved_count_label(profile)}：{profile.solved_count}"
                            if profile.solved_count is not None
                            else ""
                        ),
                        (
                            f"贡献：{profile.contribution}"
                            if profile.contribution is not None
                            else ""
                        ),
                        f"本周变化：{_format_signed_number(delta)}",
                    )
                    if value
                )
            )
            analysis = getattr(profile, "analysis", {}) or {}
            difficulty = getattr(profile, "difficulty_distribution", []) or []
            if isinstance(difficulty, list):
                distribution = " · ".join(
                    f"{item.get('label')} {item.get('count')}"
                    for item in difficulty
                    if isinstance(item, dict)
                    and item.get("label")
                    and item.get("count") is not None
                )
                if distribution:
                    difficulty_title = (
                        analysis.get("difficulty_title")
                        if isinstance(analysis, dict)
                        else ""
                    ) or "CF 做题分布"
                    lines.append(
                        f"  {difficulty_title}：{distribution}"
                    )
            if isinstance(analysis, dict):
                category = analysis.get("category_distribution") or []
                activity = analysis.get("activity_distribution") or []
                if activity and not difficulty:
                    activity_text = " · ".join(
                        f"{item.get('label')} {item.get('count')}"
                        for item in activity
                        if isinstance(item, dict)
                        and item.get("label")
                        and item.get("count") is not None
                    )
                    if activity_text:
                        lines.append(
                            f"  {analysis.get('activity_title') or '活跃度分析'}："
                            f"{activity_text}"
                        )
                if category:
                    category_text = " · ".join(
                        f"{item.get('label')} {item.get('count')}"
                        for item in category
                        if isinstance(item, dict)
                        and item.get("label")
                        and item.get("count") is not None
                    )
                    if category_text:
                        lines.append(
                            f"  {analysis.get('category_title') or '数据分布'}："
                            f"{category_text}"
                        )
                language = analysis.get("language_distribution") or []
                if language:
                    language_text = " · ".join(
                        f"{item.get('label')} {item.get('count')}"
                        for item in language[:5]
                        if isinstance(item, dict)
                        and item.get("label")
                        and item.get("count") is not None
                    )
                    if language_text:
                        lines.append(f"  常用语言：{language_text}")
                summary = analysis.get("summary") or []
                summary_text = " · ".join(
                    f"{item.get('label')} {item.get('value')}"
                    for item in summary
                    if isinstance(item, dict)
                    and item.get("label")
                    and item.get("value") is not None
                )
                if summary_text:
                    lines.append(f"  数据分析：{summary_text}")
                source = str(analysis.get("source") or "").strip()
                coverage = str(analysis.get("coverage") or "").strip()
                if source:
                    lines.append(f"  数据源：{source}")
                if coverage:
                    lines.append(f"  统计范围：{coverage}")
            rank_info = group_ranks.get(profile.platform)
            if isinstance(rank_info, dict):
                if rank_info.get("rank") is not None:
                    lines.append(
                        f"  本群排行：第 {rank_info['rank']} / "
                        f"{rank_info.get('total') or '—'} 名"
                    )
                elif rank_info.get("unavailable"):
                    lines.append("  本群排行：暂时无法计算")
                else:
                    lines.append("  本群排行：未进入榜单")
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
            verification_value = await self._account_verification_value(
                platform,
                identifier,
                profile,
            )
            if platform == "luogu":
                extra = getattr(profile, "extra", {}) or {}
                state = (
                    extra.get("verification_field_state")
                    if isinstance(extra, dict)
                    else ""
                )
                # 个人介绍为空是可绑定状态：需要先发验证码，再让用户把
                # 验证码追加到空的个人介绍中。只有字段缺失/读取失败才阻止发码。
                if not verification_value.strip() and state != "empty":
                    yield event.plain_result(
                        self._luogu_verification_empty_text(profile)
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
        confirm_command = (
            self._account_bind_command(platform)
            .replace("绑定", "确认绑定", 1)
        )
        group_hint = (
            "\n建议在机器人私聊中完成绑定，避免验证码出现在群消息里。"
            if event.get_group_id()
            else ""
        )
        yield event.plain_result(
            f"✅ 已找到 {platform_label(platform)} 账号：{profile.handle}\n"
            f"请在该账号的【{field}】中追加：{token}\n"
            f"修改完成后发送：{confirm_command}{group_hint}\n"
            "验证码 10 分钟内有效，验证成功后可以删除。"
        )

    async def _reply_admin_bind(
        self,
        event: AstrMessageEvent,
        bind: dict,
    ):
        # 目标不唯一：先给用法提示（与权限无关，且必须阻止退回自助绑定）
        if bind.get("ambiguous"):
            yield event.plain_result(
                f"⚠️ 检测到 {int(bind.get('count') or 0)} 个被 @ 的成员，"
                "无法确定要绑定谁。\n"
                "请只 @ 要绑定的那个人；如果要绑定自己，请不要 @ 任何人。"
            )
            return

        # 授权口径：只认后台 admin_users；QQ 群主/群管理员角色一律不认。
        if not await self._is_admin(event):
            yield event.plain_result("此指令仅限管理员")
            return

        target_user_id = str(bind.get("user_id") or "").strip()
        platform = str(bind.get("platform") or "")
        identifier = str(bind.get("identifier") or "").strip()
        target_display_name = str(bind.get("display_name") or "").strip()
        target_label = (
            f"@{target_display_name}" if target_display_name else "该成员"
        )
        if not target_user_id:
            yield event.plain_result("无法识别被 @ 的成员，请重新发送")
            return
        if platform not in ACCOUNT_PLATFORMS:
            yield event.plain_result("不支持的平台")
            return

        if not identifier:
            command, argument_hint, _ = ACCOUNT_BIND_USAGE_HINTS.get(
                platform,
                (f"绑定{platform}", "<账号>", "对应公开资料字段"),
            )
            sample = ACCOUNT_BIND_EXAMPLES.get(
                platform, f"{command} <账号>"
            )
            yield event.plain_result(
                f"用法：{target_label} {command} {argument_hint}\n"
                f"例如：{target_label} {sample}"
            )
            return

        group_id = str(event.get_group_id() or "").strip()

        # 管理员代操作，跳过验证码：只抓公开资料，不读验证字段。
        try:
            profile = await self.account_fetcher.get_profile(
                platform,
                identifier,
                detail=False,
                force=True,
            )
        except AccountFetchError as exc:
            yield event.plain_result(self._account_error_text(platform, exc))
            return
        except Exception as exc:  # noqa: BLE001 - 抓取异常不能把堆栈抛进群
            logger.warning(
                "管理员代绑定抓取 %s 资料失败：%s",
                platform_label(platform),
                exc,
            )
            yield event.plain_result(self._account_error_text(platform, exc))
            return

        # 仅用于“已替换”提示；不参与冲突判断（冲突交给原子写）。
        old_handle = ""
        try:
            accounts = await self.account_registry.get_user_accounts(
                target_user_id
            )
            record = accounts.get(platform)
            if isinstance(record, dict):
                old_handle = str(
                    record.get("handle")
                    or record.get("platform_user_id")
                    or ""
                )
        except Exception as exc:  # noqa: BLE001 - 提示失败不影响绑定
            logger.warning(
                "读取 %s 原有绑定失败：%s", target_user_id, exc
            )

        try:
            await self.account_registry.save_binding(
                target_user_id,
                platform,
                profile,
                group_id=group_id or None,
                qq_name=target_display_name,
            )
        except ValueError:
            unbind_command = self._account_bind_command(platform).replace(
                "绑定", "解绑", 1
            )
            yield event.plain_result(
                f"⚠️ 这个{platform_label(platform)}账号已经绑定到其他 QQ 用户，"
                "无法代绑定。\n"
                f"如需变更，请先让原绑定者发送：{unbind_command}"
            )
            return
        except Exception as exc:  # noqa: BLE001 - 存储故障要有用户可见反馈
            logger.error("管理员代绑定保存失败：%s", exc, exc_info=True)
            yield event.plain_result("⚠️ 绑定保存失败，请稍后重试")
            return

        self._invalidate_all_rank_cache()
        try:
            await self._record_profile_metric(target_user_id, profile)
        except Exception as exc:  # noqa: BLE001 - 快照失败不阻断绑定
            logger.warning(
                "代绑定记录 %s 的评分快照失败：%s", target_user_id, exc
            )

        logger.info(
            "管理员代绑定成功 admin=%s group=%s target=%s platform=%s handle=%s",
            str(event.get_sender_id() or ""),
            group_id,
            target_user_id,
            platform,
            profile.handle,
        )

        lines = [
            f"✅ 已将 {target_label} 绑定 {platform_label(platform)} 账号 "
            f"{profile.handle}（管理员代绑定）"
        ]
        if old_handle and old_handle.casefold() != str(
            profile.handle or ""
        ).casefold():
            lines.append(f"已替换原有绑定：{old_handle}")
        if group_id:
            lines.append("已自动加入本群竞赛排行。")
        else:
            lines.append("目标在群内发送一次“我的战绩”即可加入该群排行。")
        yield event.plain_result("\n".join(lines))

    async def _reply_admin_bind_mention_hint(self, event: AstrMessageEvent):
        """手打 @昵称 时的提示：那不是真正的 @，拿不到目标 openid。"""
        if not await self._is_admin(event):
            yield event.plain_result("此指令仅限管理员")
            return
        yield event.plain_result(
            "⚠️ 没识别到真正的 @：请用 QQ 输入框里的「@」从成员列表选择要绑定的人，"
            "手打 @昵称 只是普通文字、拿不到对方 ID。\n"
            "用法：@某人 绑定cf <账号>"
        )

    async def _reply_account_confirm(
        self,
        event: AstrMessageEvent,
        platform: str,
        identifier: str = "",
    ):
        user_id = str(event.get_sender_id() or "").strip()
        try:
            pending = await self.account_registry.get_pending(user_id, platform)
        except Exception as exc:  # noqa: BLE001 - 绑定状态读取失败要有反馈
            logger.error(
                "读取 %s 的 %s 待确认绑定失败：%s",
                user_id,
                platform_label(platform),
                exc,
                exc_info=True,
            )
            yield event.plain_result("⚠️ 绑定状态暂时无法读取，请稍后重试")
            return
        if pending is None:
            yield event.plain_result(
                f"没有找到待确认的{platform_label(platform)}绑定请求，"
                f"请先发送：{self._account_bind_command(platform)} <账号>"
            )
            return
        pending_platform = str(pending.get("platform") or platform).strip()
        if pending_platform != platform:
            logger.warning(
                "用户 %s 的待确认绑定平台异常：期望 %s，实际 %s",
                user_id,
                platform,
                pending_platform,
            )
            try:
                await self.account_registry.clear_pending(user_id, platform)
            except Exception:
                pass
            yield event.plain_result(
                "⚠️ 待确认绑定信息已失效，请重新发送绑定指令"
            )
            return
        pending_identifier = str(
            pending.get("platform_user_id") or pending.get("handle") or ""
        ).strip()
        normalized_pending = normalize_account_identifier(
            platform, pending_identifier
        )
        if not normalized_pending:
            try:
                await self.account_registry.clear_pending(user_id, platform)
            except Exception:
                pass
            yield event.plain_result(
                "⚠️ 待确认账号信息已失效，请重新发送绑定指令"
            )
            return
        if identifier:
            normalized = normalize_account_identifier(
                platform, identifier
            )
            if not normalized:
                yield event.plain_result(
                    f"⚠️ {platform_label(platform)}账号参数格式不正确，"
                    f"请直接发送确认绑定{platform}，或填写正确的用户名、UID或主页链接"
                )
                return
            if normalized.casefold() != normalized_pending.casefold():
                yield event.plain_result(
                    f"待确认账号是 {pending.get('handle') or normalized_pending}，"
                    "如需更换请重新发送绑定指令"
                )
                return
        try:
            profile = await self.account_fetcher.get_profile(
                platform,
                normalized_pending,
                detail=False,
                force=True,
            )
        except AccountFetchError as exc:
            yield event.plain_result(self._account_error_text(platform, exc))
            return
        except Exception as exc:
            yield event.plain_result(self._account_error_text(platform, exc))
            return

        try:
            verification_value = await self._account_verification_value(
                platform,
                normalized_pending,
                profile,
            )
        except AccountFetchError as exc:
            # 洛谷资料页临时不可用时保留待确认状态，用户可在有效期内直接重试。
            yield event.plain_result(self._account_error_text(platform, exc))
            return
        except Exception as exc:
            yield event.plain_result(self._account_error_text(platform, exc))
            return

        if platform == "luogu" and not verification_value.strip():
            # 字段为空/缺失不是验证码失效；保留待确认记录，用户补充资料后
            # 可以在有效期内直接重试确认绑定。
            yield event.plain_result(
                self._luogu_verification_empty_text(
                    profile,
                    confirmation=True,
                )
            )
            return
        expected_hash = str(pending.get("token_hash") or "")
        if not self.account_registry.token_matches(
            verification_value, expected_hash
        ):
            field = VERIFICATION_FIELD_LABELS.get(platform, "公开资料字段")
            yield event.plain_result(
                f"暂未在{field}中找到验证码，请确认已经追加正确验证码，"
                "然后再次发送确认绑定指令"
            )
            return

        # 绑定开始时记录的群是自动加入排行的归属群。只有私聊发起绑定、
        # 待确认记录没有群时，才使用确认消息所在的群。
        pending_group_id = str(pending.get("group_id") or "").strip()
        event_group_id = str(event.get_group_id() or "").strip()
        group_id = pending_group_id or event_group_id
        try:
            await self.account_registry.save_binding(
                user_id,
                platform,
                profile,
                group_id=group_id,
                qq_name=self._event_display_name(event),
            )
            self._invalidate_all_rank_cache()
            await self._record_profile_metric(user_id, profile)
        except ValueError as exc:
            yield event.plain_result(f"⚠️ 绑定失败：{exc}")
            return
        except Exception as exc:
            logger.error("保存%s绑定失败：%s", platform_label(platform), exc, exc_info=True)
            yield event.plain_result("⚠️ 绑定保存失败，请稍后重试")
            return
        if pending_group_id:
            rank_hint = "已自动加入发起绑定群的竞赛排行。"
        elif event_group_id:
            rank_hint = "已自动加入本次确认所在群的竞赛排行。"
        else:
            rank_hint = (
                "这是私聊绑定；在目标群发送一次“我的战绩”即可自动加入该群排行。"
            )
        yield event.plain_result(
            f"🎉 {platform_label(platform)} 账号 {profile.handle} 绑定成功！\n"
            + rank_hint
        )

    async def _reply_account_unbind(
        self, event: AstrMessageEvent, platform: str
    ):
        user_id = str(event.get_sender_id() or "").strip()
        try:
            removed = await self.account_registry.remove_binding(user_id, platform)
        except Exception as exc:  # noqa: BLE001 - 存储异常要转成用户可见反馈
            logger.error(
                "解绑 %s 的 %s 账号失败：%s",
                user_id,
                platform_label(platform),
                exc,
                exc_info=True,
            )
            yield event.plain_result("⚠️ 解绑失败，绑定数据暂时无法读取")
            return
        if removed:
            self._invalidate_all_rank_cache()
            try:
                remaining = await self.account_registry.get_user_accounts(user_id)
                if not remaining:
                    await self.account_registry.remove_user_from_all_groups(user_id)
                    suffix = "，并退出所有群排行"
                else:
                    suffix = ""
            except Exception as exc:  # noqa: BLE001 - 解绑已完成，不能误报为失败
                logger.warning(
                    "解绑后清理 %s 的群排行状态失败：%s",
                    user_id,
                    exc,
                )
                suffix = ""
            yield event.plain_result(f"✅ 已解绑 {platform_label(platform)}{suffix}")
        else:
            yield event.plain_result(f"你还没有绑定{platform_label(platform)}账号")

    # ------------------------------------------------------------------
    # 指令：未绑定用户战绩查询（查询cf <用户名/UID/主页链接>）
    # ------------------------------------------------------------------
    @staticmethod
    def _account_lookup_usage(platform: str) -> str:
        example = ACCOUNT_LOOKUP_USAGE_HINTS.get(
            platform, f"查询{platform} <账号>"
        )
        sample = ACCOUNT_LOOKUP_EXAMPLES.get(platform, example)
        lines = [
            f"用法：{example}",
            f"例如：{sample}",
            "请把账号写在指令后面，不能只发送指令前缀。",
        ]
        if platform in ACCOUNT_LOOKUP_UID_ONLY:
            lines.append(
                f"注意：{platform_label(platform)}公开接口不支持用户名查询，"
                "请填写数字 UID 或该用户的主页链接。"
            )
        lines.append("默认输出摘要卡；想同时看难度分布/分析，请在结尾加“详细”。")
        return "\n".join(lines)

    @staticmethod
    def _account_lookup_invalid_hint(platform: str) -> str:
        label = platform_label(platform)
        if platform == "codeforces":
            detail = "Codeforces 只接受用户名（字母/数字/下划线）或主页链接。"
        elif platform == "atcoder":
            detail = "AtCoder 只接受用户名（字母/数字/下划线）或主页链接。"
        elif platform == "nowcoder":
            detail = (
                "牛客公开接口不支持用户名查询，请填写数字用户 ID，"
                "或形如 https://ac.nowcoder.com/acm/contest/profile/<UID> 的主页链接。"
            )
        else:
            detail = (
                "洛谷公开接口不支持用户名查询，请填写数字 UID，"
                "或形如 https://www.luogu.com.cn/user/<UID> 的主页链接。"
            )
        return f"⚠️ 无法识别该{label}账号。\n{detail}"

    @staticmethod
    def _lookup_rate_verdict(
        now: float, user_last: float, group_times: list
    ) -> tuple[bool, int]:
        """返回 (是否放行, 需等待秒数)；纯函数，便于测试。"""
        if user_last and now - float(user_last) < LOOKUP_USER_COOLDOWN_SECONDS:
            wait = LOOKUP_USER_COOLDOWN_SECONDS - (now - float(user_last))
            return False, max(1, int(wait) + 1)
        recent = [
            float(item)
            for item in (group_times or [])
            if isinstance(item, (int, float))
            and now - float(item) < LOOKUP_GROUP_WINDOW_SECONDS
        ]
        if len(recent) >= LOOKUP_GROUP_MAX_PER_WINDOW:
            wait = LOOKUP_GROUP_WINDOW_SECONDS - (now - min(recent))
            return False, max(1, int(wait) + 1)
        return True, 0

    async def _check_lookup_rate_limit(
        self, event: AstrMessageEvent
    ) -> tuple[bool, int]:
        """查询指令的防滥用：每用户冷却 + 每群每分钟次数上限。"""
        user_id = str(event.get_sender_id() or "").strip()
        group_id = str(event.get_group_id() or "").strip()
        now = time.time()
        user_key = f"lookup_cd_{user_id}" if user_id else ""
        group_key = f"lookup_win_{group_id}" if group_id else ""
        try:
            user_last = (
                float(await self.get_kv_data(user_key, 0.0) or 0.0)
                if user_key
                else 0.0
            )
            group_times = (
                await self.get_kv_data(group_key, []) if group_key else []
            )
        except Exception as exc:  # noqa: BLE001 - 限流读取失败不阻断功能
            logger.warning("读取查询限流状态失败：%s", exc)
            return True, 0
        if not isinstance(group_times, list):
            group_times = []
        allowed, wait = self._lookup_rate_verdict(now, user_last, group_times)
        if not allowed:
            return False, wait
        try:
            if user_key:
                await self.put_kv_data(user_key, now)
            if group_key:
                recent = [
                    float(item)
                    for item in group_times
                    if isinstance(item, (int, float))
                    and now - float(item) < LOOKUP_GROUP_WINDOW_SECONDS
                ]
                recent.append(now)
                await self.put_kv_data(group_key, recent)
        except Exception as exc:  # noqa: BLE001 - 写入失败只是限流失效
            logger.warning("写入查询限流状态失败：%s", exc)
        return True, 0

    async def _reply_account_lookup(
        self, event: AstrMessageEvent, platform: str, raw_target: str
    ):
        """按用户名/UID/主页链接查询任意（未绑定）用户的战绩卡。"""
        target_text = str(raw_target or "").strip()
        detail = False
        detail_match = ACCOUNT_LOOKUP_DETAIL_RE.match(target_text)
        if detail_match:
            target_text = detail_match.group("target").strip()
            detail = True
        identifier = normalize_account_identifier(platform, target_text)
        if not identifier:
            yield event.plain_result(self._account_lookup_invalid_hint(platform))
            return
        allowed, wait = await self._check_lookup_rate_limit(event)
        if not allowed:
            yield event.plain_result(f"⏳ 查询太频繁了，请 {wait} 秒后再试")
            return
        label = platform_label(platform)
        try:
            profile = await self.account_fetcher.get_profile(
                platform,
                identifier,
                detail=True,
                include_submissions=False,
                include_difficulty=detail,
                include_analysis=detail,
            )
        except Exception as exc:  # noqa: BLE001 - 平台/网络错误统一文案
            yield event.plain_result(self._account_error_text(platform, exc))
            return
        delta = None
        calculator = getattr(
            self.account_fetcher, "rating_delta_for_period", None
        )
        if callable(calculator):
            try:
                delta = calculator(profile, days=7)
            except Exception as exc:  # noqa: BLE001 - 变化值是可选展示项
                logger.warning("计算 %s 的本周变化失败：%s", identifier, exc)
        weekly_changes = {platform: delta}
        # 未绑定用户不写 Rating 快照、不参与群排行，避免污染排行/进步榜数据。
        try:
            image_path = await self._render_profile_card(
                [profile],
                display_name=identifier,
                weekly_changes=weekly_changes,
                group_ranks={},
            )
        except Exception as exc:  # noqa: BLE001 - 渲染失败必须保证文字兜底
            logger.error("未绑定用户资料卡渲染异常：%s", exc, exc_info=True)
            image_path = None
        if image_path is not None and image_path.is_file():
            yield event.image_result(str(image_path))
            return
        title = f"📊 {identifier} 的{label}战绩（未绑定）"
        text = self._format_account_text(
            [profile], [], weekly_changes, title=title, group_ranks={}
        )
        if str(event.get_group_id() or "").strip():
            # 群聊里顺带引导绑定；私聊不加，避免打扰。
            text += (
                f"\n\n💡 发送“{self._account_bind_command(platform)} "
                f"{identifier}”可把该账号绑定到你的 QQ。"
            )
        async for result in self._adaptive_results(event, text):
            yield result

    async def _reply_my_account(
        self,
        event: AstrMessageEvent,
        *,
        platform: Optional[str] = None,
        force: bool = False,
        target_user_id: Optional[str] = None,
        target_display_name: str = "",
    ):
        sender_id = str(event.get_sender_id() or "").strip()
        user_id = str(target_user_id or sender_id).strip()
        is_self_query = not target_user_id or user_id == sender_id
        if is_self_query:
            display_name = self._event_display_name(event)
        else:
            display_name = (
                str(target_display_name or "").strip()
                or "该成员"
            )
        if not user_id:
            yield event.plain_result("无法识别 QQ 用户，请稍后重试")
            return
        if is_self_query:
            try:
                display_name_changed = (
                    await self.account_registry.set_user_display_name(
                        user_id,
                        self._event_display_name(event),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 昵称更新不是查询前置条件
                logger.warning("更新 %s 的群排行昵称失败：%s", user_id, exc)
                display_name_changed = False
            if display_name_changed:
                self._invalidate_all_rank_cache()
        subject = "你" if is_self_query else display_name
        account_title = (
            "📊 我的竞赛战绩"
            if is_self_query
            else f"📊 {display_name} 的竞赛战绩"
        )
        if platform:
            try:
                accounts = await self.account_registry.get_user_accounts(user_id)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "读取 %s 的绑定账号失败：%s",
                    user_id,
                    exc,
                    exc_info=True,
                )
                yield event.plain_result("⚠️ 绑定数据暂时无法读取，请稍后重试")
                return
            record = accounts.get(platform)
            if not isinstance(record, dict):
                yield event.plain_result(
                    f"{subject}还没有绑定{platform_label(platform)}账号\n"
                    f"发送：{self._account_bind_command(platform)} <账号>"
                )
                return
            identifier = str(
                record.get("platform_user_id") or record.get("handle") or ""
            )
            group_id = str(event.get_group_id() or "").strip()
            group_ranks = {}
            try:
                profile = await self.account_fetcher.get_profile(
                    platform,
                    identifier,
                    detail=True,
                    force=force,
                    include_submissions=False,
                    include_difficulty=True,
                    include_analysis=True,
                )
                if is_self_query:
                    await self._record_profile_metric(user_id, profile)
                delta = await self._profile_weekly_delta(user_id, profile)
                if group_id:
                    if is_self_query:
                        try:
                            membership_changed = await self.account_registry.set_group_member(
                                group_id,
                                user_id,
                                True,
                                preserve_opt_out=True,
                            )
                            if membership_changed:
                                self._invalidate_rank_cache(group_id)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "更新 %s 在群 %s 的排行状态失败：%s",
                                user_id,
                                group_id,
                                exc,
                            )
                    try:
                        group_ranks = await self._load_group_rank_summary(
                            group_id,
                            user_id,
                            [platform],
                            read_only=not is_self_query,
                            force=force,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "读取 %s 在群 %s 的排行失败：%s",
                            user_id,
                            group_id,
                            exc,
                        )
                        group_ranks = {}
                image_path = await self._render_profile_card(
                    [profile],
                    display_name=display_name,
                    weekly_changes={platform: delta},
                    group_ranks=group_ranks,
                )
                if image_path is not None and image_path.is_file():
                    yield event.image_result(str(image_path))
                else:
                    async for result in self._adaptive_results(
                        event,
                        self._format_account_text(
                            [profile], [], {platform: delta},
                            title=(
                                f"📊 {platform_label(platform)}战绩"
                                if is_self_query
                                else (
                                    f"📊 {display_name} 的"
                                    f"{platform_label(platform)}战绩"
                                )
                            ),
                            group_ranks=group_ranks,
                        ),
                    ):
                        yield result
                # 详细资料（含题目级分析）之后追加"推荐补题"
                if profile.analysis:
                    async for result in self._maybe_recommend_problems(
                        event, platform, profile
                    ):
                        yield result
            except Exception as exc:
                yield event.plain_result(self._account_error_text(platform, exc))
            return

        try:
            accounts, profiles, errors = await self._load_bound_profiles(
                user_id,
                detail=True,
                force=force,
                record_metrics=is_self_query,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "读取 %s 的绑定资料失败：%s",
                user_id,
                exc,
                exc_info=True,
            )
            yield event.plain_result("⚠️ 绑定数据暂时无法读取，请稍后重试")
            return
        if not accounts:
            yield event.plain_result(
                f"{subject}还没有绑定竞赛平台账号。\n"
                + self._account_platform_help()
            )
            return
        if not profiles:
            message = self._format_account_text(
                profiles,
                errors,
                title=account_title,
            )
            yield event.plain_result(
                message
                or "暂时无法读取已绑定账号资料，请稍后重试"
            )
            return
        group_id = str(event.get_group_id() or "").strip()
        group_ranks = {}
        if group_id:
            if is_self_query:
                try:
                    membership_changed = await self.account_registry.set_group_member(
                        group_id,
                        user_id,
                        True,
                        preserve_opt_out=True,
                    )
                    if membership_changed:
                        self._invalidate_rank_cache(group_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "更新 %s 在群 %s 的排行状态失败：%s",
                        user_id,
                        group_id,
                        exc,
                    )
            try:
                group_ranks = await self._load_group_rank_summary(
                    group_id,
                    user_id,
                    [profile.platform for profile in profiles],
                    read_only=not is_self_query,
                    force=force,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "读取 %s 在群 %s 的排行失败：%s",
                    user_id,
                    group_id,
                    exc,
                )
        weekly = {
            profile.platform: await self._profile_weekly_delta(user_id, profile)
            for profile in profiles
        }
        image_path = await self._render_profile_card(
            profiles,
            display_name=display_name,
            weekly_changes=weekly,
            group_ranks=group_ranks,
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
            self._format_account_text(
                profiles,
                errors,
                weekly,
                title=account_title,
                group_ranks=group_ranks,
            ),
        ):
            yield result

    async def _collect_rank_rows(
        self,
        group_id: str,
        platform: str,
        *,
        progress: bool = False,
        record_metrics: bool = True,
    ):
        """读取排行结果；短时间内同一群/平台只计算一次。

        缓存 key 只含 (group, platform, progress)，不再包含 record_metrics：
        自己查（写快照）与被 @ 查（只读）共享同一份计算。是否补写 Rating
        快照作为缓存的“副作用”处理，见 _rank_cached_rows/_record_rank_metrics。
        """
        key = (
            str(group_id),
            platform,
            bool(progress),
        )
        self._prune_rank_cache()
        now = time.monotonic()
        cached = self._rank_cache.get(key)
        if cached and now - cached[0] < RANK_CACHE_TTL:
            return await self._rank_cached_rows(
                cached, record_metrics=record_metrics
            )

        lock = self._rank_cache_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._rank_cache.get(key)
            if cached and time.monotonic() - cached[0] < RANK_CACHE_TTL:
                return await self._rank_cached_rows(
                    cached, record_metrics=record_metrics
                )
            rows, errors, metric_entries = (
                await self._collect_rank_rows_uncached(
                    group_id,
                    platform,
                    progress=progress,
                    record_metrics=False,
                )
            )
            item: list = [
                time.monotonic(),
                rows,
                errors,
                metric_entries,
                None,
            ]
            self._rank_cache[key] = item
            if record_metrics:
                await self._record_rank_metrics(metric_entries)
                item[4] = time.monotonic()
            return rows, errors

    async def _rank_cached_rows(self, item: list, *, record_metrics: bool):
        """命中缓存后返回 rows/errors；需要写快照时补一次（单飞防重）。"""
        rows, errors = item[1], item[2]
        if record_metrics and item[4] is None:
            # 先占位再 await，避免并发请求重复写同一批快照。
            item[4] = time.monotonic()
            await self._record_rank_metrics(item[3])
        return rows, errors

    async def _record_rank_metrics(self, rating_entries: list) -> None:
        """把一批 (user, snapshot_key, value) 写入 Rating 快照。"""
        if not rating_entries:
            return
        bulk_recorder = getattr(
            self.account_registry,
            "record_ratings",
            None,
        )
        if callable(bulk_recorder):
            try:
                await bulk_recorder(rating_entries)
            except Exception as exc:  # noqa: BLE001 - 快照失败不影响当前排行
                logger.warning("批量记录群排行 Rating 快照失败：%s", exc)
            return
        for user_id, snapshot_key, value in rating_entries:
            try:
                await self.account_registry.record_rating(
                    user_id,
                    snapshot_key,
                    value,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "记录群排行用户 %s 的 Rating 快照失败：%s",
                    user_id,
                    exc,
                )

    def _invalidate_rank_cache(self, group_id: str) -> None:
        """成员绑定/退出或昵称更新后，让对应群排行立即重新计算。"""
        group_key = str(group_id)
        for key in list(self._rank_cache):
            if key[0] == group_key:
                self._rank_cache.pop(key, None)
        self._rank_dirty_pending.add(group_key)
        try:
            asyncio.get_running_loop().create_task(
                self._mark_group_rank_dirty(str(group_id))
            )
        except RuntimeError:
            pass

    async def _mark_group_rank_dirty(self, group_id: str) -> None:
        """让 SQLite 快照对该群所有平台标记脏（rank + progress）。"""
        registry = getattr(self, "account_registry", None)
        store = getattr(registry, "store", None)
        if store is None or not getattr(registry, "store_enabled", False):
            return
        try:
            for platform in ACCOUNT_PLATFORMS:
                for mode in ("rank", "progress"):
                    await store.mark_rank_dirty(
                        group_id, platform, mode=mode
                    )
        except Exception as exc:  # noqa: BLE001 - 脏标记失败只影响刷新时机
            logger.warning("标记群 %s 排行脏失败: %s", group_id, exc)

    def _invalidate_all_rank_cache(self) -> None:
        """账号关系或展示名称变化时清理所有群的排行缓存。"""
        self._rank_cache.clear()
        try:
            asyncio.get_running_loop().create_task(self._mark_all_ranks_dirty())
        except RuntimeError:
            pass

    async def _mark_all_ranks_dirty(self) -> None:
        for group in await self.get_groups():
            await self._mark_group_rank_dirty(group.group_id)

    def _prune_rank_cache(self) -> None:
        """清理过期或过多的排行缓存，避免群数量增长后占用内存。"""
        now = time.monotonic()
        for key, item in list(self._rank_cache.items()):
            if now - item[0] >= RANK_CACHE_TTL:
                self._rank_cache.pop(key, None)
        overflow = len(self._rank_cache) - RANK_CACHE_MAX_ENTRIES
        if overflow > 0:
            oldest = sorted(
                self._rank_cache.items(),
                key=lambda pair: pair[1][0],
            )[:overflow]
            for key, _ in oldest:
                self._rank_cache.pop(key, None)
        for key, lock in list(self._rank_cache_locks.items()):
            if key not in self._rank_cache and not lock.locked():
                self._rank_cache_locks.pop(key, None)

    async def _collect_rank_rows_uncached(
        self,
        group_id: str,
        platform: str,
        *,
        progress: bool = False,
        record_metrics: bool = True,
        full_detail: bool = False,
    ):
        started = time.perf_counter()
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
                        or AccountFetchError(
                            "Codeforces 用户信息暂时无法获取"
                        ),
                    )
                    for user_id, record, identifier in records
                ]
            except Exception as exc:
                # 一个失效的 CF 账号不应让整个群排行失效，退回逐账号查询。
                logger.warning("Codeforces 批量读取失败，改为逐账号读取：%s", exc)

        if not resolved:
            semaphore = getattr(self, "_rank_fetch_semaphore", None)
            if semaphore is None:
                semaphore = asyncio.Semaphore(RANK_FETCH_CONCURRENCY)
                self._rank_fetch_semaphore = semaphore

            async def fetch_one(item):
                async with semaphore:
                    # detail 一律用轻量资料：进步榜差值改由本地历史计算，
                    # 避免全群逐个拉取平台 Rating 历史（CF 限速下可达 90 秒）。
                    return await self.account_fetcher.get_profile(
                        platform,
                        item[2],
                        detail=False,
                        include_submissions=False,
                    )

            for offset in range(0, len(records), RANK_FETCH_BATCH_SIZE):
                batch = records[
                    offset : offset + RANK_FETCH_BATCH_SIZE
                ]
                results = await asyncio.gather(
                    *(fetch_one(item) for item in batch),
                    return_exceptions=True,
                )
                resolved.extend(
                    (item[0], item[1], result)
                    for item, result in zip(batch, results)
                )

        rows = []
        errors = []
        metric_records = []
        rating_entries = []
        snapshot_requests = []
        direct_deltas = {}
        delta_calculator = getattr(
            self.account_fetcher,
            "rating_delta_for_period",
            None,
        )
        for user_id, record, result in resolved:
            if isinstance(result, Exception):
                errors.append((user_id, result))
                continue
            try:
                metric = self._profile_metric(result)
            except Exception as exc:  # noqa: BLE001 - 跳过异常账号继续排行
                errors.append((user_id, exc))
                continue
            if metric is None:
                continue
            rating_entries.append(
                (user_id, metric["snapshot_key"], metric["value"])
            )
            key = (str(user_id), metric["snapshot_key"])
            try:
                delta = (
                    delta_calculator(result, days=7)
                    if callable(delta_calculator)
                    else None
                )
            except Exception as exc:  # noqa: BLE001 - 变化值不是排行主数据
                logger.warning(
                    "计算群排行用户 %s 的本周变化失败：%s",
                    user_id,
                    exc,
                )
                delta = None
            direct_deltas[key] = delta
            if delta is None:
                snapshot_requests.append(key)
            metric_records.append(
                (user_id, record, result, metric, key)
            )

        if record_metrics:
            await self._record_rank_metrics(rating_entries)

        snapshot_deltas = {}
        bulk_delta_getter = getattr(
            self.account_registry,
            "get_weekly_deltas",
            None,
        )
        if snapshot_requests and callable(bulk_delta_getter):
            try:
                snapshot_deltas = await bulk_delta_getter(snapshot_requests)
            except Exception as exc:  # noqa: BLE001 - 没有变化值也可排行
                logger.warning("读取群排行历史快照失败：%s", exc)
        elif snapshot_requests:
            for key in snapshot_requests:
                try:
                    snapshot_deltas[key] = (
                        await self.account_registry.weekly_delta(
                            key[0],
                            key[1],
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "读取群排行用户 %s 的历史快照失败：%s",
                        key[0],
                        exc,
                    )
        if not isinstance(snapshot_deltas, dict):
            snapshot_deltas = {}

        # 进步榜：本地历史缺少 7 天前基线的成员，限量回退拉取详细资料补差值。
        # 首次计算因此只需 K 次网络请求（而非全群逐个拉取），其余成员会在
        # 后续刷新中随本地 Rating 历史积累自动补齐；用轮转保证覆盖不同成员。
        if progress and (PROGRESS_DETAIL_FALLBACK_LIMIT > 0 or full_detail):
            identifier_by_user = {
                str(user_id): identifier
                for user_id, _record, identifier in records
            }
            missing = [
                item
                for item in metric_records
                if direct_deltas.get(item[4]) is None
                and snapshot_deltas.get(item[4]) is None
            ]
            if missing:
                if full_detail:
                    # 后台刷新：没有人等待，直接补齐全部缺失成员。
                    selected = list(missing)
                    self._progress_fallback_cursor = 0
                else:
                    cursor = int(getattr(self, "_progress_fallback_cursor", 0))
                    cursor %= len(missing)
                    ordered = missing[cursor:] + missing[:cursor]
                    selected = ordered[:PROGRESS_DETAIL_FALLBACK_LIMIT]
                    self._progress_fallback_cursor = (
                        cursor + len(selected)
                    ) % max(1, len(missing))
                fallback_sem = asyncio.Semaphore(RANK_FETCH_CONCURRENCY)
                fallback_calculator = delta_calculator

                async def fetch_detail_delta(item):
                    identifier = identifier_by_user.get(str(item[0]))
                    if not identifier:
                        return None
                    async with fallback_sem:
                        profile = await self.account_fetcher.get_profile(
                            platform,
                            identifier,
                            detail=True,
                            include_submissions=False,
                        )
                    value = (
                        fallback_calculator(profile, days=7)
                        if callable(fallback_calculator)
                        else None
                    )
                    return item, profile, value

                fallback_results = await asyncio.gather(
                    *(fetch_detail_delta(item) for item in selected),
                    return_exceptions=True,
                )
                filled_entries = []
                for outcome in fallback_results:
                    if isinstance(outcome, Exception) or outcome is None:
                        continue
                    item, profile, value = outcome
                    if value is None:
                        continue
                    direct_deltas[item[4]] = value
                    try:
                        fallback_metric = self._profile_metric(profile)
                    except Exception:  # noqa: BLE001 - 仅用于补历史
                        fallback_metric = None
                    if fallback_metric is not None:
                        filled_entries.append(
                            (
                                item[0],
                                fallback_metric["snapshot_key"],
                                fallback_metric["value"],
                            )
                        )
                if record_metrics and filled_entries:
                    # 把补齐时拿到的 Rating 写入本地历史，后续刷新即可零网络。
                    await self._record_rank_metrics(filled_entries)

        for user_id, record, result, metric, key in metric_records:
            delta = direct_deltas.get(key)
            if delta is None:
                delta = snapshot_deltas.get(key)
            if progress:
                value = delta
                display_value = _format_signed_number(delta)
                sort_value = delta
                metric_label = "近7日变化"
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
                    "current_metric_label": metric["metric_label"],
                    "sort_value": sort_value,
                    "delta": delta,
                    "rating": result.rating,
                    "rating_rank": getattr(result, "rating_rank", None),
                    "rating_rank_total": getattr(result, "rating_rank_total", None),
                    "rating_rank_note": getattr(result, "rating_rank_note", "") or "",
                    "current_display_value": metric["display_value"],
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
        logger.info(
            "rank_refresh group=%s platform=%s progress=%s members=%d "
            "rows=%d errors=%d elapsed=%.2fs",
            group_id,
            platform,
            bool(progress),
            len(records),
            len(rows),
            len(errors),
            time.perf_counter() - started,
        )
        return rows, errors, rating_entries

    async def _reply_set_rank_membership(
        self, event: AstrMessageEvent, enabled: bool
    ):
        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            yield event.plain_result("群排行设置只能在群聊中使用")
            return
        user_id = str(event.get_sender_id() or "").strip()
        try:
            display_name_changed = await self.account_registry.set_user_display_name(
                user_id, self._event_display_name(event)
            )
        except Exception as exc:  # noqa: BLE001 - 昵称不是排行设置前置条件
            logger.warning("更新 %s 的排行昵称失败：%s", user_id, exc)
            display_name_changed = False
        if display_name_changed:
            self._invalidate_all_rank_cache()
        try:
            accounts = await self.account_registry.get_user_accounts(user_id)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "读取 %s 的绑定账号失败：%s",
                user_id,
                exc,
                exc_info=True,
            )
            yield event.plain_result("⚠️ 绑定数据暂时无法读取，请稍后重试")
            return
        if enabled and not accounts:
            yield event.plain_result(
                "你还没有绑定竞赛平台账号，绑定后会自动加入群排行"
            )
            return
        try:
            await self.account_registry.set_group_member(
                group_id, user_id, enabled
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "更新 %s 在群 %s 的排行状态失败：%s",
                user_id,
                group_id,
                exc,
                exc_info=True,
            )
            yield event.plain_result("⚠️ 群排行状态保存失败，请稍后重试")
            return
        self._invalidate_rank_cache(group_id)
        if enabled:
            yield event.plain_result(
                "✅ 已加入本群排行；已绑定的平台会出现在对应榜单中"
            )
        else:
            yield event.plain_result("✅ 已退出本群排行")

    @staticmethod
    def _rank_error_notice(errors) -> Optional[str]:
        """把逐账号失败汇总成一条精确提示；无失败时返回 None。

        - 含糊的“部分账号同步失败”会让用户以为插件坏了（实测反馈），
          因此明确给出：平台、账号数量、一条原因、以及影响范围。
        """
        pairs = [
            (str(platform or ""), str(error or "").strip())
            for platform, error in (errors or [])
        ]
        if not pairs:
            return None
        counts: Dict[str, int] = {}
        reasons: Dict[str, str] = {}
        for platform, error in pairs:
            counts[platform] = counts.get(platform, 0) + 1
            if error:
                reasons.setdefault(platform, error)
        parts = []
        for platform, count in sorted(
            counts.items(), key=lambda item: (-item[1], item[0])
        ):
            label = platform_label(platform)
            parts.append(f"{label} {count} 个账号" if count != 1 else f"{label} 1 个账号")
            reason = reasons.get(platform)
            if reason:
                parts.append(f"（{label}：{reason[:40]}）")
        return (
            "⚠️ 本次有账号资料同步失败："
            + "".join(parts)
            + "。常见原因是平台临时限流或账号资料暂不可读，"
            "稍后重试即可；这些账号本次不计入排行。"
        )

    @staticmethod
    def _regress_rows(rows: List[dict]) -> List[dict]:
        """本周退步榜：只保留近 7 日 Rating **下降**的成员，按下滑幅度从大到小排。

        与进步榜共用同一份「近 7 日变化」数据：进步榜是 delta 降序取正，
        退步榜是 delta 升序取负（例如 -120 排在 -30 前面）。
        没有历史基线的成员 delta 为 None，不计入。
        """
        regressed = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            delta = row.get("delta")
            try:
                value = int(delta)
            except (TypeError, ValueError):
                continue
            if value >= 0:
                continue  # 只统计下降；0 视为未变化
            item = dict(row)
            item["sort_value"] = value
            regressed.append(item)
        regressed.sort(
            key=lambda item: (
                int(item.get("sort_value") or 0),
                str(item.get("display_name") or "").casefold(),
            )
        )
        return regressed

    async def _reply_group_rank(
        self,
        event: AstrMessageEvent,
        mode: Optional[str],
        page: int = 1,
    ):
        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            yield event.plain_result("群排行只能在群聊中使用")
            return
        if page < 1:
            yield event.plain_result("排行页码必须从第 1 页开始")
            return
        if page != 1 and mode not in ACCOUNT_PLATFORMS:
            yield event.plain_result(
                "群排行总览和本周进步榜只展示各平台前 "
                f"{RANK_OVERVIEW_SIZE} 名；完整榜单请使用群cf排行、"
                "群牛客排行、群洛谷排行或群atcoder排行翻页"
            )
            return

        platforms = (
            [mode]
            if mode in ACCOUNT_PLATFORMS
            else list(ACCOUNT_PLATFORMS)
        )
        progress = mode == "progress"
        regress = mode == "regress"
        # 进步榜与退步榜共用同一份「近 7 日变化」数据（同一个 progress 快照），
        # 区别只在筛选与排序：进步=变化为正且降序，退步=变化为负且升序。
        delta_mode = progress or regress
        sections = {}
        errors = []
        for platform in platforms:
            try:
                rows, row_errors = await self.rank_service.read(
                    group_id,
                    platform,
                    progress=delta_mode,
                    record_metrics=True,
                    allow_stale=True,
                )
            except Exception as exc:  # noqa: BLE001 - 单个平台失败不阻断总览
                logger.error(
                    "读取群 %s 的 %s 排行失败：%s",
                    group_id,
                    platform_label(platform),
                    exc,
                    exc_info=True,
                )
                rows, row_errors = [], [("", exc)]
            if regress:
                rows = self._regress_rows(rows)
            sections[platform] = rows
            errors.extend((platform, error) for _, error in row_errors)

        if mode in ACCOUNT_PLATFORMS:
            rows = sections.get(mode, [])
            if not rows:
                if any(platform == mode for platform, _ in errors):
                    yield event.plain_result(
                        f"⚠️ {platform_label(mode)}排行暂时无法读取，请稍后重试"
                    )
                    return
                yield event.plain_result(
                    f"当前群还没有加入{platform_label(mode)}排行的成员"
                )
                return
            total = len(rows)
            total_pages = max(1, (total + RANK_PAGE_SIZE - 1) // RANK_PAGE_SIZE)
            if page > total_pages:
                yield event.plain_result(
                    f"{platform_label(mode)}排行没有第 {page} 页，"
                    f"当前共 {total_pages} 页（共 {total} 名成员）"
                )
                return
            start = (page - 1) * RANK_PAGE_SIZE
            end = min(start + RANK_PAGE_SIZE, total)
            page_rows = rows[start:end]
            page_command = RANK_PAGE_COMMANDS.get(
                mode,
                f"群{platform_label(mode)}排行",
            )
            navigation = []
            if page > 1:
                navigation.append(f"上一页：{page_command} {page - 1}")
            if page < total_pages:
                navigation.append(f"下一页：{page_command} {page + 1}")
            note_parts = [
                f"共 {total} 名成员 · 当前显示第 {start + 1}-{end} 名"
            ]
            metric = rank_metric_label_for_rows(
                rows,
                platform=mode,
                fallback="Rating",
            )
            metric_header = current_metric_header(metric)
            if mode == "luogu":
                note_parts.append("洛谷没有公开 Elo 时按平台公开排名排序")
            if navigation:
                note_parts.append("；".join(navigation))
            note = " · ".join(note_parts)
            title = f"本群 {platform_label(mode)} 排行 · 第 {page}/{total_pages} 页"
            image_path = await self._render_ranking_card(
                page_rows,
                title=title,
                subtitle=f"当前显示 {start + 1}-{end} / {total} 名成员 · 公开资料排行",
                metric_label=metric,
                note=note,
                platform=mode,
            )
            fallback = "\n".join(
                [
                    title,
                    f"当前显示 {start + 1}-{end} / {total} 名成员",
                    *(
                        _rank_fallback_row(i, row, metric_header)
                        for i, row in enumerate(page_rows, start + 1)
                    ),
                    f"提示：{note}",
                ]
            )
        else:
            if not any(sections.values()):
                if errors:
                    yield event.plain_result(
                        "⚠️ 群排行暂时无法读取，请稍后重试"
                    )
                elif regress:
                    yield event.plain_result(
                        "🎉 本周没有成员 Rating 下降"
                        "（也可能是成员暂无完整一周快照）"
                    )
                else:
                    yield event.plain_result("当前群还没有加入排行的成员")
                return
            if regress:
                title = "本群本周退步榜"
            elif progress:
                title = "本群本周进步榜"
            else:
                title = "本群竞赛排行总览"
            metric = "Rating" if not delta_mode else "近7日变化"
            if regress:
                note = "仅显示近 7 日 Rating 下降的成员；暂无完整一周快照的成员不计入"
            elif progress:
                note = "暂无完整一周快照的成员会暂不计入"
            else:
                note = "各平台分开排行，不直接比较不同平台 Rating"
            overview_sections = {
                platform: rows[:RANK_OVERVIEW_SIZE]
                for platform, rows in sections.items()
            }
            note += (
                f" · 总览每个平台仅显示前 {RANK_OVERVIEW_SIZE} 名，"
                "完整榜单请使用对应平台排行指令"
            )
            image_path = await self._render_overview_card(
                overview_sections,
                title=title,
                subtitle=f"四平台公开战绩矩阵 · 每个平台前 {RANK_OVERVIEW_SIZE} 名",
                metric_label=metric,
                note=note,
                secondary_label="" if delta_mode else "近7日变化",
                secondary_value_key=(
                    "current_display_value" if delta_mode else "delta"
                ),
            )
            fallback_lines = [title]
            for platform, rows in overview_sections.items():
                fallback_lines.append(f"【{platform_label(platform)}】")
                section_metric = rank_metric_label_for_rows(
                    rows,
                    platform=platform,
                    fallback="Rating",
                )
                section_metric_header = (
                    progress_metric_header(section_metric)
                    if delta_mode
                    else current_metric_header(section_metric)
                )
                for i, row in enumerate(rows, 1):
                    value = row.get("display_value", row["value"])
                    current_value = row.get(
                        "current_display_value",
                        row.get("rating"),
                    )
                    if delta_mode:
                        value_text = (
                            f"近7日变化：{value} · "
                            f"{section_metric_header}：{current_value or '—'}"
                        )
                    else:
                        value_text = (
                            f"{section_metric_header}：{value} · "
                            f"近7日变化：{_format_signed_number(row.get('delta'))}"
                        )
                    account_parts = [f"账号：{row['handle']}"]
                    rank_text = _platform_rank_text(row)
                    if rank_text:
                        account_parts.append(f"平台排名 {rank_text}")
                    fallback_lines.append(
                        f"{i}. {row['display_name']}\n"
                        f"   {' · '.join(account_parts)} · {value_text}"
                    )
            fallback_lines.append(f"提示：{note}")
            fallback = "\n".join(fallback_lines)
        if image_path is not None and image_path.is_file():
            yield event.image_result(str(image_path))
        else:
            async for result in self._adaptive_results(event, fallback):
                yield result
        notice = self._rank_error_notice(errors)
        if notice:
            yield event.plain_result(notice)

    async def _raw_groups(self, *, fresh: bool = False) -> dict:
        """读取 KV 中的群配置原始字典；非 fresh 时命中 30s 内存缓存。

        返回克隆后的字典：调用方（如 remember_group）修改返回值不会污染缓存
        之外的其他引用，缓存项本身由本方法统一重建。
        """
        now = time.monotonic()
        cached = self._groups_cache
        if (
            not fresh
            and cached is not None
            and now - cached[0] < GROUPS_CACHE_TTL_SECONDS
        ):
            return self._clone_groups(cached[1])
        raw = await self.get_kv_data("groups", {}) or {}
        if not isinstance(raw, dict):
            raw = {}
        raw, migrated = platform_compat.migrate_groups(
            raw, self._default_platform_id()
        )
        if migrated:
            await self.put_kv_data("groups", raw)
            logger.info(
                "acmerQQ群机器人 群配置已迁移到 platform_id 作用域（%d 条）",
                len(raw),
            )
        self._groups_cache = (time.monotonic(), raw)
        return self._clone_groups(raw)

    @staticmethod
    def _clone_groups(raw: dict) -> dict:
        """浅克隆群配置（值均为简单 JSON 类型，浅层复制即可）。"""
        return {
            str(gid): (dict(cfg) if isinstance(cfg, dict) else cfg)
            for gid, cfg in raw.items()
        }

    async def get_groups(self) -> List[GroupConfig]:
        raw = await self._raw_groups()
        groups: List[GroupConfig] = []
        for key, cfg in raw.items():
            if not isinstance(cfg, dict):
                continue
            platform_id, gid = platform_compat.split_scoped(str(key))
            try:
                item = dict(cfg)
                item["group_id"] = str(gid)
                if platform_id:
                    item["platform_id"] = platform_id
                groups.append(GroupConfig(**item))
            except Exception:
                logger.warning("群配置格式异常，已跳过: %s", key)
        return groups

    async def remember_group(
        self,
        group_id: str,
        platform_id: Optional[str] = None,
        umo: Optional[str] = None,
        name: str = "",
    ) -> None:
        gid = str(group_id)
        pid = str(platform_id or "")
        gname = str(name or "").strip()
        key = platform_compat.scoped_key(pid, gid)
        now = time.monotonic()
        # 常见路径：内存缓存新鲜且已注册该群、会话与群名都没变化 → 不读 KV、不写 KV。
        cached = self._groups_cache
        if cached is not None and now - cached[0] < GROUPS_CACHE_TTL_SECONDS:
            cfg = (cached[1] or {}).get(key)
            if isinstance(cfg, dict) and (not umo or cfg.get("umo") == umo):
                if not gname or str(cfg.get("name") or "") == gname:
                    return
        # 未知群/平台变化：读取权威数据后决定是否写回。
        raw = await self._raw_groups(fresh=True)
        cfg = raw.get(key)
        changed = False
        if isinstance(cfg, dict):
            if pid and cfg.get("platform_id") != pid:
                cfg["platform_id"] = pid
                changed = True
            if umo and cfg.get("umo") != umo:
                cfg["umo"] = umo
                changed = True
            if gname and str(cfg.get("name") or "") != gname:
                cfg["name"] = gname
                changed = True
        else:
            raw[key] = {
                "group_id": gid,
                "platform_id": pid,
                "name": gname,
                "umo": umo or "",
                "activated": False,
                "enabled": True,
                "morning_push_time": DEFAULT_MORNING_TIME,
                "push_platforms": list(DEFAULT_PLATFORMS),
                "reminder_enabled": True,
            }
            changed = True
        if changed:
            await self.put_kv_data("groups", raw)
            self._groups_cache = (time.monotonic(), self._clone_groups(raw))
            logger.info("acmerQQ群机器人 已自动注册群 %s（平台 %s）", gid, pid or "?")

    # ------------------------------------------------------------------
    # 主动推送（后台定时任务）
    # ------------------------------------------------------------------
    def _default_platform_id(self) -> str:
        """默认平台实例 ID：优先官方族，其次 OneBot；用于旧数据迁移。"""
        return platform_compat.resolve_platform_id(self.context) or "legacy"

    def _qq_platform_id(self) -> str:
        """兼容旧调用：解析当前可用平台实例 ID。"""
        return self._default_platform_id()

    def _session_for(
        self,
        group_id: str,
        platform_id: Optional[str] = None,
        umo: Optional[str] = None,
    ):
        """构造主动发送会话：优先使用持久化的 unified_msg_origin。"""
        if umo:
            return str(umo)
        return MessageSesion(
            platform_name=platform_id or self._default_platform_id(),
            message_type=MessageType.GROUP_MESSAGE,
            session_id=str(group_id),
        )

    def _group_scene_ready(
        self, group_id: str, platform_id: Optional[str] = None
    ) -> bool:
        """主动推送是否就绪：官方族要求该群发过消息，OneBot 直接放行。"""
        return platform_compat.scene_ready(
            self.context, str(group_id), str(platform_id or "")
        )

    async def send_notification(self, group: GroupConfig, text: str) -> bool:
        """发送通知；返回是否发送成功。@全体成员 开启且无权限时自动降级。"""
        if not self._group_scene_ready(group.group_id, group.platform_id):
            logger.warning(
                "群 %s 主动推送会话未就绪（本次运行该群还没给机器人发过消息），"
                "跳过发送；请先让群内发一条消息",
                group.group_id,
            )
            return False
        settings = await self.get_settings()
        channel = platform_compat.channel_of_platform_id(
            self.context, group.platform_id
        )
        at_all = bool(settings.get("at_all_enabled", False))
        blocked_key = "at_all_blocked_until_" + platform_compat.scoped_key(
            group.platform_id, group.group_id
        )
        blocked_until = await self.get_kv_data(blocked_key, 0.0)
        if at_all and (blocked_until or 0) < time.time():
            sent = await self._post_to_group(
                group.group_id,
                text,
                platform_id=group.platform_id,
                umo=group.umo,
                prefix=platform_compat.at_all_prefix(channel),
            )
            if sent:
                logger.info("已向群 %s 提交 @全体成员 标记", group.group_id)
                return True
            logger.warning(
                "群 %s 发送 @全体成员 失败，自动降级为普通通知", group.group_id
            )
            await self.put_kv_data(
                blocked_key,
                time.time() + AT_ALL_BLOCK_SECONDS,
            )
        sent = await self._post_to_group(
            group.group_id,
            text,
            platform_id=group.platform_id,
            umo=group.umo,
        )
        if sent:
            logger.info("已向群 %s 发送普通通知", group.group_id)
        return sent

    async def _post_to_group(
        self,
        group_id: str,
        text: str,
        platform_id: Optional[str] = None,
        umo: Optional[str] = None,
        prefix: Optional[List[Any]] = None,
    ) -> bool:
        session = self._session_for(group_id, platform_id, umo)
        prefix_components = list(prefix or [])
        try:
            value = str(text or "").strip()
            await self.get_settings()
            should_render_as_image = self.output_renderer.needs_image(value)
            chain = MessageChain(prefix_components + [Plain(value)])
            rendered_as_image = False
            if should_render_as_image:
                try:
                    image_path = await self._run_render(
                        self.output_renderer.render, value
                    )
                    if image_path is not None and image_path.is_file():
                        chain = MessageChain(
                            prefix_components
                            + [Image.fromFileSystem(str(image_path))]
                        )
                        rendered_as_image = True
                except Exception as exc:  # noqa: BLE001 - 长推送必须有文字兜底
                    logger.warning("群 %s 长通知转图片失败：%s", group_id, exc)

            if should_render_as_image and not rendered_as_image:
                # 转图不可用时按安全长度拆分，避免把超长原文直接交给平台。
                ok = await self._send_text_chunks(
                    session, value, prefix=prefix_components
                )
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
                ok = await self._send_text_chunks(
                    session, value, prefix=prefix_components
                )
            if not ok:
                logger.warning("发送到群 %s 失败：未找到匹配平台", group_id)
                return False
            return True
        except Exception as exc:
            logger.error("发送到群 %s 失败: %s", group_id, exc)
            return False

    async def _send_text_chunks(
        self, session, text: str, prefix: Optional[List[Any]] = None
    ) -> bool:
        """按安全长度发送文字分片，返回是否全部分片成功。

        单片失败仍继续尝试剩余分片（提高送达率），并记录精确失败位置，
        避免把原因都归到“未找到匹配平台”。prefix 仅加在首个分片前。
        """
        prefix_components = list(prefix or [])
        pieces = list(text_chunks(text))
        all_ok = True
        total = len(pieces)
        for index, piece in enumerate(pieces, start=1):
            components = list(prefix_components) if index == 1 else []
            try:
                ok = await self.context.send_message(
                    session, MessageChain(components + [Plain(piece)])
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "acmerQQ群机器人 文字分片第 %d/%d 条发送异常：%s",
                    index,
                    total,
                    exc,
                )
                ok = False
            if not ok:
                all_ok = False
                logger.warning(
                    "acmerQQ群机器人 文字分片第 %d/%d 条发送失败（已尝试继续后续分片）",
                    index,
                    total,
                )
        return all_ok

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
        daily = await self._daily_problem_lines(group, settings)
        if daily:
            lines.extend(daily)
        return "\n".join(lines)

    async def _daily_problem_lines(
        self, group: GroupConfig, settings: dict
    ) -> List[str]:
        """早报末尾的"今日一题"行；关闭开关或题目池不可用时返回空列表。"""
        if not settings.get("daily_problem_enabled", True):
            return []
        platform = settings["daily_problem_platform"]
        if platform not in group.push_platforms and platform not in settings["push_platforms"]:
            return []
        count = int(settings.get("daily_problem_count") or 1)
        day = datetime.now(CN_TZ).strftime("%Y-%m-%d")
        try:
            problem = await self._daily_problem_for_group(
                group.group_id, platform, day
            )
        except Exception as exc:  # noqa: BLE001 - 抽题失败不影响早报
            logger.warning("群 %s 抽取每日一题失败：%s", group.group_id, exc)
            return []
        if problem is None:
            return []
        lines = [
            f"— 🎯 今日一题（{PLATFORM_LABELS.get(platform, platform)}）—",
            f"{problem.display()}",
            problem.url,
        ]
        del count  # 目前固定 1 题：多题会拉长早报，保留配置位供后续扩展
        return lines

    async def build_weekly_board_cards(
        self, group: GroupConfig
    ) -> List[Dict[str, Any]]:
        """渲染「本周进步榜」「本周退步榜」两张总览卡（供早报追加推送）。

        - 与交互指令 `本周进步榜` / `本周退步榜` 完全相同的卡片；
        - 复用 progress 快照，**不额外抓取 OJ 数据**；
        - 无数据的榜单不返回；卡片渲染失败时带纯文本兜底（image=None）。
        """
        settings = await self.get_settings()
        platforms = [
            p for p in settings["push_platforms"] if p in group.push_platforms
        ] or list(DEFAULT_PLATFORMS)
        progress_sections: Dict[str, list] = {}
        regress_sections: Dict[str, list] = {}
        for platform in platforms:
            try:
                rows, _errors = await self.rank_service.read(
                    group.group_id,
                    platform,
                    progress=True,
                    record_metrics=True,
                    allow_stale=True,
                )
            except Exception as exc:  # noqa: BLE001 - 单平台失败不影响早报
                logger.warning(
                    "构建群 %s 的 %s 周榜失败：%s",
                    group.group_id,
                    platform,
                    exc,
                )
                continue
            # 与交互指令保持一致：进步榜取快照前 N（按变化降序），
            # 退步榜取下降成员前 N（按跌幅降序）。
            if rows:
                progress_sections[platform] = rows[:RANK_OVERVIEW_SIZE]
            regressed = self._regress_rows(rows)[:RANK_OVERVIEW_SIZE]
            if regressed:
                regress_sections[platform] = regressed

        boards: List[Dict[str, Any]] = []
        for title, sections, hint in (
            ("本群本周进步榜", progress_sections, "近 7 日 Rating 进步最多的成员"),
            ("本群本周退步榜", regress_sections, "近 7 日 Rating 下降最多的成员"),
        ):
            if not sections:
                continue
            note = (
                f"{hint} · 暂无完整一周快照的成员不计入 · "
                f"每个平台仅前 {RANK_OVERVIEW_SIZE} 名"
            )
            image_path = await self._render_overview_card(
                sections,
                title=title,
                subtitle=(
                    f"四平台公开战绩矩阵 · 每个平台前 {RANK_OVERVIEW_SIZE} 名"
                ),
                metric_label="近7日变化",
                note=note,
                secondary_label="",
                secondary_value_key="current_display_value",
            )
            boards.append(
                {
                    "title": title,
                    "image": image_path,
                    "text": self._weekly_board_text(title, sections, note),
                }
            )
        return boards

    @staticmethod
    def _weekly_board_text(
        title: str, sections: Dict[str, list], note: str = ""
    ) -> str:
        """周榜卡片的纯文本兜底（卡片渲染不可用时发送）。"""
        icon = "📉" if "退步" in title else "📈"
        lines = [f"{icon} {title}"]
        for platform, rows in sections.items():
            lines.append(f"【{platform_label(platform)}】")
            for index, row in enumerate(rows, 1):
                name = (
                    str(row.get("display_name") or "").strip()
                    or str(row.get("handle") or "未知成员")
                )
                current = (
                    row.get("current_display_value")
                    or row.get("display_value")
                    or "—"
                )
                lines.append(
                    f"{index}. {name} "
                    f"{_format_signed_number(row.get('delta'))}"
                    f"（当前 {current}）"
                )
        if note:
            lines.append(f"提示：{note}")
        return "\n".join(lines)

    async def _send_group_image(
        self, group: GroupConfig, image_path, *, caption: str = ""
    ) -> bool:
        """主动向群发送一张图片（用于周榜卡片）；失败返回 False。"""
        if not self._group_scene_ready(group.group_id, group.platform_id):
            logger.warning(
                "群 %s 主动推送会话未就绪，跳过图片推送（%s）",
                group.group_id,
                caption,
            )
            return False
        session = self._session_for(group.group_id, group.platform_id, group.umo)
        chain = MessageChain([Image.fromFileSystem(str(image_path))])
        try:
            ok = await self.context.send_message(session, chain)
        except Exception as exc:  # noqa: BLE001 - 图片推送失败由调用方决定后续
            logger.error(
                "群 %s 图片推送异常（%s）：%s", group.group_id, caption, exc
            )
            return False
        if not ok:
            logger.warning(
                "群 %s 图片推送失败（%s，可能未找到匹配平台）",
                group.group_id,
                caption,
            )
        return bool(ok)

    async def push_weekly_boards(self, group: GroupConfig) -> bool:
        """推送两张周榜卡：先进步榜、后退步榜。图片优先，渲染失败回退文字。"""
        boards = await self.build_weekly_board_cards(group)
        if not boards:
            return True  # 没有可推送的周榜数据，不算失败
        all_ok = True
        for board in boards:
            title = str(board.get("title") or "")
            image_path = board.get("image")
            if image_path is not None and Path(image_path).is_file():
                ok = await self._send_group_image(
                    group, image_path, caption=title
                )
            else:
                ok = await self.send_notification(
                    group, str(board.get("text") or "")
                )
            if ok:
                logger.info("群 %s 已推送 %s", group.group_id, title)
            else:
                all_ok = False
                logger.warning("群 %s 的 %s 推送失败", group.group_id, title)
        return all_ok

    # ------------------------------------------------------------------
    # 每日一题 / 推荐补题（A2）
    # ------------------------------------------------------------------
    async def _group_rating_anchor(
        self, group_id: str, platform: str
    ) -> Optional[int]:
        """群内该平台 Rating 的中位数（用作抽题难度锚点）；取不到返回 None。

        只读排行快照（stale-while-revalidate）：冷群会退化为一次常规排行计算，
        与用户主动查 `群xx排行` 的成本一致。
        """
        if not group_id:
            return None
        try:
            rows, _errors = await self.rank_service.read(
                group_id, platform, record_metrics=False, allow_stale=True
            )
        except Exception as exc:  # noqa: BLE001 - 锚点缺失时用默认档
            logger.warning("读取群 %s 的 %s 排行锚点失败：%s", group_id, platform, exc)
            return None
        values = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            for key in ("rating", "sort_value"):
                value = row.get(key)
                if isinstance(value, int):
                    values.append(value)
                    break
        if not values:
            return None
        values.sort()
        return values[len(values) // 2]

    def _cached_solved_ids(self, platform: str, identifiers: List[str]) -> List[List[str]]:
        """只收集"分析缓存里已有"的成员已通过题（不为抽题额外抓取）。"""
        fetcher = getattr(self, "account_fetcher", None)
        if fetcher is None:
            return []
        out: List[List[str]] = []
        for identifier in identifiers:
            key = (platform, str(identifier).casefold(), True, False, True)
            cached = fetcher._cache.get(key)
            if not cached:
                continue
            analysis = getattr(cached[1], "analysis", None) or {}
            solved = analysis.get("solved_problem_ids") or []
            if solved:
                out.append(list(solved))
        return out

    async def _daily_problem_for_group(
        self, group_id: str, platform: str, day: str
    ):
        """取（必要时抽取并缓存）当天的每日一题；索引不可用时返回 None。"""
        pool = await self.problem_service.ensure_index(platform)
        if not pool:
            return None
        cache_key = f"daily_{group_id or 'private'}_{day}"
        cached = await self.get_kv_data(cache_key, {}) or {}
        problem_id = str(cached.get("problem_id") or "")
        if problem_id:
            for problem in pool:
                if problem.problem_id == problem_id:
                    return problem
        anchor = await self._group_rating_anchor(group_id, platform)
        solved_sets = []
        if group_id:
            members = await self._settlement_members(group_id, platform)
            solved_sets = self._cached_solved_ids(
                platform, [handle for _uid, _name, handle in members]
            )
        problem = self.problem_service.pick_daily(
            group_id=group_id or "private",
            day=day,
            pool=pool,
            rating=anchor,
            exclude=self.problem_service._exclude_ids(solved_sets),
        )
        if problem is None:
            return None
        await self.put_kv_data(
            cache_key,
            {"problem_id": problem.problem_id, "platform": platform},
        )
        return problem

    async def _reply_daily_problem(self, event: AstrMessageEvent):
        """`每日一题`：与早报同源同一题（KV 缓存保证一致）。"""
        settings = await self.get_settings()
        platform = settings["daily_problem_platform"]
        group_id = str(event.get_group_id() or "").strip()
        day = datetime.now(CN_TZ).strftime("%Y-%m-%d")
        problem = await self._daily_problem_for_group(group_id, platform, day)
        if problem is None:
            async for result in self._adaptive_results(
                event,
                "🎯 题目池尚未就绪（题库索引构建中或该平台不可用），请稍后再试\n"
                "可在 WebUI 切换「每日一题平台」为牛客（本地索引，最快）。",
            ):
                yield result
            return
        lines = [
            f"🎯 今日一题（{PLATFORM_LABELS.get(platform, platform)} · {day}）",
            problem.display(),
            problem.url,
        ]
        async for result in self._adaptive_results(event, "\n".join(lines)):
            yield result

    async def _maybe_recommend_problems(self, event, platform: str, profile):
        """详细资料卡之后追加"推荐补题"（仅当该平台有已通过题集合）。"""
        settings = await self.get_settings()
        if not settings.get("recommend_enabled", True):
            return
        analysis = getattr(profile, "analysis", None) or {}
        solved = analysis.get("solved_problem_ids") or []
        if not solved:
            return
        pool = await self.problem_service.ensure_index(platform)
        if not pool:
            return
        picks = self.problem_service.recommend(
            pool=pool,
            solved=solved,
            weak_tags=weak_tags_from_analysis(analysis, limit=3),
            rating=getattr(profile, "rating", None),
            limit=RECOMMEND_PROBLEM_LIMIT,
        )
        if not picks:
            return
        lines = ["🎯 推荐补题（都是你还没通过的题）"]
        for index, problem in enumerate(picks, start=1):
            lines.append(f"{index}. {problem.display()}\n   {problem.url}")
        async for result in self._adaptive_results(event, "\n".join(lines)):
            yield result

    # ------------------------------------------------------------------
    # 赛后赛果推送（A1）
    # ------------------------------------------------------------------
    async def _render_settlement_card(
        self,
        sections,
        *,
        title: str,
        subtitle: str,
        note: str = "",
        platform_order=None,
    ):
        """渲染赛果卡；失败时返回 None，由调用方发送纯文本。"""
        try:
            return await self._run_render(
                self.account_card_renderer.render_settlement,
                sections,
                title=title,
                subtitle=subtitle,
                note=note,
                platform_order=platform_order,
            )
        except Exception as exc:  # noqa: BLE001 - UI 失败不能阻断推送
            logger.error("赛果卡渲染失败，改用文字：%s", exc, exc_info=True)
            return None

    async def _settlement_members(
        self, group_id: str, platform: str
    ) -> List[tuple]:
        """本群已绑定该平台的成员：[(user_id, display_name, handle)]。"""
        member_ids = await self.account_registry.get_group_member_ids(group_id)
        accounts = await self.account_registry.get_all_accounts()
        members: List[tuple] = []
        for user_id in member_ids:
            record = accounts.get(user_id, {}).get(platform)
            if not isinstance(record, dict):
                continue
            handle = str(
                record.get("platform_user_id") or record.get("handle") or ""
            ).strip()
            if not handle:
                continue
            display = str(
                record.get("display_name") or record.get("handle") or handle
            ).strip()
            members.append((str(user_id), display, handle))
        return members

    @staticmethod
    def _settlement_text(result) -> str:
        """赛果卡的纯文本兜底（渲染不可用时使用）。"""
        lines = [f"🏁 {result.contest_name} 赛果"]
        for row in result.rows:
            rank = f"#{row.rank}" if row.rank else "—"
            solved = (
                f"{row.solved}/{row.total_problems} 题"
                if row.solved is not None and row.total_problems
                else "—"
            )
            count = f" · {row.user_count} 人参赛" if row.user_count else ""
            lines.append(f"{rank} {row.display_name}（{row.handle}） {solved}{count}")
        if result.extra_note:
            lines.append(result.extra_note)
        if result.note:
            lines.append(f"📚 {result.note}")
        lines.append("ℹ️ 评分变化以平台为准")
        return "\n".join(lines)

    async def tick_settlements(self, now: Optional[datetime] = None) -> int:
        """赛后赛果巡检（由 scheduler 每 tick 调用）；返回实际推送的群次数。"""
        settings = await self.get_settings()
        if not settings.get("settle_push_enabled", True):
            return 0
        moment = now or datetime.now(timezone.utc)
        delay_minutes = int(settings.get("settle_delay_minutes") or 0)
        min_participants = int(settings.get("settle_min_participants") or 1)
        show_unsolved = bool(settings.get("settle_show_unsolved", True))
        pushed = 0
        for group in await self.get_groups():
            if not group.enabled or not getattr(
                group, "settle_push_enabled", True
            ):
                continue
            platforms = [
                platform
                for platform in settings["push_platforms"]
                if platform in group.push_platforms
            ]
            for platform in platforms:
                if platform not in SETTLE_PLATFORMS:
                    continue
                # 赛程接口只返回"未开始"的比赛：把"上一次缓存里的赛程"与
                # "本次抓取到的赛程"都记入最近比赛记录，结束后再从记录里挑候选
                # （否则比赛一结束就从列表消失，永远结算不到）。
                previous = self.fetcher._cache.get(platform)
                if previous and isinstance(previous[1], list):
                    self.settlement.remember_contests(platform, previous[1])
                contests, err = await self.fetcher.fetch_platform(platform)
                if contests:
                    self.settlement.remember_contests(platform, contests)
                if err and not contests:
                    continue
                candidates = self.settlement.settlement_candidates(
                    platform, moment, delay_minutes
                )
                if candidates:
                    logger.info(
                        "赛后赛果巡检：%s 有 %d 场刚结束待结算（%s）",
                        platform,
                        len(candidates),
                        "、".join(
                            f"{item.contest_id} {item.name[:24]}"
                            for item in candidates
                        ),
                    )
                for contest in candidates:
                    key = f"settle_{group.group_id}_{platform}_{contest.contest_id}"
                    if await self.get_kv_data(key, False):
                        continue
                    try:
                        pushed += await self._push_settlement(
                            group,
                            platform,
                            contest,
                            key,
                            min_participants=min_participants,
                            show_unsolved=show_unsolved,
                            platform_order=list(platforms),
                        )
                    except Exception as exc:  # noqa: BLE001 - 单场失败不影响其他群/平台
                        logger.warning(
                            "群 %s 的 %s %s 赛果处理失败：%s",
                            group.group_id,
                            platform,
                            contest.contest_id,
                            exc,
                        )
        try:
            self.settlement.save_recent_contests()
        except Exception as exc:  # noqa: BLE001 - 落盘失败不影响推送
            logger.warning("保存最近比赛记录失败：%s", exc)
        return pushed

    async def _push_settlement(
        self,
        group: GroupConfig,
        platform: str,
        contest,
        key: str,
        *,
        min_participants: int,
        show_unsolved: bool,
        platform_order: List[str],
        write_key: bool = True,
    ) -> int:
        """处理单场赛果：采集 → 渲染 → 推送 → 写幂等键；返回 1/0。

        write_key=False 供后台“立即试跑”使用：只推送、不写幂等键，
        避免挤掉当天正式的赛后赛果推送。
        """
        members = await self._settlement_members(group.group_id, platform)
        if len(members) < max(1, min_participants):
            logger.info(
                "群 %s 跳过 %s %s 赛果：本群绑定该平台的成员 %d 人，低于阈值 %d",
                group.group_id,
                platform,
                contest.contest_id,
                len(members),
                max(1, min_participants),
            )
            return 0
        result = await self.settlement.collect(platform, contest, members)
        if result is None:
            logger.info(
                "群 %s 跳过 %s %s 赛果：赛果采集失败（接口异常或平台未公开）",
                group.group_id,
                platform,
                contest.contest_id,
            )
            return 0
        if not result.has_content():
            logger.info(
                "群 %s 跳过 %s %s 赛果：本群 %d 名绑定成员均未参加该场比赛",
                group.group_id,
                platform,
                contest.contest_id,
                len(members),
            )
            return 0
        sections = {platform: [row.to_card_row() for row in result.rows]}
        if show_unsolved:
            unsolved = sorted(
                {label for row in result.rows for label in row.unsolved}
            )
            if unsolved:
                extra = "本场未通过：" + "、".join(unsolved)
                result.note = f"{result.note} · {extra}" if result.note else extra
        if result.extra_note:
            result.note = (
                f"{result.note} · {result.extra_note}"
                if result.note
                else result.extra_note
            )
        note = (
            f"{result.note} · 评分变化以平台为准"
            if result.note
            else "评分变化以平台为准"
        )
        title = f"🏁 {result.contest_name} 赛果"
        subtitle = f"本群 {len(result.rows)} 人参赛"
        image_path = None
        if sections:
            image_path = await self._render_settlement_card(
                sections,
                title=title,
                subtitle=subtitle,
                note=note,
                platform_order=platform_order,
            )
        if image_path is not None and Path(image_path).is_file():
            ok = await self._send_group_image(group, image_path, caption=title)
        else:
            ok = await self.send_notification(
                group, self._settlement_text(result)
            )
        if not ok:
            logger.warning(
                "群 %s 的 %s 赛果推送失败，下个周期重试",
                group.group_id,
                platform,
            )
            return 0
        if write_key and key:
            await self.put_kv_data(key, True)
        await self._log_push(
            group.group_id,
            "settle",
            True,
            f"{platform} {contest.contest_id}"
            + ("" if write_key else "（试跑）"),
        )
        logger.info(
            "群 %s 已推送 %s %s 赛果（%d 人%s）",
            group.group_id,
            platform,
            contest.contest_id,
            len(result.rows),
            "" if write_key else "，试跑未写幂等键",
        )
        return 1

    # ------------------------------------------------------------------
    # 群训练周报（A3）
    # ------------------------------------------------------------------
    @staticmethod
    def _iso_week_key(moment: datetime) -> str:
        """ISO 周键：``2026-W38``（周一为一周之始，跨年由 isocalendar 处理）。"""
        iso = moment.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"

    @staticmethod
    def _delta_rows(rows) -> List[dict]:
        """从进步榜行里挑出「有近 7 日变化」的成员（delta 为 None 的不计）。"""
        picked: List[dict] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            try:
                delta = int(row.get("delta"))
            except (TypeError, ValueError):
                continue
            picked.append(
                {
                    "user_id": str(row.get("user_id") or ""),
                    "display_name": str(
                        row.get("display_name")
                        or row.get("handle")
                        or "未知成员"
                    ),
                    "handle": str(row.get("handle") or ""),
                    "delta": delta,
                }
            )
        return picked

    async def build_weekly_report(
        self, group: GroupConfig
    ) -> Optional[dict]:
        """构建群训练周报：``{"cards": [...], "text": "..."}``；无数据返回 None。

        - 卡片**直接复用** ``build_weekly_board_cards()``（进步榜/退步榜两张现有卡）；
        - 文字统计来自 ``rank_service.read(progress=True)``（本地快照，零网络）
          与 ``src/weekly_stats.py``（CF/AtCoder 近 7 日活跃，1~2 请求/成员）；
        - 牛客/洛谷不统计活跃，文案如实标注。
        """
        settings = await self.get_settings()
        platforms = [
            p for p in settings["push_platforms"] if p in group.push_platforms
        ] or list(DEFAULT_PLATFORMS)
        moment = datetime.now(CN_TZ)
        since = moment - timedelta(days=WEEKLY_WINDOW_DAYS)

        # 1) 本地零请求：进步榜与退步榜共用同一份「近 7 日变化」快照。
        deltas: List[dict] = []
        for platform in platforms:
            try:
                rows, _errors = await self.rank_service.read(
                    group.group_id,
                    platform,
                    progress=True,
                    record_metrics=False,
                    allow_stale=True,
                )
            except Exception as exc:  # noqa: BLE001 - 单平台失败不影响周报
                logger.warning(
                    "周报读取群 %s 的 %s 进步榜失败：%s",
                    group.group_id,
                    platform,
                    exc,
                )
                continue
            for item in self._delta_rows(rows):
                item["platform"] = platform
                deltas.append(item)

        # 2) 轻量活跃统计：只覆盖 CF / AtCoder（牛客/洛谷无等价轻量接口）。
        activity_days = 0
        submissions = 0
        activity_records = 0
        active_users: set = set()
        for platform in ("codeforces", "atcoder"):
            if platform not in platforms:
                continue
            try:
                members = await self._settlement_members(
                    group.group_id, platform
                )
            except Exception as exc:  # noqa: BLE001 - 活跃统计失败不阻塞周报
                logger.warning(
                    "周报读取群 %s 的 %s 成员失败：%s",
                    group.group_id,
                    platform,
                    exc,
                )
                continue
            if not members:
                continue
            activities = await collect_weekly_activity(
                self, platform, members, since.timestamp()
            )
            for user_id, activity in activities.items():
                if activity.source == "unavailable":
                    continue
                activity_records += 1
                activity_days += int(activity.active_days)
                submissions += int(activity.submissions)
                if activity.submissions > 0:
                    active_users.add(user_id)

        # 3) 参与人数：本周有活跃或有 Rating 变化的成员。
        participants = {
            item["user_id"] for item in deltas if item["delta"] != 0
        } | active_users
        if not participants:
            logger.info("群 %s 本周无训练数据，跳过周报", group.group_id)
            return None

        lines = [
            f"📊 本周训练周报（{since:%m-%d} ~ {moment:%m-%d}）",
            f"👥 参与人数：{len(participants)} 人",
        ]
        if activity_records:
            lines.append(
                f"🔥 人均活跃天数：{activity_days / activity_records:.1f} 天"
                "（CF+AtCoder，按绑定账号计）"
            )
            lines.append(
                f"📝 人均提交：{submissions / activity_records:.1f} 次"
                "（CF+AtCoder）"
            )
        else:
            lines.append(
                "🔥 活跃数据：本周未取得 CF/AtCoder 活跃统计，仅统计 Rating 变化"
            )
        best = max(deltas, key=lambda item: item["delta"]) if deltas else None
        worst = min(deltas, key=lambda item: item["delta"]) if deltas else None
        if best is not None and best["delta"] > 0:
            lines.append(
                f"📈 进步最多：{best['display_name']} "
                f"{_format_signed_number(best['delta'])}"
                f"（{platform_label(best['platform'])}）"
            )
        if worst is not None and worst["delta"] < 0:
            lines.append(
                f"📉 退步最多：{worst['display_name']} "
                f"{_format_signed_number(worst['delta'])}"
                f"（{platform_label(worst['platform'])}）"
            )
        lines.append(
            "📚 数据源：本地 Rating 快照 + CF user.status / AtCoder kenkoooo；"
            "牛客、洛谷本周报不统计活跃"
        )

        cards: List[Dict[str, Any]] = []
        try:
            cards = await self.build_weekly_board_cards(group)
        except Exception as exc:  # noqa: BLE001 - 卡片失败仍有文字统计
            logger.warning("群 %s 周报卡片渲染失败：%s", group.group_id, exc)
        return {"cards": cards, "text": "\n".join(lines)}

    async def tick_weekly_report(
        self, now: Optional[datetime] = None
    ) -> int:
        """群训练周报巡检（由 scheduler 每 tick 调用）；返回实际推送的群数。

        触发条件：``weekly_report_enabled`` 开启 + 今天是指定星期 +
        当前 HH:MM 等于 ``weekly_report_time``；幂等键 ``weekly_<群ID>_<ISO周>``
        （发送成功才写键，失败下一 tick 重试）。
        """
        settings = await self.get_settings()
        if not settings.get("weekly_report_enabled", True):
            return 0
        moment = now or datetime.now(CN_TZ)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=CN_TZ)
        moment = moment.astimezone(CN_TZ)
        weekday = int(
            settings.get("weekly_report_weekday")
            or DEFAULT_WEEKLY_REPORT_WEEKDAY
        )
        if moment.isoweekday() != weekday:
            return 0
        try:
            push_time = validate_hhmm(
                settings.get("weekly_report_time") or DEFAULT_WEEKLY_REPORT_TIME
            )
        except ValueError:
            return 0
        if moment.strftime("%H:%M") != push_time:
            return 0

        week_key = self._iso_week_key(moment)
        pushed = 0
        for group in await self.get_groups():
            if not group.enabled:
                continue
            if await self._push_weekly_report_for_group(
                group, moment, week_key=week_key
            ):
                pushed += 1
        return pushed

    async def _push_weekly_report_for_group(
        self,
        group: GroupConfig,
        moment: datetime,
        *,
        week_key: str = "",
        write_key: bool = True,
    ) -> bool:
        """推送单个群的训练周报；返回是否送达。

        write_key=False 供后台“立即试跑”使用：不写 weekly 幂等键。
        """
        week = week_key or self._iso_week_key(moment)
        key = f"weekly_{group.group_id}_{week}"
        if write_key and await self.get_kv_data(key, False):
            return False
        try:
            report = await self.build_weekly_report(group)
        except Exception as exc:  # noqa: BLE001 - 单群失败不影响其他群
            logger.warning("群 %s 周报构建失败：%s", group.group_id, exc)
            return False
        if not report:
            return False
        text = str(report.get("text") or "").strip()
        text_sent = True
        if text:
            text_sent = await self.send_notification(group, text)
        if not text_sent:
            logger.warning("群 %s 周报文字推送失败，下个周期重试", group.group_id)
            return False
        cards_ok = True
        sent_cards = 0
        for card in report.get("cards") or []:
            title = str(card.get("title") or "本周训练周报")
            image_path = card.get("image")
            if image_path is not None and Path(image_path).is_file():
                ok = await self._send_group_image(
                    group, image_path, caption=title
                )
            else:
                ok = await self.send_notification(
                    group, str(card.get("text") or "")
                )
            if ok:
                sent_cards += 1
            else:
                cards_ok = False
                logger.warning("群 %s 的 %s 推送失败", group.group_id, title)
        if not text and sent_cards == 0:
            # 既没有文字也没有卡片 → 视为未送达，不写幂等键。
            logger.warning("群 %s 周报无可推送内容，跳过", group.group_id)
            return False
        if write_key:
            await self.put_kv_data(key, True)
        try:
            await self._log_push(
                group.group_id,
                "weekly_report",
                True,
                "试跑" if not write_key else week,
            )
        except Exception:  # noqa: BLE001 - 日志失败不影响推送
            pass
        logger.info(
            "群 %s 已推送训练周报（%s，文字=%s，卡片=%d%s）",
            group.group_id,
            week,
            "是" if text else "否",
            sent_cards,
            "" if cards_ok else "，部分卡片失败",
        )
        return True

    # ------------------------------------------------------------------
    # 报名截止提醒（A4）
    # ------------------------------------------------------------------
    @staticmethod
    def _signup_reminder_text(
        contest, deadline: datetime, remaining_seconds: float
    ) -> str:
        """报名截止提醒文案：比赛名 + 北京时间截止 + 剩余时长 + 链接。"""
        if remaining_seconds >= 3600:
            hours = f"{remaining_seconds / 3600:.1f}".rstrip("0").rstrip(".")
            left = f"还有 {hours} 小时"
        else:
            minutes = max(1, int(remaining_seconds // 60))
            left = f"还有 {minutes} 分钟"
        return (
            f"📝 报名即将截止：{contest.name}\n"
            f"🕐 截止 {deadline.astimezone(CN_TZ):%Y-%m-%d %H:%M}（北京时间）\n"
            f"⏳ {left}\n"
            f"🔗 {contest.url}"
        )

    async def tick_signup_reminders(
        self, now: Optional[datetime] = None
    ) -> int:
        """牛客报名截止提醒（由 scheduler 每 tick 调用）；返回提醒的比赛数。

        - 只提醒"未开始且报名截止在 ``[now, now+24h]``（或 ``[now, now+2h]``）"的比赛；
        - 幂等键 ``signup_<contestID>_<24h|2h>`` 是**全局键**（不按群）：
          报名截止对所有群一样，避免每个群各推一次造成重复打扰；
        - 只向"推送平台包含 nowcoder"的群发送；发送成功才写键。
        """
        groups = [
            group
            for group in await self.get_groups()
            if group.enabled and "nowcoder" in (group.push_platforms or [])
        ]
        if not groups:
            return 0
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        moment = moment.astimezone(timezone.utc)
        try:
            contests, err = await self.fetcher.fetch_platform("nowcoder")
        except Exception as exc:  # noqa: BLE001 - 抓取异常不能打断 tick
            logger.warning("报名提醒读取牛客赛程失败：%s", exc)
            return 0
        if err or not contests:
            return 0

        reminded = 0
        for contest in contests:
            deadline = getattr(contest, "signup_end_time", None)
            if not isinstance(deadline, datetime):
                continue
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            deadline = deadline.astimezone(timezone.utc)
            start = getattr(contest, "start_time", None)
            if not isinstance(start, datetime):
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if start <= moment:
                continue  # 已开始的比赛不再提醒报名
            remaining = (deadline - moment).total_seconds()
            if remaining < 0:
                continue
            # 取"当前适用"的最小档位：剩余 ≤2h 发 2h 档，否则发 24h 档；
            # 超出 24h 不提醒。这样插件晚启动时不会同一 tick 连发两条。
            tier = None
            for name, hours in sorted(
                SIGNUP_REMINDER_TIERS, key=lambda item: item[1]
            ):
                if remaining <= hours * 3600:
                    tier = name
                    break
            if tier is None:
                continue
            key = f"signup_{contest.contest_id}_{tier}"
            if await self.get_kv_data(key, False):
                continue
            text = self._signup_reminder_text(contest, deadline, remaining)
            sent = False
            for group in groups:
                if await self._send_signup_text(group, text):
                    sent = True
            if not sent:
                logger.warning(
                    "牛客比赛 %s 报名提醒发送失败，下个周期重试",
                    contest.contest_id,
                )
                continue
            await self.put_kv_data(key, True)
            await self._log_push(
                "", "signup", True, f"{contest.contest_id} {tier}"
            )
            reminded += 1
            logger.info(
                "已推送牛客比赛 %s 的报名截止提醒（%s，剩余 %.1f 小时，%d 个群）",
                contest.contest_id,
                tier,
                max(0.0, remaining / 3600),
                len(groups),
            )
        return reminded

    async def _send_signup_text(self, group: GroupConfig, text: str) -> bool:
        """向单个群发送报名提醒；异常按失败处理，不打断其他群。"""
        try:
            return bool(await self.send_notification(group, text))
        except Exception as exc:  # noqa: BLE001 - 单群失败不影响其他群
            logger.warning("群 %s 报名提醒发送异常：%s", group.group_id, exc)
            return False

    async def build_test_text(self, group: GroupConfig) -> str:
        """测试推送内容：优先今日早报，今日无比赛时展示最近一场。

        与真实早报保持一致：末尾同样附加本周进步榜/退步榜，
        方便在 WebUI 里直接预览早报的完整形态。
        """
        boards = ""
        try:
            boards = await self.build_weekly_boards_text(group)
        except Exception:  # noqa: BLE001 - 预览周榜失败不影响测试推送
            logger.warning("构建测试推送周榜失败", exc_info=True)
            boards = ""
        parts: List[str] = []
        morning = await self.build_morning_text(group)
        if morning:
            parts.append(morning)
        else:
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
            parts.append("\n".join(lines))
        if boards:
            parts.append(boards)
        return "\n\n".join(parts)

    async def _adaptive_results(
        self, event: AstrMessageEvent, text: str
    ):
        """短结果发文字，长结果转 PNG；渲染失败时拆分为多条文字。"""
        value = str(text or "").strip()
        if not value:
            return
        # 同步应用已缓存的阈值配置（不 await，避免丢换行）。
        self._apply_cached_renderer_settings()
        if not self.output_renderer.needs_image(value):
            # 短文本直接整段发送；不要在 yield 前 await get_settings，
            # 否则 AstrBot 被动回复链路会丢失 Plain 内的换行（实测）。
            if len(value) > MAX_TEXT_CHUNK:
                for piece in text_chunks(value):
                    yield event.plain_result(piece)
            else:
                yield event.plain_result(value)
            return

        # HTML/浏览器调用是阻塞操作，放到线程中（受全局渲染信号量约束），
        # 避免卡住 AstrBot 事件循环或同时拉起多个浏览器。
        await self.get_settings()
        try:
            image_path = await self._run_render(
                self.output_renderer.render, value
            )
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

    @filter.platform_adapter_type(
        filter.PlatformAdapterType.QQOFFICIAL
        | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
        | filter.PlatformAdapterType.AIOCQHTTP
    )
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
        # 官方通道“必须先 @机器人 才会响应”，而机器人的 @ 可能是占位符且留在正文里。
        # 统一清掉 @ 记号后再匹配指令，保证“@机器人 + 指令”也能被识别。
        command_text = self._strip_any_mention_tokens(raw_message)
        message_str = normalize_command(command_text)
        mention_target = self._mentioned_profile_target(event, raw_message)
        if mention_target is not None:
            async for result in self._reply_my_account(
                event,
                target_user_id=mention_target["user_id"],
                target_display_name=mention_target["display_name"],
            ):
                yield result
            return
        admin_bind = self._mentioned_admin_bind(event, raw_message)
        if admin_bind is not None:
            async for result in self._reply_admin_bind(event, admin_bind):
                yield result
            return
        if self._literal_at_admin_bind(event, raw_message) is not None:
            async for result in self._reply_admin_bind_mention_hint(event):
                yield result
            return
        if not message_str:
            return
        bind_match = ACCOUNT_BIND_RE.match(command_text)
        if bind_match:
            platform = normalize_platform(bind_match.group(1))
            if platform:
                async for result in self._reply_account_bind(
                    event, platform, bind_match.group(2)
                ):
                    yield result
            return
        bind_usage_match = ACCOUNT_BIND_USAGE_RE.match(command_text)
        if bind_usage_match:
            platform = normalize_platform(bind_usage_match.group(1))
            if platform:
                command, argument_hint, field = ACCOUNT_BIND_USAGE_HINTS.get(
                    platform,
                    (f"绑定{platform}", "<账号>", "对应公开资料字段"),
                )
                sample = ACCOUNT_BIND_EXAMPLES.get(
                    platform, f"{command} <账号>"
                )
                yield event.plain_result(
                    f"用法：{command} {argument_hint}\n"
                    f"例如：{sample}\n"
                    f"请填写账号后再发送，不能只发送“{command}”。\n"
                    f"绑定后请按提示把验证码追加到【{field}】，"
                    "再发送确认绑定指令。"
                )
            return
        if ACCOUNT_CONFIRM_USAGE_RE.match(command_text):
            yield event.plain_result(
                "用法：确认绑定cf/确认绑定牛客/确认绑定洛谷/确认绑定atcoder <验证码>\n"
                "例如：确认绑定cf ACM-ABCDEFGH\n"
                "请先发送绑定指令拿到验证码，把它填进对应平台的公开资料字段后再确认。"
            )
            return
        if ACCOUNT_UNBIND_USAGE_RE.match(command_text):
            yield event.plain_result(
                "用法：解绑cf / 解绑牛客 / 解绑洛谷 / 解绑atcoder\n"
                "例如：解绑cf\n"
                "请带上要解绑的平台名。"
            )
            return
        confirm_match = ACCOUNT_CONFIRM_RE.match(command_text)
        if confirm_match:
            platform = normalize_platform(confirm_match.group(1))
            if platform:
                async for result in self._reply_account_confirm(
                    event, platform, confirm_match.group(2) or ""
                ):
                    yield result
            return
        unbind_match = ACCOUNT_UNBIND_RE.match(command_text)
        if unbind_match:
            platform = normalize_platform(unbind_match.group(1))
            if platform:
                async for result in self._reply_account_unbind(event, platform):
                    yield result
            return
        lookup_match = ACCOUNT_LOOKUP_RE.match(command_text)
        if lookup_match:
            platform = normalize_platform(lookup_match.group(1))
            if platform:
                async for result in self._reply_account_lookup(
                    event, platform, lookup_match.group(2)
                ):
                    yield result
            return
        lookup_usage_match = ACCOUNT_LOOKUP_USAGE_RE.match(command_text)
        if lookup_usage_match:
            platform = normalize_platform(lookup_usage_match.group(1))
            if platform:
                yield event.plain_result(self._account_lookup_usage(platform))
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
        rank_command = parse_group_rank_command(message_str)
        if rank_command is not None:
            rank_mode, rank_page = rank_command
            async for result in self._reply_group_rank(
                event,
                rank_mode,
                page=rank_page,
            ):
                yield result
            return
        if message_str in MENU_COMMANDS:
            async for result in self._adaptive_results(event, MENU_TEXT):
                yield result
            return
        if message_str in DAILY_PROBLEM_COMMANDS:
            async for result in self._reply_daily_problem(event):
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
        platform_id = str(event.get_platform_id() or "")
        await self.remember_group(
            str(group_id),
            platform_id=platform_id,
            umo=str(getattr(event, "unified_msg_origin", "") or ""),
            name=event_group_name(event),
        )
        # 已激活的群：重复发送激活命令不再回复，静默忽略
        current = next(
            (
                g
                for g in await self.get_groups()
                if g.group_id == str(group_id)
                and str(g.platform_id or "") == platform_id
            ),
            None,
        )
        if current is not None and current.activated:
            logger.info("群 %s 已处于激活状态，忽略重复激活命令", group_id)
            return
        ready = self._group_scene_ready(str(group_id), platform_id)
        if ready:
            raw = await self._raw_groups()
            cfg = raw.get(platform_compat.scoped_key(platform_id, str(group_id)))
            if isinstance(cfg, dict):
                cfg["activated"] = True
                await self.put_kv_data("groups", raw)
                self._groups_cache = None
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
    @filter.platform_adapter_type(
        filter.PlatformAdapterType.QQOFFICIAL
        | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
        | filter.PlatformAdapterType.AIOCQHTTP
    )
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        if group_id:
            gid = str(group_id)
            platform_id = str(event.get_platform_id() or "")
            key = platform_compat.scoped_key(platform_id, gid)
            first_this_run = key not in self._seen_group_this_run
            self._seen_group_this_run.add(key)
            await self.remember_group(
                gid,
                platform_id=platform_id,
                umo=str(getattr(event, "unified_msg_origin", "") or ""),
                name=event_group_name(event),
            )
            if first_this_run:
                raw = await self._raw_groups()
                cfg = raw.get(key)
                if isinstance(cfg, dict) and cfg.get("activated"):
                    logger.info(
                        "群 %s 已自动重新激活（重启后收到首条消息）", gid
                    )

    # ------------------------------------------------------------------
    # WebUI 配置
    # ------------------------------------------------------------------
    @staticmethod
    def _query_param(name: str, default: str = "") -> str:
        """读取 GET 查询参数。

        不同 AstrBot 版本的 request 实现略有差异（args / query），这里集中兜底，
        避免各接口散落 try/except。
        """
        for source in (
            getattr(request, "args", None),
            getattr(request, "query", None),
        ):
            if source is None:
                continue
            getter = getattr(source, "get", None)
            if not callable(getter):
                continue
            try:
                value = getter(name)
            except Exception:  # noqa: BLE001 - 参数读取失败按缺省处理
                continue
            if value is not None:
                return str(value)
        return default

    async def _log_push(
        self, group_id: str, kind: str, ok: bool, detail: str = ""
    ) -> None:
        """把一次推送结果写进环形日志（最近 PUSH_LOG_MAX 条），失败只告警。"""
        try:
            items = await self.get_kv_data(PUSH_LOG_KEY, []) or []
        except Exception as exc:  # noqa: BLE001 - 日志失败不影响推送
            logger.warning("读取推送日志失败：%s", exc)
            return
        if not isinstance(items, list):
            items = []
        items.append(
            {
                "ts": time.time(),
                "group_id": str(group_id),
                "kind": str(kind),
                "ok": bool(ok),
                "detail": str(detail or "")[:200],
            }
        )
        try:
            await self.put_kv_data(PUSH_LOG_KEY, items[-PUSH_LOG_MAX:])
        except Exception as exc:  # noqa: BLE001
            logger.warning("写入推送日志失败：%s", exc)

    @staticmethod
    def _next_time_occurrence(
        now: datetime, hhmm: str
    ) -> Optional[datetime]:
        """今天的 hhmm 若已过，则返回明天的同一时刻。"""
        try:
            text = validate_hhmm(hhmm)
        except ValueError:
            return None
        hour, minute = (int(part) for part in text.split(":"))
        moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return moment if moment > now else moment + timedelta(days=1)

    @staticmethod
    def _next_weekday_occurrence(
        now: datetime, weekday: int, hhmm: str
    ) -> Optional[datetime]:
        """下一个指定星期（1=周一）的 hhmm。"""
        try:
            text = validate_hhmm(hhmm)
        except ValueError:
            return None
        hour, minute = (int(part) for part in text.split(":"))
        target = max(1, min(7, int(weekday)))
        moment = (now + timedelta(days=(target - now.isoweekday()) % 7)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if moment <= now:
            moment += timedelta(days=7)
        return moment

    async def _next_push_times(
        self, groups: List[GroupConfig], now: Optional[datetime] = None
    ) -> List[dict]:
        """各群下次早报 + 全局下次周报；赛果/报名提醒是事件驱动，不在其中。"""
        moment = now or datetime.now(CN_TZ)
        settings = await self.get_settings()
        entries: List[dict] = []
        for group in groups:
            if not group.enabled:
                continue
            at = self._next_time_occurrence(moment, group.morning_push_time)
            if at is not None:
                entries.append(
                    {
                        "group_id": str(group.group_id),
                        "kind": "morning",
                        "at": at.isoformat(),
                    }
                )
        if settings.get("weekly_report_enabled", True):
            at = self._next_weekday_occurrence(
                moment,
                settings.get("weekly_report_weekday", 1),
                settings.get("weekly_report_time", "20:00"),
            )
            if at is not None:
                entries.append(
                    {"group_id": "", "kind": "weekly_report", "at": at.isoformat()}
                )
        entries.sort(key=lambda item: item["at"])
        return entries[:20]

    async def refresh_group_names(self, *, limit: int = 50) -> dict:
        """给还没有名字的群补一次群名：OneBot 走 get_group_info，官方走开放接口。"""
        raw = await self._raw_groups(fresh=True)
        pending = [
            (key, cfg)
            for key, cfg in raw.items()
            if isinstance(cfg, dict) and not str(cfg.get("name") or "").strip()
        ]
        if not pending:
            return {"updated": 0, "failed": 0, "skipped": 0, "pending": 0}
        updated = failed = 0
        for key, cfg in pending[:limit]:
            pid, gid = platform_compat.split_scoped(str(key))
            pid = str(pid or cfg.get("platform_id") or "")
            channel = platform_compat.channel_of_platform_id(self.context, pid)
            name = await fetch_group_name(self.context, pid, str(gid), channel)
            if not name:
                failed += 1
                continue
            cfg["name"] = name
            updated += 1
        if updated:
            await self.put_kv_data("groups", raw)
            self._groups_cache = None
            logger.info("acmerQQ群机器人 已补全 %d 个群名", updated)
        return {
            "updated": updated,
            "failed": failed,
            "skipped": max(0, len(pending) - limit),
            "pending": len(pending),
        }

    async def _web_refresh_group_names(self):
        """后台「刷新群名」：补齐缺失的群名称。"""
        payload = await request.json(default=None)
        limit = 50
        if isinstance(payload, dict) and payload.get("limit") is not None:
            try:
                limit = int(payload["limit"])
            except (TypeError, ValueError):
                limit = 50
        limit = max(1, min(200, limit))
        try:
            data = await self.refresh_group_names(limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("刷新群名失败：%s", exc, exc_info=True)
            return error_response(f"刷新群名失败：{exc}")
        return json_response({"status": "success", "data": data})

    async def _web_overview(self):
        """后台概览：计数、平台实例、存储后端、最近推送、下次推送。"""
        try:
            groups = await self.get_groups()
            admins = await self._get_admins()
        except Exception as exc:  # noqa: BLE001
            logger.warning("后台概览读取失败：%s", exc)
            return error_response("读取概览失败，请稍后重试")
        try:
            raw_log = await self.get_kv_data(PUSH_LOG_KEY, []) or []
        except Exception:  # noqa: BLE001 - 日志缺失不影响概览
            raw_log = []
        if not isinstance(raw_log, list):
            raw_log = []
        recent = [item for item in raw_log if isinstance(item, dict)][-20:][::-1]
        return json_response(
            {
                "status": "success",
                "data": {
                    "group_count": len(groups),
                    "enabled_group_count": sum(1 for g in groups if g.enabled),
                    "admin_count": len(admins),
                    "platform_id": self._default_platform_id(),
                    "store_backend": (
                        "sqlite"
                        if self.account_registry.store_enabled
                        else "kv"
                    ),
                    "next_pushes": await self._next_push_times(groups),
                    "recent_pushes": recent,
                },
            }
        )

    async def _web_push_log(self):
        """推送日志：倒序返回，支持 limit / group_id。"""
        try:
            limit = int(self._query_param("limit", "50") or 50)
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(PUSH_LOG_MAX, limit))
        group_id = self._query_param("group_id", "").strip()
        try:
            items = await self.get_kv_data(PUSH_LOG_KEY, []) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取推送日志失败：%s", exc)
            items = []
        if not isinstance(items, list):
            items = []
        rows = [item for item in items if isinstance(item, dict)]
        if group_id:
            rows = [
                item
                for item in rows
                if str(item.get("group_id") or "") == group_id
            ]
        rows = rows[::-1][:limit]
        return json_response(
            {"status": "success", "data": {"items": rows, "total": len(rows)}}
        )

    async def _web_config_get(self):
        return json_response(
            {
                "status": "success",
                "data": {
                    "admin_users": await self._get_admins(),
                    "settings": await self.get_settings(),
                    "groups": [g.model_dump() for g in await self.get_groups()],
                    "platform_id": self._default_platform_id(),
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
                normalized = await self._normalize_settings_payload(
                    settings, await self.get_settings()
                )
                await self.put_kv_data("settings", normalized)
                self._settings_cache = None
                self._configure_output_renderer(
                    {
                        "max_plain_text_chars": normalized[
                            "max_plain_text_chars"
                        ],
                        "max_plain_text_lines": normalized[
                            "max_plain_text_lines"
                        ],
                    }
                )
                self._configure_contest_fetcher(
                    {"nowcoder_scope": normalized["nowcoder_scope"]}
                )
            if "groups" in payload:
                raw = self._build_groups_payload(payload["groups"])
                await self.put_kv_data("groups", raw)
                self._groups_cache = None
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
        if not self._group_scene_ready(group_id, group.platform_id):
            return error_response(
                "QQ 主动推送会话未就绪：请先让该群给机器人发一条消息，再点测试推送"
            )
        text = await self.build_test_text(group)
        sent = await self.send_notification(group, text)
        if not sent:
            await self._log_push(group_id, "test", False, "发送失败")
            return error_response("发送失败，请查看 AstrBot 日志")
        # 与真实早报一致：正文之后追加两张周榜图片，便于在后台预览完整效果。
        try:
            await self.push_weekly_boards(group)
        except Exception:  # noqa: BLE001 - 周榜预览失败不影响测试推送结果
            logger.warning("测试推送的周榜发送失败", exc_info=True)
        await self._log_push(group_id, "test", True, "测试推送")
        return json_response({"status": "success", "data": {"message": "测试推送已发送"}})

    async def _normalize_settings_payload(
        self, settings: dict, current: dict
    ) -> dict:
        """校验并归一化 settings；非法值抛 ValueError（与保存配置同一套规则）。"""
        morning = validate_hhmm(
            settings.get("morning_push_time", current["morning_push_time"])
        )
        raw_platforms = settings.get("push_platforms", current["push_platforms"])
        platforms = raw_platforms or list(DEFAULT_PLATFORMS)
        if not isinstance(platforms, list):
            raise ValueError("push_platforms 必须是列表")
        platforms = [p for p in DEFAULT_PLATFORMS if p in platforms]
        max_plain_text_chars = self._validate_bounded_int(
            settings.get("max_plain_text_chars", current["max_plain_text_chars"]),
            "文字转图片最大字符数",
            MIN_MAX_PLAIN_TEXT_CHARS,
            MAX_MAX_PLAIN_TEXT_CHARS,
        )
        max_plain_text_lines = self._validate_bounded_int(
            settings.get("max_plain_text_lines", current["max_plain_text_lines"]),
            "文字转图片最大行数",
            MIN_MAX_PLAIN_TEXT_LINES,
            MAX_MAX_PLAIN_TEXT_LINES,
        )
        recent_contest_days = self._validate_bounded_int(
            settings.get("recent_contest_days", current["recent_contest_days"]),
            "最近比赛查询天数",
            MIN_RECENT_CONTEST_DAYS,
            MAX_RECENT_CONTEST_DAYS,
        )
        nowcoder_scope = self._read_nowcoder_scope(
            settings.get("nowcoder_scope", current["nowcoder_scope"])
        )
        settle_delay_minutes = self._validate_bounded_int(
            settings.get("settle_delay_minutes", current["settle_delay_minutes"]),
            "赛后赛果推送延迟",
            MIN_SETTLE_DELAY_MINUTES,
            MAX_SETTLE_DELAY_MINUTES,
        )
        daily_problem_count = self._validate_bounded_int(
            settings.get("daily_problem_count", current["daily_problem_count"]),
            "每日一题数量",
            MIN_DAILY_PROBLEM_COUNT,
            MAX_DAILY_PROBLEM_COUNT,
        )
        settle_min_participants = self._validate_bounded_int(
            settings.get(
                "settle_min_participants", current["settle_min_participants"]
            ),
            "赛后赛果最少参赛人数",
            MIN_SETTLE_MIN_PARTICIPANTS,
            MAX_SETTLE_MIN_PARTICIPANTS,
        )
        weekly_report_weekday = self._validate_bounded_int(
            settings.get("weekly_report_weekday", current["weekly_report_weekday"]),
            "训练周报推送星期",
            MIN_WEEKLY_REPORT_WEEKDAY,
            MAX_WEEKLY_REPORT_WEEKDAY,
        )
        weekly_report_time = validate_hhmm(
            settings.get("weekly_report_time", current["weekly_report_time"])
        )
        return {
            "morning_push_time": morning,
            "push_platforms": platforms or list(DEFAULT_PLATFORMS),
            "reminder_enabled": bool(
                settings.get("reminder_enabled", current["reminder_enabled"])
            ),
            "at_all_enabled": bool(
                settings.get("at_all_enabled", current["at_all_enabled"])
            ),
            "max_plain_text_chars": max_plain_text_chars,
            "max_plain_text_lines": max_plain_text_lines,
            "recent_contest_days": recent_contest_days,
            "nowcoder_scope": nowcoder_scope,
            "settle_push_enabled": bool(
                settings.get("settle_push_enabled", current["settle_push_enabled"])
            ),
            "settle_delay_minutes": settle_delay_minutes,
            "settle_min_participants": settle_min_participants,
            "settle_show_unsolved": bool(
                settings.get("settle_show_unsolved", current["settle_show_unsolved"])
            ),
            "daily_problem_enabled": bool(
                settings.get("daily_problem_enabled", current["daily_problem_enabled"])
            ),
            "daily_problem_platform": self._read_daily_problem_platform(
                settings.get(
                    "daily_problem_platform", current["daily_problem_platform"]
                )
            ),
            "daily_problem_count": daily_problem_count,
            "recommend_enabled": bool(
                settings.get("recommend_enabled", current["recommend_enabled"])
            ),
            "weekly_report_enabled": bool(
                settings.get("weekly_report_enabled", current["weekly_report_enabled"])
            ),
            "weekly_report_weekday": weekly_report_weekday,
            "weekly_report_time": weekly_report_time,
        }

    @staticmethod
    def _build_groups_payload(groups) -> dict:
        """校验并归一化 groups 列表；非法值抛 ValueError。"""
        if not isinstance(groups, list):
            raise ValueError("groups 必须是列表")
        raw = {}
        for item in groups:
            if not isinstance(item, dict) or not item.get("group_id"):
                continue
            gid = str(item["group_id"])
            pid = str(item.get("platform_id") or "")
            # 注意：payload 里本来就带 group_id（前端回传整行），
            # 不能直接 GroupConfig(group_id=gid, **item)，否则重复关键字必报 TypeError。
            fields = {key: value for key, value in item.items() if key != "group_id"}
            try:
                cfg = GroupConfig(group_id=gid, **fields)
            except Exception as exc:
                raise ValueError(f"群 {gid} 配置不合法：{exc}") from exc
            raw[platform_compat.scoped_key(pid, gid)] = cfg.model_dump()
        return raw

    async def _web_bindings_list(self):
        """账号绑定列表（查）：一次性返回，前端筛选与分页。"""
        platform = self._query_param("platform", "").strip()
        keyword = self._query_param("q", "").strip().lower()
        try:
            limit = int(self._query_param("limit", "200") or 200)
        except (TypeError, ValueError):
            limit = 200
        try:
            offset = int(self._query_param("offset", "0") or 0)
        except (TypeError, ValueError):
            offset = 0
        limit = max(1, min(2000, limit))
        offset = max(0, offset)
        try:
            accounts = await self.account_registry.get_all_accounts()
        except Exception as exc:  # noqa: BLE001
            logger.warning("后台读取绑定失败：%s", exc)
            return error_response("读取绑定失败，请稍后重试")
        items = []
        for user_id, per_user in accounts.items():
            if not isinstance(per_user, dict):
                continue
            for platform_name, record in per_user.items():
                if not isinstance(record, dict):
                    continue
                if platform and str(platform_name) != platform:
                    continue
                handle = str(
                    record.get("handle") or record.get("platform_user_id") or ""
                )
                qq_name = str(record.get("qq_name") or "")
                if keyword and keyword not in (
                    f"{user_id} {handle} {qq_name}".lower()
                ):
                    continue
                try:
                    verified_at = float(record.get("verified_at") or 0)
                except (TypeError, ValueError):
                    verified_at = 0.0
                items.append(
                    {
                        "user_id": str(user_id),
                        "platform": str(platform_name),
                        "handle": handle,
                        "platform_user_id": str(
                            record.get("platform_user_id") or ""
                        ),
                        "display_name": str(record.get("display_name") or handle),
                        "qq_name": qq_name,
                        "verified_at": verified_at,
                    }
                )
        items.sort(key=lambda item: item["verified_at"], reverse=True)
        counts: Dict[str, int] = {}
        for item in items:
            counts[item["platform"]] = counts.get(item["platform"], 0) + 1
        return json_response(
            {
                "status": "success",
                "data": {
                    "total": len(items),
                    "items": items[offset: offset + limit],
                    "platform_counts": counts,
                },
            }
        )

    async def _web_bindings_write(self):
        """账号绑定增/改/删（actions: save | delete）。"""
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            return error_response("请求体格式不正确")
        action = str(payload.get("action") or "").strip()

        if action == "delete":
            items = payload.get("items")
            if not isinstance(items, list) or not items:
                return error_response("请选择要删除的绑定")
            removed = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                user_id = str(item.get("user_id") or "").strip()
                platform = str(item.get("platform") or "").strip()
                if not user_id or platform not in ACCOUNT_PLATFORMS:
                    continue
                try:
                    if await self.account_registry.remove_binding(user_id, platform):
                        removed += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "后台解绑失败 user=%s platform=%s: %s",
                        user_id,
                        platform,
                        exc,
                    )
            if removed:
                self._invalidate_all_rank_cache()
                logger.info("后台解绑 admin=web removed=%d", removed)
            return json_response(
                {"status": "success", "data": {"action": "delete", "removed": removed}}
            )

        if action != "save":
            return error_response("不支持的 action")

        user_id = str(payload.get("user_id") or "").strip()
        platform = str(payload.get("platform") or "").strip()
        identifier = str(payload.get("identifier") or "").strip()
        qq_name = str(payload.get("qq_name") or "").strip()[:32]
        group_id = str(payload.get("group_id") or "").strip()

        if not user_id:
            return error_response("请填写 QQ 用户 ID")
        if platform not in ACCOUNT_PLATFORMS:
            return error_response("不支持的平台")
        if not identifier:
            return error_response("请填写账号")
        normalized = normalize_account_identifier(platform, identifier)
        if not normalized:
            return error_response(
                self.account_fetcher.invalid_identifier_message(platform)
            )

        try:
            profile = await self.account_fetcher.get_profile(
                platform, normalized, detail=False, force=True
            )
        except AccountFetchError as exc:
            return error_response(self._account_error_text(platform, exc))
        except Exception as exc:  # noqa: BLE001
            logger.warning("后台绑定抓取失败：%s", exc)
            return error_response(self._account_error_text(platform, exc))

        old_handle = ""
        try:
            record = (await self.account_registry.get_user_accounts(user_id)).get(
                platform
            )
            if isinstance(record, dict):
                old_handle = str(
                    record.get("handle") or record.get("platform_user_id") or ""
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取原绑定失败：%s", exc)

        try:
            await self.account_registry.save_binding(
                user_id,
                platform,
                profile,
                group_id=group_id or None,
                qq_name=qq_name,
            )
        except ValueError:
            return error_response("这个平台账号已经绑定到其他 QQ 用户，请先删除原绑定")
        except Exception as exc:  # noqa: BLE001
            logger.error("后台绑定保存失败：%s", exc, exc_info=True)
            return error_response("绑定保存失败，请稍后重试")

        self._invalidate_all_rank_cache()
        logger.info(
            "后台绑定 admin=web user=%s platform=%s handle=%s",
            user_id,
            platform,
            profile.handle,
        )
        return json_response(
            {
                "status": "success",
                "data": {
                    "action": "save",
                    "item": {
                        "user_id": user_id,
                        "platform": platform,
                        "handle": profile.handle,
                        "qq_name": qq_name,
                    },
                    "replaced": old_handle or None,
                },
            }
        )

    async def _web_rank_members(self):
        """群排行成员列表：区分自然成员与后台强制加入，并带出绑定账号。"""
        group_id = self._query_param("group_id", "").strip()
        if not group_id:
            return error_response("缺少 group_id")
        try:
            members = await self.account_registry.list_rank_members(group_id)
            accounts = await self.account_registry.get_all_accounts()
        except Exception as exc:  # noqa: BLE001
            logger.warning("后台读取排行成员失败：%s", exc)
            return error_response("读取排行成员失败，请稍后重试")

        items = []
        for item in members:
            uid = str(item.get("user_id") or "")
            bound = []
            for platform, record in (accounts.get(uid) or {}).items():
                if not isinstance(record, dict):
                    continue
                bound.append(
                    {
                        "platform": platform,
                        "handle": str(
                            record.get("handle") or record.get("platform_user_id") or ""
                        ),
                        "display_name": str(record.get("display_name") or ""),
                        "qq_name": str(record.get("qq_name") or ""),
                    }
                )
            qq_name = next((b["qq_name"] for b in bound if b["qq_name"]), "")
            items.append({**item, "accounts": bound, "qq_name": qq_name})
        manual_count = sum(1 for item in items if item.get("manual"))
        return json_response(
            {
                "status": "success",
                "data": {
                    "group_id": group_id,
                    "items": items,
                    "total": len(items),
                    "manual_count": manual_count,
                },
            }
        )

    async def _web_rank_members_write(self):
        """后台强制把已绑定用户加入某群排行，或移除。"""
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            return error_response("请求体格式不正确")
        action = str(payload.get("action") or "").strip()
        group_id = str(payload.get("group_id") or "").strip()
        user_id = str(payload.get("user_id") or "").strip()
        if not group_id or not user_id:
            return error_response("缺少 group_id / user_id")

        if action == "add":
            try:
                accounts = await self.account_registry.get_user_accounts(user_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取待加入用户绑定失败：%s", exc)
                return error_response("读取该用户的绑定失败，请稍后重试")
            if not accounts:
                return error_response("该用户还没有绑定任何平台账号，请先在「账号绑定」里绑定")
            try:
                result = await self.account_registry.add_rank_member(
                    group_id, user_id, added_by="webui"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("强制加入排行失败：%s", exc, exc_info=True)
                return error_response("强制加入失败，请稍后重试")
            self._invalidate_rank_cache(group_id)
            logger.info(
                "后台强制加入排行 group=%s user=%s preexisting=%s",
                group_id,
                user_id,
                result.get("preexisting"),
            )
            return json_response({"status": "success", "data": result})

        if action == "remove":
            try:
                result = await self.account_registry.remove_rank_member(group_id, user_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("移除排行成员失败：%s", exc, exc_info=True)
                return error_response("移除失败，请稍后重试")
            self._invalidate_rank_cache(group_id)
            logger.info(
                "后台移除排行成员 group=%s user=%s restored=%s",
                group_id,
                user_id,
                result.get("restored"),
            )
            return json_response({"status": "success", "data": result})

        return error_response("不支持的 action")

    async def _web_rank(self):
        """群排行只读查看；默认命中快照，refresh=1 才强制重算。"""
        group_id = self._query_param("group_id", "").strip()
        if not group_id:
            return error_response("缺少 group_id")
        platform = self._query_param("platform", "codeforces").strip() or "codeforces"
        if platform not in ACCOUNT_PLATFORMS:
            return error_response("不支持的平台")
        progress = self._query_param("progress", "0") in {"1", "true", "True"}
        refresh = self._query_param("refresh", "0") in {"1", "true", "True"}
        try:
            rows, errors = await self.rank_service.read(
                group_id,
                platform,
                progress=progress,
                allow_stale=not refresh,
                force=refresh,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("后台读取排行失败：%s", exc)
            return error_response("读取排行失败，请稍后重试")
        return json_response(
            {
                "status": "success",
                "data": {
                    "rows": rows,
                    "errors": [str(item) for item in (errors or [])],
                    "stale": not refresh,
                },
            }
        )

    async def _run_now_settle(self, group: GroupConfig, contest_id: str) -> List[dict]:
        """试跑赛果：挑一场刚结束的比赛推送（不写幂等键）。"""
        settings = await self.get_settings()
        delay_minutes = int(settings.get("settle_delay_minutes") or 0)
        min_participants = int(settings.get("settle_min_participants") or 1)
        show_unsolved = bool(settings.get("settle_show_unsolved", True))
        platforms = [
            p for p in settings["push_platforms"] if p in group.push_platforms
        ] or list(group.push_platforms or DEFAULT_PLATFORMS)
        moment = datetime.now(timezone.utc)
        for platform in platforms:
            if platform not in SETTLE_PLATFORMS:
                continue
            try:
                contests, _err = await self.fetcher.fetch_platform(platform)
            except Exception:  # noqa: BLE001
                continue
            if contests:
                self.settlement.remember_contests(platform, contests)
            candidates = self.settlement.settlement_candidates(
                platform, moment, delay_minutes
            )
            if contest_id:
                candidates = [
                    item
                    for item in candidates
                    if str(getattr(item, "contest_id", "")) == contest_id
                ]
            if not candidates:
                continue
            contest = candidates[0]
            try:
                pushed = await self._push_settlement(
                    group,
                    platform,
                    contest,
                    "",
                    min_participants=min_participants,
                    show_unsolved=show_unsolved,
                    platform_order=list(platforms),
                    write_key=False,
                )
            except Exception as exc:  # noqa: BLE001
                return [{"step": "赛果推送", "ok": False, "message": str(exc)}]
            if pushed:
                return [
                    {
                        "step": "赛果推送",
                        "ok": True,
                        "message": f"{platform} {getattr(contest, 'contest_id', '')}",
                    }
                ]
        return [
            {
                "step": "赛果推送",
                "ok": False,
                "message": "没有可推送的已结束比赛（或本群无人参加）",
            }
        ]

    async def _run_now_signup(self, group: GroupConfig, contest_id: str) -> List[dict]:
        """试跑报名提醒：挑一场未开始且报名未截止的牛客比赛（不写幂等键）。"""
        try:
            contests, err = await self.fetcher.fetch_platform("nowcoder")
        except Exception as exc:  # noqa: BLE001
            return [{"step": "报名提醒", "ok": False, "message": str(exc)}]
        if err or not contests:
            return [{"step": "报名提醒", "ok": False, "message": "读不到牛客赛程"}]
        moment = datetime.now(timezone.utc)
        for contest in contests:
            cid = str(getattr(contest, "contest_id", ""))
            if contest_id and cid != contest_id:
                continue
            deadline = getattr(contest, "signup_end_time", None)
            start = getattr(contest, "start_time", None)
            if not isinstance(deadline, datetime) or not isinstance(start, datetime):
                continue
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if start <= moment:
                continue
            remaining = (deadline - moment).total_seconds()
            if remaining < 0:
                continue
            text = self._signup_reminder_text(contest, deadline, remaining)
            sent = await self._send_signup_text(group, text)
            return [
                {"step": "报名提醒", "ok": bool(sent), "message": cid or "已发送"}
            ]
        return [
            {
                "step": "报名提醒",
                "ok": False,
                "message": "没有可提醒的比赛（需未开始且报名未截止）",
            }
        ]

    async def _web_run_now(self):
        """立即试跑：单群、指定类型；不写任何幂等键。"""
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            return error_response("请求体格式不正确")
        group_id = str(payload.get("group_id") or "").strip()
        kind = str(payload.get("kind") or "").strip()
        contest_id = str(payload.get("contest_id") or "").strip()
        if not group_id:
            return error_response("缺少 group_id")
        if kind not in RUN_NOW_KINDS:
            return error_response("不支持的试跑类型")
        group = next(
            (g for g in await self.get_groups() if g.group_id == group_id), None
        )
        if group is None:
            return error_response("该群未注册，请先让群内发一条消息")
        if not group.enabled:
            return error_response("该群已停用推送")
        if not self._group_scene_ready(group.group_id, group.platform_id):
            return error_response("QQ 主动推送会话未就绪：请先让该群给机器人发一条消息")
        results: List[dict] = []
        try:
            if kind == "morning":
                text = await self.build_test_text(group)
                sent = await self.send_notification(group, text)
                results.append(
                    {
                        "step": "发送早报",
                        "ok": bool(sent),
                        "message": "已发送" if sent else "发送失败",
                    }
                )
                if sent:
                    try:
                        boards_ok = await self.push_weekly_boards(group)
                        results.append(
                            {
                                "step": "推送周榜",
                                "ok": bool(boards_ok),
                                "message": "已发送" if boards_ok else "无数据或发送失败",
                            }
                        )
                    except Exception as exc:  # noqa: BLE001
                        results.append(
                            {"step": "推送周榜", "ok": False, "message": str(exc)}
                        )
            elif kind == "weekly":
                pushed = await self._push_weekly_report_for_group(
                    group, datetime.now(CN_TZ), write_key=False
                )
                results.append(
                    {
                        "step": "训练周报",
                        "ok": bool(pushed),
                        "message": "已发送" if pushed else "无数据或发送失败",
                    }
                )
            elif kind == "settle":
                results.extend(await self._run_now_settle(group, contest_id))
            elif kind == "signup":
                results.extend(await self._run_now_signup(group, contest_id))
        except Exception as exc:  # noqa: BLE001
            logger.error("试跑失败：%s", exc, exc_info=True)
            return error_response(f"试跑失败：{exc}")
        ok_all = bool(results) and all(bool(item.get("ok")) for item in results)
        await self._log_push(group_id, "test", ok_all, f"试跑:{kind}")
        return json_response(
            {"status": "success", "data": {"ok": ok_all, "results": results}}
        )

    async def _web_import(self):
        """导入配置：apply=false 只校验并返回差异；apply=true 落库。"""
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            return error_response("请求体格式不正确")
        data = payload.get("payload")
        if not isinstance(data, dict):
            return error_response("导入内容格式不正确")
        include_groups = bool(payload.get("include_groups", True))
        apply_changes = bool(payload.get("apply", False))
        try:
            current = await self.get_settings()
            current_admins = await self._get_admins()
            diff: Dict[str, Any] = {}

            incoming_admins = data.get("admin_users")
            if incoming_admins is not None:
                if not isinstance(incoming_admins, list):
                    raise ValueError("admin_users 必须是列表")
                normalized_admins = [
                    str(item).strip() for item in incoming_admins if str(item).strip()
                ]
                diff["admin_users"] = {
                    "add": sorted(set(normalized_admins) - set(current_admins)),
                    "remove": sorted(set(current_admins) - set(normalized_admins)),
                    "keep": sorted(set(normalized_admins) & set(current_admins)),
                }
            else:
                normalized_admins = current_admins

            incoming_settings = data.get("settings")
            if incoming_settings is not None:
                if not isinstance(incoming_settings, dict):
                    raise ValueError("settings 必须是对象")
                normalized_settings = await self._normalize_settings_payload(
                    incoming_settings, current
                )
                diff["settings"] = {
                    key: {"from": current.get(key), "to": value}
                    for key, value in normalized_settings.items()
                    if current.get(key) != value
                }
            else:
                normalized_settings = None

            groups_payload = None
            if include_groups and data.get("groups") is not None:
                groups_payload = self._build_groups_payload(data["groups"])
                current_raw = await self._raw_groups(fresh=True)
                diff["groups"] = {
                    "add": sorted(set(groups_payload) - set(current_raw)),
                    "remove": sorted(set(current_raw) - set(groups_payload)),
                    "update": sorted(
                        key
                        for key in set(groups_payload) & set(current_raw)
                        if current_raw.get(key) != groups_payload.get(key)
                    ),
                }
        except ValueError as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.error("导入配置校验失败：%s", exc, exc_info=True)
            return error_response("导入失败：内容无法解析")

        if not apply_changes:
            return json_response(
                {"status": "success", "data": {"applied": False, "diff": diff}}
            )

        applied: Dict[str, Any] = {}
        try:
            if incoming_admins is not None:
                await self.put_kv_data("admin_users", normalized_admins)
                applied["admin_users"] = len(normalized_admins)
            if normalized_settings is not None:
                await self.put_kv_data("settings", normalized_settings)
                self._settings_cache = None
                self._configure_output_renderer(
                    {
                        "max_plain_text_chars": normalized_settings[
                            "max_plain_text_chars"
                        ],
                        "max_plain_text_lines": normalized_settings[
                            "max_plain_text_lines"
                        ],
                    }
                )
                self._configure_contest_fetcher(
                    {"nowcoder_scope": normalized_settings["nowcoder_scope"]}
                )
                applied["settings"] = len(normalized_settings)
            if groups_payload is not None:
                await self.put_kv_data("groups", groups_payload)
                self._groups_cache = None
                applied["groups"] = len(groups_payload)
        except Exception as exc:  # noqa: BLE001
            logger.error("导入配置写入失败：%s", exc, exc_info=True)
            return error_response("导入写入失败，请重试")
        return json_response(
            {
                "status": "success",
                "data": {"applied": True, "applied_counts": applied},
            }
        )
