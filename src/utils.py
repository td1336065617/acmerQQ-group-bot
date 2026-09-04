"""通用工具函数。"""
from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, time as datetime_time, timedelta, timezone
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
CN_TZ = timezone(timedelta(hours=8))


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


def normalize_command(value: str) -> str:
    """规范化聊天指令：去除首尾空白并忽略英文大小写。"""
    return str(value or "").strip().casefold()


def _as_utc(value: object) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def contest_start_utc(contest: object) -> Optional[datetime]:
    """读取在线/线下比赛的统一开始时间（UTC）。"""
    start_time = _as_utc(getattr(contest, "start_time", None))
    if start_time is not None:
        return start_time
    start_date = getattr(contest, "start_date", None)
    if isinstance(start_date, datetime):
        start_date = start_date.date()
    if isinstance(start_date, date):
        return datetime.combine(
            start_date, datetime_time.min, tzinfo=CN_TZ
        ).astimezone(timezone.utc)
    return None


def contest_end_utc(contest: object) -> Optional[datetime]:
    """读取在线/线下比赛的统一结束时间（UTC）。"""
    end_time = _as_utc(getattr(contest, "end_time", None))
    if end_time is not None:
        return end_time
    start_time = contest_start_utc(contest)
    duration = getattr(contest, "duration_minutes", 0) or 0
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        duration = 0
    if start_time is not None and duration > 0:
        return start_time + timedelta(minutes=duration)
    end_date = getattr(contest, "end_date", None)
    if isinstance(end_date, datetime):
        end_date = end_date.date()
    if not isinstance(end_date, date):
        start_date = getattr(contest, "start_date", None)
        if isinstance(start_date, datetime):
            start_date = start_date.date()
        end_date = start_date if isinstance(start_date, date) else None
    if isinstance(end_date, date):
        return datetime.combine(
            end_date, datetime_time.max, tzinfo=CN_TZ
        ).astimezone(timezone.utc)
    return None


def is_contest_in_recent_window(
    contest: object, now: datetime, days: int
) -> bool:
    """判断比赛是否在未来 days 天内，或当前仍在进行。"""
    current = _as_utc(now)
    start = contest_start_utc(contest)
    if current is None or start is None:
        return False
    cutoff = current + timedelta(days=max(1, int(days)))
    if start > cutoff:
        return False
    end = contest_end_utc(contest)
    if end is not None:
        return end >= current
    return start >= current


def log_plugin(name: str, message: str) -> None:
    logger.info("[%s] %s", name, message)
