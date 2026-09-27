"""推送被拒（主动消息无权限）处理：错误分类、退避计算、状态键名（S1，台账 M1.1-M1.4）。

纯逻辑模块：不持有 bot 引用、不做 IO；KV 读写与缓存由 main.py 负责（见实现文档 §1）。

导入约定：src 子包只在子包内互相引用、不导入根包成员——
因为测试把插件目录插进 sys.path 后，src 是顶层包，双点相对导入会直接报错
（根包成员 platform_compat 在测试里以顶层名导入）。
因此键格式与 platform_compat.scoped_key 保持一致，并由测试断言两相等来锁定，
不允许单方面改格式。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Tuple

# ---- 常量（设计文档 §5.2 / §5.6；不开放为设置，调整走代码评审） ----
#: 连续被拒 1/2/3/4+ 次的暂停时长（分钟）
DENIED_BACKOFF_MINUTES: Tuple[int, ...] = (30, 360, 720, 1440)
HEALTH_TTL_DAYS = 90
BLOCKED_MARKER_TTL_HOURS = 72
LAST_ERROR_MAX_CHARS = 120
SAMPLE_MAX_CHARS = 200

K_PERMISSION = "permission_denied"
K_MUTED = "muted"
K_RATE = "rate_limited"
K_TRANSIENT = "transient"
K_UNKNOWN = "unknown"
K_NA = "not_applicable"

#: 错误类 -> 模式（按顺序匹配；类内长模式优先，避免子串误判）
PATTERN_TABLE: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        K_PERMISSION,
        ("主动消息失败", "无权限", "机器人不在群", "已退出该群", "not allowed", "forbidden"),
    ),
    (K_MUTED, ("被禁言", "禁言中", "muted", "mute")),
    (K_RATE, ("太频繁", "频率", "too many", "rate limit", "限额")),
    (K_TRANSIENT, ("timeout", "timed out", "connection reset", "connection refused", "连接")),
)

#: 异常类型名（小写）-> 错误类；类型名优先于文本
TYPE_HINTS: Dict[str, str] = {
    "forbiddenerror": K_PERMISSION,
    "forbidden": K_PERMISSION,
    "permissiondeniederror": K_PERMISSION,
}

_FULLWIDTH_OFFSET = 0xFEE0
_CODE_RE = re.compile(r"\b(?:code|status)[=:]\s*(\d{3})")
_ID_RE = re.compile(r"[0-9a-fA-F]{32}")
_URL_QUERY_RE = re.compile(r"(https?://\S+)\?\S+")


def normalize_text(value: Any) -> str:
    """归一化：非字符串先转字符串；全角转半角、转小写、压缩空白。"""
    if value is None:
        return ""
    out = []
    for ch in str(value):
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            ch = chr(code - _FULLWIDTH_OFFSET)
        elif code == 0x3000:
            ch = " "
        out.append(ch)
    return " ".join("".join(out).lower().split())


def redact(value: Any, limit: int = SAMPLE_MAX_CHARS) -> str:
    """脱敏与截断：抹掉 32 位十六进制标识与 URL 查询串；用于 KV 存储与日志。"""
    text = str(value or "")
    text = _ID_RE.sub("<id>", text)
    text = _URL_QUERY_RE.sub(r"\1?...", text)
    return text[:limit]


def classify(error: Any, channel: str = "official") -> Dict[str, Any]:
    """分类入口：绝不抛异常（调用点在发送路径上，分类失败不能拖垮发送）。

    返回 {kind, matched_by, pattern, raw, code}。
    匹配顺序：通道非 official -> not_applicable；异常类型名 -> 文本模式 -> unknown。
    """
    try:
        return _classify(error, channel)
    except Exception:  # noqa: BLE001 - 分类器自身故障也不能阻断发送
        return {"kind": K_UNKNOWN, "matched_by": "crash", "pattern": "", "raw": "", "code": None}


def _classify(error: Any, channel: str) -> Dict[str, Any]:
    if channel and channel != "official":
        return {"kind": K_NA, "matched_by": "channel", "pattern": "", "raw": "", "code": None}
    if isinstance(error, str):
        type_name = ""
        text = error
        code = None
    else:
        type_name = type(error).__name__
        text = str(error)
        code = getattr(error, "code", None)
    if not isinstance(code, int):
        match = _CODE_RE.search(text)
        code = int(match.group(1)) if match else None
    norm = normalize_text(text)
    compact = norm.replace(" ", "")
    raw = redact(text)
    hint = TYPE_HINTS.get(type_name.lower())
    if hint:
        return {"kind": hint, "matched_by": "type", "pattern": type_name, "raw": raw, "code": code}
    for kind, patterns in PATTERN_TABLE:
        for pattern in sorted(patterns, key=len, reverse=True):
            p_norm = normalize_text(pattern)
            if not p_norm:
                continue
            # 双轨匹配：原文归一形（英文按空格比对）+ 去空格紧凑形
            # （中文文本里出现散空格时，只比归一形会漏匹配——写测试时发现的缺陷）
            if p_norm in norm or p_norm.replace(" ", "") in compact:
                return {"kind": kind, "matched_by": "text", "pattern": pattern, "raw": raw, "code": code}
    return {"kind": K_UNKNOWN, "matched_by": "none", "pattern": "", "raw": raw, "code": code}


def is_denied_kind(kind: str) -> bool:
    """只有这两类触发群级暂停（unknown/限流/网络类一律保守重试）。"""
    return kind in (K_PERMISSION, K_MUTED)


def next_suspend_minutes(count: int) -> int:
    """第 count 次被拒的暂停时长（count 从 1 起；超出序列取末位上限）。"""
    if count < 1:
        count = 1
    index = min(count, len(DENIED_BACKOFF_MINUTES)) - 1
    return DENIED_BACKOFF_MINUTES[index]


def health_key(platform_id: str, group_id: str) -> str:
    """状态键：push_health_<平台>:<群>；平台为空时退化为 push_health_<群>。

    格式与 platform_compat.scoped_key 完全一致（由测试断言等价锁定），
    本模块不导入根包成员（src 子包导入约定，见模块 docstring）。
    """
    pid = str(platform_id or "")
    gid = str(group_id or "")
    scoped = f"{pid}:{gid}" if pid else gid
    return f"push_health_{scoped}"
