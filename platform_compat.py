"""QQ 双通道兼容层（acmer 插件）。

官方族：qq_official / qq_official_webhook
非官方：aiocqhttp（OneBot v11）

只处理插件需要的差异：平台判定、平台实例解析、主动推送就绪判定、@全体、
作用域键与群配置迁移。
"""
from __future__ import annotations

from typing import Any

from astrbot.api.message_components import At, Plain

OFFICIAL_NAMES = {"qq_official", "qq_official_webhook"}
ONEBOT_NAMES = {"aiocqhttp"}
SEP = ":"


def channel_by_name(name: str) -> str:
    if name in OFFICIAL_NAMES:
        return "official"
    if name in ONEBOT_NAMES:
        return "onebot"
    return ""


def resolve_channel(event) -> str:
    try:
        name = event.get_platform_name()
    except Exception:  # noqa: BLE001
        return ""
    return channel_by_name(name)


def _insts(context) -> list:
    platform_manager = getattr(context, "platform_manager", None)
    return list(getattr(platform_manager, "platform_insts", []) or [])


def _meta(inst) -> Any:
    try:
        return inst.meta()
    except Exception:  # noqa: BLE001
        return None


def channel_of_platform_id(context, platform_id: str) -> str:
    """按平台实例 ID 反查通道类型。"""
    target = str(platform_id or "")
    if not target:
        return ""
    for inst in _insts(context):
        meta = _meta(inst)
        if meta is None:
            continue
        if str(getattr(meta, "id", "") or "") == target:
            return channel_by_name(str(getattr(meta, "name", "") or ""))
    return ""


def resolve_platform_id(context, *, event=None, umo: str | None = None) -> str:
    """解析事件/会话所属的平台实例 ID。"""
    if event is not None:
        getter = getattr(event, "get_platform_id", None)
        if callable(getter):
            try:
                value = str(getter() or "")
                if value:
                    return value
            except Exception:  # noqa: BLE001, S110
                pass
    if umo:
        try:
            from astrbot.core.platform.message_session import MessageSesion

            return str(MessageSesion.from_str(str(umo)).platform_name)
        except Exception:  # noqa: BLE001
            head = str(umo).split(":", 1)[0]
            if head:
                return head
    for want in ("official", "onebot"):
        for inst in _insts(context):
            meta = _meta(inst)
            if meta is None:
                continue
            if channel_by_name(str(getattr(meta, "name", "") or "")) == want:
                return str(
                    getattr(meta, "id", "") or getattr(meta, "name", "") or ""
                )
    return ""


def scene_ready(context, group_id: str, platform_id: str) -> bool:
    """主动推送是否就绪。

    官方族受被动窗口限制，需要该群本次运行给机器人发过消息；
    OneBot 无此限制，直接放行。
    """
    channel = channel_of_platform_id(context, platform_id)
    if channel == "onebot":
        return True
    gid = str(group_id)
    target = str(platform_id or "")
    for inst in _insts(context):
        meta = _meta(inst)
        if meta is None:
            continue
        if target and str(getattr(meta, "id", "") or "") != target:
            continue
        if channel_by_name(str(getattr(meta, "name", "") or "")) != "official":
            continue
        scene = getattr(inst, "_session_scene", {}).get(gid)
        if scene == "group":
            return True
    return False


def at_all_prefix(channel: str) -> list:
    """@全体成员前缀：OneBot 用 At(all)，官方族沿用文本标记。"""
    if channel == "onebot":
        return [At(qq="all"), Plain("\n")]
    return [Plain("<@everyone>\n")]


def scoped_key(platform_id: str, raw_id: str) -> str:
    raw_id = str(raw_id or "")
    platform_id = str(platform_id or "")
    return f"{platform_id}{SEP}{raw_id}" if platform_id else raw_id


def split_scoped(key: str) -> tuple[str, str]:
    key = str(key or "")
    if SEP in key:
        pid, raw = key.split(SEP, 1)
        return pid, raw
    return "", key


def admin_entry_matches(entry: str, platform_id: str, user_id: str) -> bool:
    entry = str(entry or "").strip()
    if not entry:
        return False
    if SEP in entry:
        pid, uid = entry.split(SEP, 1)
        return pid == str(platform_id or "") and uid == str(user_id or "")
    return entry == str(user_id or "")


def migrate_groups(raw: dict, default_platform_id: str) -> tuple[dict, bool]:
    """把旧结构 {gid: cfg} 迁移为 {platform_id:gid: cfg}；幂等。"""
    if not isinstance(raw, dict):
        return {}, False
    out: dict = {}
    changed = False
    for key, cfg in raw.items():
        pid, gid = split_scoped(str(key))
        if not pid:
            if isinstance(cfg, dict):
                pid = str(cfg.get("platform_id") or "")
            if not pid:
                pid = str(default_platform_id or "legacy")
            changed = True
        new_key = f"{pid}{SEP}{gid}" if pid else gid
        if new_key != str(key):
            changed = True
        out[new_key] = cfg
    return out, changed
