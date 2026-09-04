"""各平台比赛数据抓取（aiohttp + 公开接口/页面）。"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import aiohttp

from astrbot.api import logger

from src.models import (
    OFFLINE_PLATFORM,
    PLATFORM_LABELS,
    QUERY_PLATFORMS,
    Contest,
    OfflineContest,
)
from src.utils import LENTILLE_RE, USER_AGENT, fetch_text_with_retry

CF_API_URL = "https://codeforces.com/api/contest.list"
NC_CALENDAR_URL = "https://ac.nowcoder.com/acm/calendar/contest"
NC_REFERER = "https://ac.nowcoder.com/acm/contest/calendar"
LUOGU_CONTEST_URL = "https://www.luogu.com.cn/contest/list"
ATCODER_CONTESTS_URL = "https://atcoder.jp/contests/"
XCPC_DOMAINS = (
    "https://www.xcpc.link/",
    "https://www.xcpc.ink/",
)
XCPC_SCRIPT_RE = re.compile(
    r"<script\b[^>]*\bsrc=[\"']([^\"']+\.js(?:\?[^\"']*)?)[\"']",
    re.I,
)
XCPC_SCRIPT_IMPORT_RE = re.compile(
    r"[`\"']([^`\"']+\.js(?:\?[^`\"']*)?)[`\"']",
    re.I,
)
XCPC_SCHEDULE_MARKER_RE = re.compile(
    r"=\s*\[\s*\{\s*(?:startDate|[\"']startDate[\"'])\s*:"
)


class ContestFetcher:
    """比赛抓取器，带 5 分钟内存缓存。"""

    def __init__(self, cache_ttl: int = 300) -> None:
        self.cache_ttl = cache_ttl
        self._cache: Dict[str, Tuple[float, list]] = {}
        self._source_urls: Dict[str, str] = {}
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
    ) -> Tuple[list, Optional[str]]:
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
                contests = await self._fetch_atcoder()
            elif platform == "luogu":
                contests = await self._fetch_luogu()
            elif platform == OFFLINE_PLATFORM:
                contests = await self._fetch_offline()
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
    ) -> Dict[str, Tuple[list, Optional[str]]]:
        out: Dict[str, Tuple[list, Optional[str]]] = {}
        for platform in QUERY_PLATFORMS:
            contests, err = await self.fetch_platform(platform, force=force)
            out[platform] = (contests, err)
        return out

    def source_url(self, platform: str) -> str:
        """返回最近一次成功抓取该平台使用的数据源地址。"""
        return self._source_urls.get(platform, "")

    def source_text(self, platform: str) -> str:
        """返回适合展示给用户的数据源说明。"""
        actual = self.source_url(platform)
        if actual:
            return actual
        if platform == OFFLINE_PLATFORM:
            return f"{XCPC_DOMAINS[0]}（备用：{XCPC_DOMAINS[1]}）"
        return ""

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

    async def _fetch_atcoder(self) -> List[Contest]:
        """直接解析 AtCoder 官网赛程表（Upcoming Contests）。

        牛客比赛日历对 AtCoder 收录不全（实测 9 月起为空），因此 AtCoder
        改用官网页面解析，保证拿到全部未开始比赛。
        """
        if self.session is None:
            raise RuntimeError("session 未初始化")
        text = await fetch_text_with_retry(self.session, ATCODER_CONTESTS_URL)
        match = re.search(
            r'<div id="contest-table-upcoming">(.*?)'
            r'(?:<div id="contest-table-|</main>|$)',
            text,
            re.S,
        )
        if not match:
            raise ValueError("未找到 AtCoder upcoming 赛程")
        section = match.group(1)
        contests: List[Contest] = []
        for row in re.findall(r"<tr>(.*?)</tr>", section, re.S):
            link = re.search(r'href="(/contests/[^"]+)"[^>]*>([^<]+)</a>', row)
            time_tag = re.search(r"<time[^>]*>([^<]+)</time>", row)
            if not link or not time_tag:
                continue
            try:
                start = datetime.strptime(
                    time_tag.group(1).strip(), "%Y-%m-%d %H:%M:%S%z"
                )
                start = start.astimezone(timezone.utc)
            except ValueError:
                continue
            duration = re.search(
                r'<td class="text-center">(\d{1,2}):(\d{2})</td>', row
            )
            duration_minutes = (
                int(duration.group(1)) * 60 + int(duration.group(2))
                if duration
                else 0
            )
            slug = link.group(1)
            contests.append(
                Contest(
                    platform="atcoder",
                    name=link.group(2).strip(),
                    start_time=start,
                    duration_minutes=duration_minutes,
                    url=f"https://atcoder.jp{slug}",
                    contest_id=slug.split("/")[-1],
                )
            )
        contests.sort(key=lambda c: c.start_time)
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

    # ------------------------------------------------------------------
    # XCPC Link 线下赛程
    # ------------------------------------------------------------------
    async def _fetch_offline(self) -> List[OfflineContest]:
        """从 XCPC Link 首页加载的 JS 赛程数组中提取线下赛事。"""
        errors: List[str] = []
        for domain in XCPC_DOMAINS:
            try:
                contests = await self._fetch_offline_from_domain(domain)
                self._source_urls[OFFLINE_PLATFORM] = domain
                return contests
            except Exception as exc:  # noqa: BLE001 - 尝试备用域名
                errors.append(f"{domain}: {exc}")
                logger.warning("XCPC Link 数据源抓取失败（%s）：%s", domain, exc)
        raise RuntimeError(
            "XCPC Link 主站和备用域名均无法访问；" + "；".join(errors)
        )

    async def _fetch_offline_from_domain(self, domain: str) -> List[OfflineContest]:
        if self.session is None:
            raise RuntimeError("session 未初始化")
        page = await fetch_text_with_retry(self.session, domain)
        script_urls = [urljoin(domain, src) for src in XCPC_SCRIPT_RE.findall(page)]
        if not script_urls:
            raise ValueError("首页未找到赛程脚本")

        pending = list(dict.fromkeys(script_urls))
        visited = set()
        last_error: Optional[Exception] = None
        # Vite 会把首页组件拆成动态 import 的 chunk；递归读取同源 JS，
        # 这样不依赖浏览器执行 Vue，也能拿到嵌入 chunk 的赛程数组。
        while pending and len(visited) < 32:
            script_url = pending.pop(0)
            if script_url in visited:
                continue
            visited.add(script_url)
            script = ""
            try:
                script = await fetch_text_with_retry(self.session, script_url)
                rows = self._parse_xcpc_schedule(script, domain)
                if rows:
                    rows.sort(key=lambda item: item.start_date)
                    return rows
            except Exception as exc:  # noqa: BLE001 -继续尝试其他脚本
                last_error = exc
                # 当前脚本不是赛程 chunk 时继续检查它引用的 JS。
            for imported in XCPC_SCRIPT_IMPORT_RE.findall(script):
                imported_url = urljoin(script_url, imported)
                if imported_url not in visited and imported_url not in pending:
                    pending.append(imported_url)
        if last_error:
            raise last_error
        raise ValueError("赛程脚本中未找到线下赛事数据")

    @classmethod
    def _parse_xcpc_schedule(
        cls, script: str, source_url: str
    ) -> List[OfflineContest]:
        """解析压缩 JS 中形如 ``[{startDate:`...`...}]`` 的赛程数组。"""
        array_text = cls._extract_js_array(script)
        objects = cls._split_js_objects(array_text)
        contests: List[OfflineContest] = []
        for obj in objects:
            start_text = cls._js_field(obj, "startDate")
            category = cls._js_field(obj, "category")
            venue = cls._js_field(obj, "venue")
            if not start_text or not category:
                continue
            # XCPC Link 同时收录网络赛；本功能只返回线下站点。
            if venue and ("网络赛" in venue or "线上" in venue):
                continue
            try:
                start_date = date.fromisoformat(start_text)
                end_text = cls._js_field(obj, "endDate")
                end_date = date.fromisoformat(end_text) if end_text else None
            except ValueError:
                continue

            name = cls._format_xcpc_name(category, venue)
            official = cls._js_field(obj, "officialWebsite")
            if official:
                official = urljoin(source_url, official)
            contests.append(
                OfflineContest(
                    name=name,
                    start_date=start_date,
                    end_date=end_date,
                    venue=venue,
                    organizer=cls._js_field(obj, "organizer"),
                    problem_setter=cls._js_field(obj, "problemSetter"),
                    official_url=official,
                    allocation_plan=cls._js_field(obj, "allocationPlan"),
                    source_url=source_url,
                )
            )
        return contests

    @staticmethod
    def _format_xcpc_name(category: str, venue: str) -> str:
        if not venue:
            return category
        if "赛" in venue:
            return f"{category} {venue}"
        return f"{category} {venue}站"

    @classmethod
    def _extract_js_array(cls, script: str) -> str:
        marker = XCPC_SCHEDULE_MARKER_RE.search(script)
        if marker is None:
            raise ValueError("未找到 XCPC Link 赛程数组")
        start = script.find("[", marker.start(), marker.end())
        if start < 0:
            raise ValueError("XCPC Link 赛程数组格式异常")
        depth = 0
        index = start
        while index < len(script):
            char = script[index]
            if char in "`'\"":
                index = cls._skip_js_string(script, index)
                continue
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    return script[start : index + 1]
            index += 1
        raise ValueError("XCPC Link 赛程数组未闭合")

    @classmethod
    def _split_js_objects(cls, array_text: str) -> List[str]:
        objects: List[str] = []
        object_start: Optional[int] = None
        depth = 0
        index = 0
        while index < len(array_text):
            char = array_text[index]
            if char in "`'\"":
                index = cls._skip_js_string(array_text, index)
                continue
            if char == "{" and depth == 0:
                object_start = index
                depth = 1
            elif char == "{" and depth > 0:
                depth += 1
            elif char == "}" and depth > 0:
                depth -= 1
                if depth == 0 and object_start is not None:
                    objects.append(array_text[object_start : index + 1])
                    object_start = None
            index += 1
        return objects

    @staticmethod
    def _skip_js_string(text: str, start: int) -> int:
        """返回 JS 字符串结束后的下标，兼容模板/单引号/双引号字符串。"""
        quote = text[start]
        index = start + 1
        while index < len(text):
            if text[index] == "\\":
                index += 2
                continue
            if text[index] == quote:
                return index + 1
            index += 1
        return len(text)

    @classmethod
    def _js_field(cls, obj: str, field: str) -> str:
        pattern = re.compile(
            rf"(?:\b{re.escape(field)}\b|[\"']{re.escape(field)}[\"'])"
            r"\s*:\s*(?:"
            r"`((?:\\.|[^`])*)`|"
            r"'((?:\\.|[^'\\])*)'|"
            r'"((?:\\.|[^"\\])*)"'
            r")",
            re.S,
        )
        match = pattern.search(obj)
        if match is None:
            return ""
        value = next((item for item in match.groups() if item is not None), "")
        return cls._decode_js_string(value).strip()

    @staticmethod
    def _decode_js_string(value: str) -> str:
        """解码赛程字段中的常见 JS 转义，不依赖浏览器或 JS 运行时。"""
        escapes = {
            "n": "\n",
            "r": "\r",
            "t": "\t",
            "b": "\b",
            "f": "\f",
            "v": "\v",
            "\\": "\\",
            "`": "`",
            "'": "'",
            '"': '"',
        }
        result: List[str] = []
        index = 0
        while index < len(value):
            char = value[index]
            if char != "\\" or index + 1 >= len(value):
                result.append(char)
                index += 1
                continue
            next_char = value[index + 1]
            if next_char == "u" and index + 2 < len(value):
                if value[index + 2] == "{" and "}" in value[index + 3 :]:
                    end = value.index("}", index + 3)
                    try:
                        result.append(chr(int(value[index + 3 : end], 16)))
                        index = end + 1
                        continue
                    except ValueError:
                        pass
                hex_text = value[index + 2 : index + 6]
                if len(hex_text) == 4 and re.fullmatch(r"[0-9a-fA-F]{4}", hex_text):
                    result.append(chr(int(hex_text, 16)))
                    index += 6
                    continue
            if next_char == "x" and index + 3 < len(value):
                hex_text = value[index + 2 : index + 4]
                if re.fullmatch(r"[0-9a-fA-F]{2}", hex_text):
                    result.append(chr(int(hex_text, 16)))
                    index += 4
                    continue
            result.append(escapes.get(next_char, next_char))
            index += 2
        return "".join(result)
