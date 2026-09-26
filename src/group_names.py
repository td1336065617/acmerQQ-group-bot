"""群名解析：OneBot 用 get_group_info，官方走开放接口（复用 botpy 的 access_token）。

AstrBot 的 qq_official 适配器**不带群名**（只有 aiocqhttp 会填 abm.group.group_name），
所以官方群名必须自己调 GET /v2/groups/{group_openid}/info。
"""
from __future__ import annotations

from typing import Any

try:  # 测试环境可无 astrbot
    from astrbot.api import logger
except Exception:  # noqa: BLE001
    import logging

    logger = logging.getLogger("acmer_qq_group_bot")

OFFICIAL_GROUP_INFO_PATH = "/v2/groups/{group_openid}/info"


def event_group_name(event: Any) -> str:
    """事件自带的群名（OneBot 有，官方没有）。"""
    group = getattr(getattr(event, "message_obj", None), "group", None)
    return str(getattr(group, "group_name", "") or "").strip()


def _platform_inst(context: Any, platform_id: str) -> Any:
    getter = getattr(context, "get_platform_inst", None)
    if not callable(getter):
        return None
    try:
        return getter(platform_id)
    except Exception:  # noqa: BLE001
        return None


async def fetch_group_name(
    context: Any, platform_id: str, group_id: str, channel: str = ""
) -> str:
    """主动拉取群名；拿不到返回空串（调用方按“未获取”展示）。"""
    if channel == "onebot":
        return await _fetch_onebot(context, platform_id, group_id)
    if channel == "official":
        return await _fetch_official(context, platform_id, group_id)
    return ""


async def _fetch_onebot(context: Any, platform_id: str, group_id: str) -> str:
    inst = _platform_inst(context, platform_id)
    bot = getattr(inst, "bot", None) if inst is not None else None
    if bot is None:
        return ""
    try:
        payload = await bot.call_action("get_group_info", group_id=int(group_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("OneBot 取群名失败（%s/%s）：%s", platform_id, group_id, exc)
        return ""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("group_name") or "").strip()


async def _fetch_official(context: Any, platform_id: str, group_id: str) -> str:
    inst = _platform_inst(context, platform_id)
    client = getattr(inst, "client", None) if inst is not None else None
    http = getattr(getattr(client, "api", None), "_http", None)
    if http is None:
        return ""
    try:  # 延迟导入：测试环境无需安装 botpy
        from botpy.http import Route
    except Exception:  # noqa: BLE001
        logger.warning("botpy 不可用，无法获取官方群名")
        return ""
    route = Route("GET", OFFICIAL_GROUP_INFO_PATH, group_openid=group_id)
    try:
        payload = await http.request(route)
    except Exception as exc:  # noqa: BLE001
        logger.warning("官方取群名失败（%s/%s）：%s", platform_id, group_id, exc)
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("group_name") or "").strip()
