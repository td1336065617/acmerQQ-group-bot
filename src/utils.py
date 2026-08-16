"""通用工具函数。"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

from astrbot.api import logger

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# 洛谷页面把比赛数据内嵌在 #lentille-context JSON 中
LENTILLE_RE = re.compile(
    r'<script id="lentille-context" type="application/json">(.*?)</script>',
    re.S,
)


async def fetch_text_with_retry(
    session,
    url: str,
    *,
    retries: int = 3,
    timeout: float = 10.0,
    **kwargs: Any,
) -> str:
    """带超时与重试的 GET 请求，返回响应文本。"""
    last_error: Optional[Exception] = None
    attempts = max(1, retries)
    for attempt in range(attempts):
        try:
            async with session.get(url, timeout=timeout, **kwargs) as resp:
                resp.raise_for_status()
                return await resp.text()
        except Exception as exc:  # noqa: BLE001 - 统一重试，由上层分类处理
            last_error = exc
            if attempt < attempts - 1:
                await asyncio.sleep(1.0 + attempt)
    if last_error is not None:
        raise last_error
    raise RuntimeError("请求未执行")  # pragma: no cover


def validate_hhmm(value: str) -> str:
    """校验 HH:MM 24 小时制时间，返回规范化字符串。"""
    text = str(value or "").strip()
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", text):
        raise ValueError("推送时间格式应为 HH:MM（24 小时制）")
    return text


def log_plugin(name: str, message: str) -> None:
    logger.info("[%s] %s", name, message)
