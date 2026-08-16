"""各平台比赛数据抓取（aiohttp + 公开接口/页面）。"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import aiohttp

from astrbot.api import logger

from src.models import PLATFORM_LABELS, Contest
from src.utils import LENTILLE_RE, USER_AGENT, fetch_text_with_retry

CF_API_URL = "https://codeforces.com/api/contest.list"
NC_CALENDAR_URL = "https://ac.nowcoder.com/acm/calendar/contest"
NC_REFERER = "https://ac.nowcoder.com/acm/contest/calendar"
LUOGU_CONTEST_URL = "https://www.luogu.com.cn/contest/list"


class ContestFetcher:
    """比赛抓取器，带 5 分钟内存缓存。"""

    def __init__(self, cache_ttl: int = 300) -> None:
        self.cache_ttl = cache_ttl
        self._cache: Dict[str, Tuple[float, List[Contest]]] = {}
        self.session: Optional[aiohttp.ClientSession] = None

    async def initialize(self) -> None:
        if self.session is None:
            self.session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT}
            )

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def fetch_platform(
        self, platform: str, force: bool = False
    ) -> Tuple[List[Contest], Optional[str]]:
        """抓取指定平台，返回 (比赛列表, 错误提示)；失败时列表为空。"""
        if not force:
            cached = self._cache.get(platform)
            if cached and time.time() - cached[0] < self.cache_ttl:
                return cached[1], None
        label = PLATFORM_LABELS.get(platform, platform)
        try:
            if platform == "codeforces":
                contests = await self._fetch_codeforces()
            elif platform == "nowcoder":
                contests = await self._fetch_nowcoder_calendar("nowcoder")
            elif platform == "atcoder":
                contests = await self._fetch_nowcoder_calendar("atcoder")
            elif platform == "luogu":
                contests = await self._fetch_luogu()
            else:
                return [], "未知平台"
        except asyncio.TimeoutError:
            logger.error("比赛数据抓取超时: %s", platform)
            return [], f"⏰ {label} 数据获取超时，请稍后重试"
        except aiohttp.ClientError as exc:
            logger.error("比赛数据网络错误(%s): %s", platform, exc)
            return [], f"🌐 {label} 网络连接失败，请检查网络后重试"
        except (ValueError, KeyError, json.JSONDecodeError, re.error) as exc:
            logger.error("比赛数据解析失败(%s): %s", platform, exc)
            return [], f"🔧 {label} 数据格式有变化，请联系管理员修复"
        except Exception as exc:  # noqa: BLE001
            logger.error("比赛数据未知错误(%s): %s", platform, exc, exc_info=True)
            return [], f"❌ 获取 {label} 数据失败，请稍后重试"
        self._cache[platform] = (time.time(), contests)
        return contests, None

    async def fetch_all(
        self, force: bool = False
    ) -> Dict[str, Tuple[List[Contest], Optional[str]]]:
        out: Dict[str, Tuple[List[Contest], Optional[str]]] = {}
        for platform in PLATFORM_LABELS:
            contests, err = await self.fetch_platform(platform, force=force)
            out[platform] = (contests, err)
        return out

    # ------------------------------------------------------------------
    async def _fetch_codeforces(self) -> List[Contest]:
        if self.session is None:
            raise RuntimeError("session 未初始化")
        text = await fetch_text_with_retry(self.session, CF_API_URL)
        data = json.loads(text)
        if data.get("status") != "OK":
            raise ValueError(f"CF API status: {data.get('status')}")
        contests: List[Contest] = []
        for item in data.get("result") or []:
            if item.get("phase") != "BEFORE":
                continue
            start = datetime.fromtimestamp(
                int(item["startTimeSeconds"]), tz=timezone.utc
            )
            contests.append(
                Contest(
                    platform="codeforces",
                    name=str(item.get("name") or ""),
                    start_time=start,
                    duration_minutes=int(item.get("durationSeconds") or 0) // 60,
                    url=f"https://codeforces.com/contest/{item.get('id')}",
                    contest_id=str(item.get("id") or ""),
                )
            )
        contests.sort(key=lambda c: c.start_time)
        return contests

    async def _fetch_nowcoder_calendar(self, platform: str) -> List[Contest]:
        """牛客比赛日历：同时包含牛客与 AtCoder 比赛，按 ojName/名称过滤。"""
        now = datetime.now(timezone.utc)
        # 抓取当前月与下个月，避免月末时下月比赛被漏掉
        months: List[str] = [f"{now.year}-{now.month}"]
        if now.month == 12:
            months.append(f"{now.year + 1}-1")
        else:
            months.append(f"{now.year}-{now.month + 1}")
        merged: List[Contest] = []
        seen: set = set()
        for month in months:
            for contest in await self._fetch_nc_month(platform, month):
                key = (contest.platform, contest.contest_id)
                if key not in seen:
                    seen.add(key)
                    merged.append(contest)
        merged.sort(key=lambda c: c.start_time)
        return merged

    async def _fetch_nc_month(self, platform: str, month: str) -> List[Contest]:
        if self.session is None:
            raise RuntimeError("session 未初始化")
        params = {
            "token": "",
            "month": month,
            "_": str(int(time.time() * 1000)),
        }
        headers = {
            "Referer": NC_REFERER,
            "X-Requested-With": "XMLHttpRequest",
        }
        text = await fetch_text_with_retry(
            self.session, NC_CALENDAR_URL, params=params, headers=headers
        )
        data = json.loads(text)
        if str(data.get("code")) not in ("0", "None"):
            raise ValueError(f"牛客接口错误: {data.get('msg')}")
        contests: List[Contest] = []
        for item in data.get("data") or []:
            oj_name = str(item.get("ojName") or "")
            name = str(item.get("contestName") or "")
            if platform == "atcoder":
                if "atcoder" not in oj_name.lower() and "AtCoder" not in name:
                    continue
            elif platform == "nowcoder":
                if "牛客" not in oj_name and "牛客" not in name:
                    continue
            try:
                start = datetime.fromtimestamp(
                    int(item["startTime"]) / 1000, tz=timezone.utc
                )
                end_ts = item.get("endTime")
                end = (
                    datetime.fromtimestamp(int(end_ts) / 1000, tz=timezone.utc)
                    if end_ts
                    else None
                )
            except (KeyError, TypeError, ValueError):
                continue
            duration = int((end - start).total_seconds() // 60) if end else 0
            contests.append(
                Contest(
                    platform=platform,
                    name=name,
                    start_time=start,
                    end_time=end,
                    duration_minutes=duration,
                    url=str(item.get("link") or ""),
                    contest_id=str(item.get("contestId") or ""),
                )
            )
        return contests

    async def _fetch_luogu(self) -> List[Contest]:
        """洛谷比赛列表：页面公开可访问，无需登录 Cookie。"""
        if self.session is None:
            raise RuntimeError("session 未初始化")
        text = await fetch_text_with_retry(self.session, LUOGU_CONTEST_URL)
        match = LENTILLE_RE.search(text)
        if not match:
            raise ValueError("未找到 lentille-context 数据")
        payload = json.loads(match.group(1))
        rows = payload["data"]["contests"]["result"]
        now_ts = int(time.time())
        contests: List[Contest] = []
        for item in rows:
            start_ts = int(item.get("startTime") or 0)
            end_ts = int(item.get("endTime") or 0)
            if start_ts <= now_ts:
                continue
            start = datetime.fromtimestamp(start_ts, tz=timezone.utc)
            end = datetime.fromtimestamp(end_ts, tz=timezone.utc) if end_ts else None
            duration = int((end - start).total_seconds() // 60) if end else 0
            contests.append(
                Contest(
                    platform="luogu",
                    name=str(item.get("name") or ""),
                    start_time=start,
                    end_time=end,
                    duration_minutes=duration,
                    url=f"https://www.luogu.com.cn/contest/{item.get('id')}",
                    contest_id=str(item.get("id") or ""),
                )
            )
        contests.sort(key=lambda c: c.start_time)
        return contests
