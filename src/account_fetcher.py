"""四个竞赛平台的公开账号资料抓取、绑定校验与缓存。"""
from __future__ import annotations

import asyncio
import html
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlencode, urlparse

import aiohttp

from astrbot.api import logger

from .account_models import (
    ACCOUNT_PLATFORMS,
    AccountFetchError,
    AccountProfile,
)
from .models import CN_TZ
from .utils import LENTILLE_RE, USER_AGENT, fetch_text_with_retry

CF_API_URL = "https://codeforces.com/api"
NOWCODER_PROFILE_URL = "https://ac.nowcoder.com/acm/contest/profile/{uid}"
NOWCODER_PRACTICE_URL = (
    "https://ac.nowcoder.com/acm/contest/profile/{uid}/practice-coding"
)
NOWCODER_PROBLEM_LIST_URL = "https://ac.nowcoder.com/acm/problem/list"
NOWCODER_RATING_BASIC_URL = (
    "https://ac.nowcoder.com/acm/contest/rating-basic?uid={uid}"
)
NOWCODER_RATING_HISTORY_URL = (
    "https://ac.nowcoder.com/acm/contest/rating-history?uid={uid}"
)
NOWCODER_CONTEST_HISTORY_URL = (
    "https://ac.nowcoder.com/acm/contest/profile/contest-joined-history"
)
NOWCODER_RATING_INDEX_URL = (
    "https://ac.nowcoder.com/acm/contest/rating-index"
)
LUOGU_PROFILE_URL = "https://www.luogu.com/user/{uid}"
LUOGU_LEGACY_PROFILE_URL = "https://www.luogu.com.cn/user/{uid}"
LUOGU_PRACTICE_URL = "https://www.luogu.com/user/{uid}/practice"
LUOGU_LEGACY_PRACTICE_URL = "https://www.luogu.com.cn/user/{uid}/practice"
LUOGU_API_URL = "https://www.luogu.com.cn/api/user/show?uid={uid}"
ATCODER_PROFILE_URL = "https://atcoder.jp/users/{handle}?lang=en"
ATCODER_HISTORY_JSON_URL = "https://atcoder.jp/users/{handle}/history/json"
ATCODER_PROBLEM_MODELS_URL = (
    "https://kenkoooo.com/atcoder/resources/problem-models.json"
)
ATCODER_SUBMISSIONS_URL = (
    "https://kenkoooo.com/atcoder/atcoder-api/v3/user/submissions"
)

PROFILE_CACHE_TTL = 10 * 60
DETAIL_CACHE_TTL = 10 * 60
ANALYSIS_CACHE_TTL = 12 * 60 * 60
ANALYSIS_FAILURE_CACHE_TTL = 5 * 60
RESOURCE_CACHE_TTL = 24 * 60 * 60
CF_MIN_REQUEST_INTERVAL = 2.1
RATING_HISTORY_LIMIT = 200
CF_SUBMISSION_SCAN_LIMIT = 10000
NOWCODER_ANALYSIS_PAGE_SIZE = 100
NOWCODER_ANALYSIS_MAX_PAGES = 20
NOWCODER_PROBLEM_META_LIMIT = 300
NOWCODER_PROBLEM_META_CONCURRENCY = 6
NOWCODER_DIFFICULTY_BUCKETS = (
    ("≤599", None, 599),
    ("600–999", 600, 999),
    ("1000–1399", 1000, 1399),
    ("1400–1799", 1400, 1799),
    ("1800–2199", 1800, 2199),
    ("2200–2599", 2200, 2599),
    ("2600+", 2600, None),
)
LUOGU_DIFFICULTY_LABELS = {
    0: "暂无评定",
    1: "入门",
    2: "普及−",
    3: "普及",
    4: "普及+/提高−",
    5: "提高",
    6: "提高+/省选−",
    7: "省选/NOI−",
    8: "NOI/NOI+/CTS",
}
ATCODER_SUBMISSION_PAGE_SIZE = 500
ATCODER_SUBMISSION_SCAN_LIMIT = 10000
ATCODER_DIFFICULTY_BUCKETS = (
    ("≤399", None, 399),
    ("400–799", 400, 799),
    ("800–1199", 800, 1199),
    ("1200–1599", 1200, 1599),
    ("1600–1999", 1600, 1999),
    ("2000–2399", 2000, 2399),
    ("2400+", 2400, None),
)
CF_DIFFICULTY_BUCKETS = (
    ("≤999", None, 999),
    ("1000–1199", 1000, 1199),
    ("1200–1399", 1200, 1399),
    ("1400–1599", 1400, 1599),
    ("1600–1799", 1600, 1799),
    ("1800–1999", 1800, 1999),
    ("2000–2199", 2000, 2199),
    ("2200–2399", 2200, 2399),
    ("2400–2599", 2400, 2599),
    ("2600–2799", 2600, 2799),
    ("2800–2999", 2800, 2999),
    ("3000–3199", 3000, 3199),
    ("3200+", 3200, None),
)

_CF_HANDLE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ATCODER_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_NOWCODER_UID_RE = re.compile(r"^\d{1,20}$")
_LUOGU_UID_RE = re.compile(r"^\d{1,20}$")
_HTML_TAG_RE = re.compile(r"<[^>]+>", re.S)
_ATCODER_HISTORY_RE = re.compile(
    r"var\s+rating_history\s*=\s*(\[.*?\])\s*;\s*</script>",
    re.S,
)
_NOWCODER_PRACTICE_ROW_RE = re.compile(
    r"<tr\b[^>]*>(.*?)</tr>",
    re.I | re.S,
)
_NOWCODER_STATE_RE = re.compile(
    r'<div\s+class=["\']my-state-item["\'][^>]*>.*?'
    r'<div\s+class=["\']state-num["\']>(.*?)</div>\s*'
    r"<span>(.*?)</span>",
    re.I | re.S,
)
_NOWCODER_PAGE_TOTAL_RE = re.compile(
    r'<ul\b[^>]*\bdata-total=["\'](\d+)["\']',
    re.I,
)


def _clean_text(value: object) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = _HTML_TAG_RE.sub(" ", text)
    return " ".join(text.replace("\xa0", " ").split()).strip()


def _parse_int(value: object) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "—", "N/A", "null", "None"}:
        return None
    match = re.search(r"-?\d+", text)
    if not match:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


def _parse_timestamp(value: object) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000 if number > 10_000_000_000 else number
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).replace(
                tzinfo=CN_TZ
            ).timestamp()
        except ValueError:
            continue
    return None


def _distribution_rows(
    counts: Dict[str, int],
    *,
    limit: Optional[int] = None,
    unknown_labels: tuple[str, ...] = (),
) -> List[Dict[str, Any]]:
    items = sorted(
        (
            (str(label), int(count))
            for label, count in counts.items()
            if count > 0
        ),
        key=lambda item: (-item[1], item[0]),
    )
    if unknown_labels:
        unknown = set(unknown_labels)
        items.sort(
            key=lambda item: (
                1 if item[0] in unknown else 0,
                -item[1],
                item[0],
            )
        )
    if limit is not None:
        items = items[:max(1, int(limit))]
    return [
        {"label": label, "count": count}
        for label, count in items
    ]


def _nested_dicts(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _nested_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nested_dicts(child)


def _first_value(data: object, *keys: str) -> object:
    for item in _nested_dicts(data):
        for key in keys:
            if key in item and item[key] not in (None, ""):
                return item[key]
    return None


def normalize_account_identifier(platform: str, value: object) -> str:
    """把用户名、UID 或平台主页链接规范化为抓取标识。"""
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else "")
    path = unquote(parsed.path).rstrip("/") if parsed.scheme else text

    if platform == "codeforces":
        if parsed.scheme:
            match = re.search(r"/(?:profile|user)/([^/?#]+)", path, re.I)
            text = match.group(1) if match else ""
        else:
            text = text.removeprefix("@").strip()
        return text if _CF_HANDLE_RE.fullmatch(text) else ""

    if platform == "atcoder":
        if parsed.scheme:
            match = re.search(r"/users/([^/?#]+)", path, re.I)
            text = match.group(1) if match else ""
        return text if _ATCODER_HANDLE_RE.fullmatch(text) else ""

    if platform == "nowcoder":
        if parsed.scheme:
            match = re.search(r"/acm/contest/profile/(\d+)", path, re.I)
            text = match.group(1) if match else ""
        return text if _NOWCODER_UID_RE.fullmatch(text) else ""

    if platform == "luogu":
        if parsed.scheme:
            match = re.search(r"/user/(\d+)", path, re.I)
            text = match.group(1) if match else ""
        return text if _LUOGU_UID_RE.fullmatch(text) else ""

    return ""


class AccountFetcher:
    """公开资料抓取器；缓存只保存在进程内，避免验证码落盘。"""

    def __init__(
        self,
        session: Optional[aiohttp.ClientSession] = None,
        cache_ttl: int = PROFILE_CACHE_TTL,
    ) -> None:
        self.session = session
        self._owns_session = False
        self.cache_ttl = max(30, int(cache_ttl))
        self._cache: Dict[
            Tuple[str, str, bool, bool, bool],
            Tuple[float, AccountProfile],
        ] = {}
        self._locks: Dict[
            Tuple[str, str, bool, bool, bool],
            asyncio.Lock,
        ] = {}
        self._resource_cache: Dict[str, Tuple[float, object]] = {}
        self._resource_locks: Dict[str, asyncio.Lock] = {}
        self._analysis_semaphore = asyncio.Semaphore(2)
        self._nowcoder_problem_cache: Dict[
            str, Tuple[float, Dict[str, Any]]
        ] = {}
        self._cf_lock = asyncio.Lock()
        self._cf_last_request = 0.0

    async def initialize(
        self, session: Optional[aiohttp.ClientSession] = None
    ) -> None:
        if session is not None:
            self.session = session
            self._owns_session = False
        elif self.session is None:
            self.session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT}
            )
            self._owns_session = True

    async def close(self) -> None:
        if self._owns_session and self.session is not None:
            await self.session.close()
        self.session = None
        self._owns_session = False

    async def get_profile(
        self,
        platform: str,
        identifier: str,
        *,
        detail: bool = False,
        force: bool = False,
        include_submissions: bool = True,
        include_difficulty: bool = False,
        include_analysis: bool = False,
    ) -> AccountProfile:
        if platform not in ACCOUNT_PLATFORMS:
            raise AccountFetchError("不支持的平台")
        normalized = normalize_account_identifier(platform, identifier)
        if not normalized:
            raise AccountFetchError(self.invalid_identifier_message(platform), temporary=False)
        analysis_requested = bool(
            detail and (include_difficulty or include_analysis)
        )
        key = (
            platform,
            normalized.casefold(),
            detail,
            bool(detail and include_submissions),
            analysis_requested,
        )
        now = time.time()
        cached = self._cache.get(key)
        ttl = (
            ANALYSIS_CACHE_TTL
            if analysis_requested
            else DETAIL_CACHE_TTL
            if detail
            else self.cache_ttl
        )
        if (
            not force
            and cached
            and now - cached[0]
            < self._cache_ttl_for_profile(cached[1], ttl)
        ):
            return cached[1]

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._cache.get(key)
            if (
                not force
                and cached
                and time.time() - cached[0]
                < self._cache_ttl_for_profile(cached[1], ttl)
            ):
                return cached[1]
            profile = await self._fetch_profile(
                platform,
                normalized,
                detail,
                include_submissions=include_submissions,
                include_difficulty=analysis_requested,
                include_analysis=analysis_requested,
            )
            profile.fetched_at = time.time()
            self._cache[key] = (profile.fetched_at, profile)
            # 详细资料可以复用为摘要资料，减少后续排行请求。
            if detail:
                self._cache[
                    (
                        platform,
                        normalized.casefold(),
                        False,
                        False,
                        False,
                    )
                ] = (
                    profile.fetched_at,
                    profile,
                )
            return profile

    @staticmethod
    def _cache_ttl_for_profile(
        profile: object,
        default_ttl: int,
    ) -> int:
        analysis = getattr(profile, "analysis", {}) or {}
        if (
            isinstance(analysis, dict)
            and analysis.get("analysis_status") == "unavailable"
        ):
            return min(default_ttl, ANALYSIS_FAILURE_CACHE_TTL)
        return default_ttl

    async def get_profiles(
        self,
        platform: str,
        identifiers: List[str],
        *,
        detail: bool = False,
        force: bool = False,
        include_submissions: bool = True,
        include_difficulty: bool = False,
        include_analysis: bool = False,
    ) -> Dict[str, AccountProfile]:
        """批量读取账号资料；Codeforces 摘要使用一次 user.info 请求。"""
        normalized = []
        for identifier in identifiers:
            value = normalize_account_identifier(platform, identifier)
            if value and value.casefold() not in {
                item.casefold() for item in normalized
            }:
                normalized.append(value)
        if not normalized:
            return {}
        if platform != "codeforces" or detail:
            profiles = await asyncio.gather(
                *(
                    self.get_profile(
                        platform,
                        identifier,
                        detail=detail,
                        force=force,
                        include_submissions=include_submissions,
                        include_difficulty=include_difficulty,
                        include_analysis=include_analysis,
                    )
                    for identifier in normalized
                )
            )
            return {
                profile.platform_user_id.casefold(): profile
                for profile in profiles
            }

        result: Dict[str, AccountProfile] = {}
        missing = []
        now = time.time()
        for identifier in normalized:
            key = (
                "codeforces",
                identifier.casefold(),
                False,
                False,
                False,
            )
            cached = self._cache.get(key)
            if not force and cached and now - cached[0] < self.cache_ttl:
                result[identifier.casefold()] = cached[1]
            else:
                missing.append(identifier)

        for offset in range(0, len(missing), 50):
            chunk = missing[offset : offset + 50]
            data = await self._cf_json(
                "user.info", {"handles": ";".join(chunk)}
            )
            if not isinstance(data, dict) or data.get("status") != "OK":
                raise AccountFetchError("Codeforces 用户信息暂时无法获取")
            for user in data.get("result") or []:
                profile = self._profile_from_codeforces_user(user)
                profile.fetched_at = time.time()
                key = (
                    "codeforces",
                    profile.handle.casefold(),
                    False,
                    False,
                    False,
                )
                self._cache[key] = (profile.fetched_at, profile)
                result[profile.handle.casefold()] = profile
        return result

    async def verify(
        self, platform: str, identifier: str, token: str
    ) -> Tuple[AccountProfile, bool]:
        profile = await self.get_profile(platform, identifier, force=True)
        field = await self.get_verification_value(
            platform,
            identifier,
            profile=profile,
            force=True,
        )
        return profile, bool(token and token.casefold() in field.casefold())

    async def get_verification_value(
        self,
        platform: str,
        identifier: str,
        *,
        profile: Optional[AccountProfile] = None,
        force: bool = False,
    ) -> str:
        """读取绑定校验字段；洛谷个人介绍来自 .com 用户页。"""
        if profile is not None:
            return str(
                getattr(profile, "verification_value", "") or ""
            )
        normalized = normalize_account_identifier(platform, identifier)
        if not normalized:
            raise AccountFetchError(
                self.invalid_identifier_message(platform),
                temporary=False,
            )
        profile = await self.get_profile(
            platform,
            normalized,
            detail=False,
            force=force,
        )
        return str(profile.verification_value or "")

    @staticmethod
    def invalid_identifier_message(platform: str) -> str:
        return {
            "codeforces": "Codeforces 用户名或主页链接格式不正确",
            "nowcoder": "请填写牛客数字用户 ID 或个人主页链接",
            "luogu": "请填写洛谷数字 UID 或个人主页链接",
            "atcoder": "AtCoder 用户名或主页链接格式不正确",
        }.get(platform, "账号格式不正确")

    async def _fetch_profile(
        self,
        platform: str,
        identifier: str,
        detail: bool,
        *,
        include_submissions: bool = True,
        include_difficulty: bool = False,
        include_analysis: bool = False,
    ) -> AccountProfile:
        if platform == "codeforces":
            return await self._fetch_codeforces(
                identifier,
                detail,
                include_submissions=include_submissions,
                include_difficulty=include_difficulty or include_analysis,
            )
        if platform == "nowcoder":
            return await self._fetch_nowcoder(
                identifier,
                detail,
                include_analysis=include_analysis,
            )
        if platform == "luogu":
            return await self._fetch_luogu(
                identifier,
                detail,
                include_analysis=include_analysis,
            )
        if platform == "atcoder":
            return await self._fetch_atcoder(
                identifier,
                detail,
                include_analysis=include_analysis,
            )
        raise AccountFetchError("不支持的平台", temporary=False)

    async def _fetch_text(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        retries: int = 2,
        timeout: float = 10.0,
    ) -> str:
        if self.session is None:
            raise AccountFetchError("账号抓取器尚未初始化")
        try:
            return await fetch_text_with_retry(
                self.session,
                url,
                retries=retries,
                timeout=timeout,
                headers=headers or {},
            )
        except asyncio.TimeoutError as exc:
            raise AccountFetchError("平台响应超时，请稍后重试") from exc
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                raise AccountFetchError("未找到该平台账号", temporary=False) from exc
            raise AccountFetchError("平台暂时无法访问，请稍后重试") from exc
        except aiohttp.ClientError as exc:
            raise AccountFetchError("平台网络连接失败，请稍后重试") from exc
        except Exception as exc:  # noqa: BLE001
            raise AccountFetchError("平台暂时无法访问，请稍后重试") from exc

    async def _fetch_json(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        retries: int = 2,
        timeout: float = 10.0,
    ) -> object:
        text = await self._fetch_text(
            url,
            headers=headers,
            retries=retries,
            timeout=timeout,
        )
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise AccountFetchError("平台返回的数据格式异常，请稍后重试") from exc

    async def _cf_json(self, method: str, params: Dict[str, str]) -> object:
        query = urlencode(params)
        url = f"{CF_API_URL}/{method}?{query}"
        if self.session is None:
            raise AccountFetchError("账号抓取器尚未初始化")
        async with self._cf_lock:
            wait = CF_MIN_REQUEST_INTERVAL - (
                time.monotonic() - self._cf_last_request
            )
            if wait > 0:
                await asyncio.sleep(wait)
            self._cf_last_request = time.monotonic()
            last_error: Optional[Exception] = None
            for attempt in range(2):
                try:
                    async with self.session.get(url, timeout=10) as response:
                        text = await response.text()
                        try:
                            data = json.loads(text)
                        except json.JSONDecodeError as exc:
                            raise AccountFetchError(
                                "Codeforces 返回的数据格式异常"
                            ) from exc
                        # Codeforces 在账号不存在时会返回 HTTP 400，但响应体仍是
                        # 标准 FAILED JSON；交给上层按 comment 分类。
                        if response.status < 500:
                            return data
                        response.raise_for_status()
                except AccountFetchError:
                    raise
                except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                    last_error = exc
                    if attempt == 0:
                        await asyncio.sleep(1)
            raise AccountFetchError(
                "Codeforces 网络连接失败，请稍后重试"
            ) from last_error

    async def _fetch_codeforces(
        self,
        handle: str,
        detail: bool,
        *,
        include_submissions: bool = True,
        include_difficulty: bool = False,
    ) -> AccountProfile:
        data = await self._cf_json("user.info", {"handles": handle})
        if not isinstance(data, dict) or data.get("status") != "OK":
            comment = str((data or {}).get("comment") or "") if isinstance(data, dict) else ""
            if "not found" in comment.casefold():
                raise AccountFetchError("未找到该 Codeforces 用户", temporary=False)
            raise AccountFetchError("Codeforces 用户信息暂时无法获取")
        rows = data.get("result") or []
        if not rows:
            raise AccountFetchError("未找到该 Codeforces 用户", temporary=False)
        user = rows[0]
        canonical = str(user.get("handle") or handle)
        profile = self._profile_from_codeforces_user(user)
        if detail:
            rating_data = await self._cf_json(
                "user.rating", {"handle": canonical}
            )
            if isinstance(rating_data, dict) and rating_data.get("status") == "OK":
                history = rating_data.get("result") or []
                profile.contest_count = len(history)
                parsed_history = self._parse_cf_history(
                    history, limit=RATING_HISTORY_LIMIT
                )
                profile.rating_history = parsed_history
                profile.recent_contests = parsed_history[:5]
                if parsed_history:
                    profile.recent_delta = parsed_history[0].get("delta")
            if include_submissions or include_difficulty:
                status_data = await self._cf_json(
                    "user.status",
                    {
                        "handle": canonical,
                        "from": "1",
                        "count": str(CF_SUBMISSION_SCAN_LIMIT),
                    },
                )
                if (
                    isinstance(status_data, dict)
                    and status_data.get("status") == "OK"
                ):
                    rows = status_data.get("result") or []
                    if include_submissions:
                        profile.recent_submissions = (
                            self._parse_cf_submissions(rows)
                        )
                    if include_difficulty:
                        (
                            profile.difficulty_distribution,
                            profile.solved_count,
                        ) = self._parse_cf_difficulty_distribution(rows)
                        profile.extra["difficulty_scan_limit"] = (
                            CF_SUBMISSION_SCAN_LIMIT
                        )
                        profile.extra["difficulty_scanned_submissions"] = len(
                            rows
                        )
                        profile.analysis = self._build_cf_analysis(
                            rows,
                            profile.difficulty_distribution,
                            profile.solved_count,
                        )
        return profile

    @classmethod
    def _profile_from_codeforces_user(
        cls, user: Dict[str, Any]
    ) -> AccountProfile:
        canonical = str(user.get("handle") or "")
        return AccountProfile(
            platform="codeforces",
            handle=canonical,
            platform_user_id=canonical,
            display_name=canonical,
            profile_url=f"https://codeforces.com/profile/{quote(canonical)}",
            verification_value=str(user.get("lastName") or ""),
            rating=_parse_int(user.get("rating")),
            rank_text=str(user.get("rank") or ""),
            max_rating=_parse_int(user.get("maxRating")),
            max_rank_text=str(user.get("maxRank") or ""),
            school=str(user.get("organization") or ""),
            organization=str(user.get("organization") or ""),
            country=str(user.get("country") or ""),
            city=str(user.get("city") or ""),
            color=cls._codeforces_color(str(user.get("rank") or "")),
            contribution=_parse_int(user.get("contribution")),
            # titlePhoto 是横向头图，头像卡片应优先使用 avatar。
            avatar_url=str(user.get("avatar") or user.get("titlePhoto") or ""),
            source_url="https://codeforces.com/api/user.info",
        )

    @staticmethod
    def _codeforces_color(rank: str) -> str:
        rank = rank.casefold()
        if "legendary" in rank:
            return "red"
        if "international grandmaster" in rank:
            return "red"
        if "grandmaster" in rank:
            return "red"
        if "master" in rank:
            return "orange"
        if "candidate" in rank:
            return "violet"
        if "expert" in rank:
            return "blue"
        if "specialist" in rank:
            return "cyan"
        if "pupil" in rank:
            return "green"
        return "gray"

    @staticmethod
    def _parse_cf_history(rows: list, limit: int) -> List[Dict[str, Any]]:
        out = []
        for row in reversed(rows[-limit:]):
            old = _parse_int(row.get("oldRating"))
            new = _parse_int(row.get("newRating"))
            out.append(
                {
                    "name": str(row.get("contestName") or ""),
                    "rank": _parse_int(row.get("rank")),
                    "delta": new - old if old is not None and new is not None else None,
                    "old_rating": old,
                    "rating": new,
                    "timestamp": _parse_timestamp(row.get("ratingUpdateTimeSeconds")),
                    "url": (
                        f"https://codeforces.com/contest/{row.get('contestId')}"
                        if row.get("contestId")
                        else ""
                    ),
                }
            )
        return out

    @staticmethod
    def _parse_cf_submissions(rows: list) -> List[Dict[str, Any]]:
        out = []
        for row in rows[:5]:
            problem = row.get("problem") or {}
            verdict = str(row.get("verdict") or "")
            out.append(
                {
                    "name": str(problem.get("name") or ""),
                    "verdict": verdict,
                    "language": str(row.get("programmingLanguage") or ""),
                    "timestamp": _parse_timestamp(row.get("creationTimeSeconds")),
                    "url": (
                        f"https://codeforces.com/contest/{row.get('contestId')}/submission/{row.get('id')}"
                        if row.get("contestId") and row.get("id")
                        else ""
                    ),
                }
            )
        return out

    @staticmethod
    def _parse_cf_difficulty_distribution(
        rows: list,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """统计已通过的不同 CF 题目难度分布。"""
        accepted_ratings: Dict[tuple, Optional[int]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("verdict") or "").upper() != "OK":
                continue
            problem = row.get("problem") or {}
            if not isinstance(problem, dict):
                problem = {}
            contest_id = problem.get("contestId") or row.get("contestId")
            index = problem.get("index") or row.get("problemIndex")
            problemset_name = (
                problem.get("problemsetName")
                or row.get("problemsetName")
                or ""
            )
            name = str(problem.get("name") or "").strip()
            if contest_id and index:
                key = ("contest", str(contest_id), str(index))
            elif problemset_name and index:
                key = ("set", str(problemset_name), str(index))
            elif name:
                key = ("name", name)
            else:
                # 没有稳定题目标识时不把多次提交重复计数。
                key = ("row", str(row.get("id") or len(accepted_ratings)))
            rating = _parse_int(problem.get("rating"))
            previous = accepted_ratings.get(key)
            if key in accepted_ratings and (
                previous is not None or rating is None
            ):
                continue
            accepted_ratings[key] = rating

        counts = [0] * len(CF_DIFFICULTY_BUCKETS)
        unknown_count = 0
        for rating in accepted_ratings.values():
            if rating is None:
                unknown_count += 1
                continue
            for position, (_, minimum, maximum) in enumerate(
                CF_DIFFICULTY_BUCKETS
            ):
                if (
                    (minimum is None or rating >= minimum)
                    and (maximum is None or rating <= maximum)
                ):
                    counts[position] += 1
                    break

        distribution = [
            {
                "label": label,
                "count": count,
            }
            for (label, _, _), count in zip(CF_DIFFICULTY_BUCKETS, counts)
            if count > 0
        ]
        if unknown_count > 0:
            distribution.append(
                {
                    "label": "未标分",
                    "count": unknown_count,
                }
            )
        return distribution, len(accepted_ratings)

    @staticmethod
    def _build_cf_analysis(
        rows: list,
        difficulty_distribution: List[Dict[str, Any]],
        solved_count: Optional[int],
    ) -> Dict[str, Any]:
        accepted_rows = [
            row
            for row in rows
            if isinstance(row, dict)
            and str(row.get("verdict") or "").upper() == "OK"
        ]
        language_counts: Dict[str, int] = {}
        active_days_30 = set()
        active_days_90 = set()
        now = time.time()
        for row in rows:
            if not isinstance(row, dict):
                continue
            language = (
                str(row.get("programmingLanguage") or "").strip()
                or "未标注"
            )
            language_counts[language] = language_counts.get(language, 0) + 1
            timestamp = _parse_timestamp(row.get("creationTimeSeconds"))
            if timestamp is None:
                continue
            age = now - timestamp
            day = datetime.fromtimestamp(timestamp, tz=CN_TZ).date()
            if 0 <= age <= 30 * 86400:
                active_days_30.add(day)
            if 0 <= age <= 90 * 86400:
                active_days_90.add(day)
        complete = len(rows) < CF_SUBMISSION_SCAN_LIMIT
        acceptance_rate = (
            round(len(accepted_rows) / len(rows) * 100, 1)
            if rows and complete
            else None
        )
        return {
            "source": "Codeforces 官方 user.status API",
            "coverage": (
                f"最近 {len(rows)} 条公开提交"
                + (
                    "（已读完当前公开记录）"
                    if complete
                    else f"（最多读取 {CF_SUBMISSION_SCAN_LIMIT} 条）"
                )
            ),
            "submission_count": len(rows),
            "accepted_submission_count": (
                len(accepted_rows) if complete else None
            ),
            "solved_count": solved_count,
            "acceptance_rate": acceptance_rate,
            "active_days_30": len(active_days_30),
            "active_days_90": len(active_days_90),
            "difficulty_title": "Codeforces 题目难度分布",
            "difficulty_distribution": list(difficulty_distribution),
            "language_distribution": _distribution_rows(
                language_counts,
                limit=8,
            ),
            "summary": [
                item
                for item in (
                    {"label": "提交", "value": len(rows)},
                    (
                        {"label": "通过题", "value": solved_count}
                        if solved_count is not None
                        else None
                    ),
                    (
                        {
                            "label": "提交通过率",
                            "value": f"{acceptance_rate:.1f}%",
                        }
                        if acceptance_rate is not None
                        else None
                    ),
                    {"label": "30日活跃", "value": f"{len(active_days_30)}天"},
                    {"label": "90日活跃", "value": f"{len(active_days_90)}天"},
                )
                if item is not None
            ],
        }

    async def _fetch_nowcoder(
        self,
        uid: str,
        detail: bool,
        *,
        include_analysis: bool = False,
    ) -> AccountProfile:
        profile_url = NOWCODER_PROFILE_URL.format(uid=uid)
        text = await self._fetch_text(
            profile_url,
            headers={"Accept-Language": "zh-CN,zh;q=0.9"},
        )
        if "window.curUser.id" not in text and "coder-name" not in text:
            raise AccountFetchError("未找到该牛客用户", temporary=False)
        name_match = re.search(
            r'class=["\'][^"\']*coder-name[^"\']*["\'][^>]*'
            r'data-title=["\']([^"\']+)["\']',
            text,
            re.I,
        )
        if not name_match:
            name_match = re.search(
                r'class=["\'][^"\']*coder-name[^"\']*["\'][^>]*>(.*?)</a>',
                text,
                re.I | re.S,
            )
        name = _clean_text(name_match.group(1)) if name_match else uid
        brief_match = re.search(
            r'<div\s+class=["\']coder-brief["\']>(.*?)</div>',
            text,
            re.I | re.S,
        )
        signature = _clean_text(brief_match.group(1)) if brief_match else ""
        if signature == "个性签名":
            signature = ""
        school_match = re.search(
            r'class=["\'][^"\']*edu-item[^"\']*["\'][^>]*>.*?'
            r'class=["\']coder-edu-txt["\']>(.*?)</span>',
            text,
            re.I | re.S,
        )
        school = _clean_text(school_match.group(1)) if school_match else ""
        # 页面结构可能因版本变化，直接按标签附近的 state-num 兜底。
        status_numbers = re.findall(
            r'class=["\'][^"\']*state-num[^"\']*["\'][^>]*>([^<]+)</',
            text,
            re.I,
        )
        rating = _parse_int(status_numbers[0]) if status_numbers else None
        rank = _parse_int(status_numbers[1]) if len(status_numbers) > 1 else None
        count_match = re.search(
            r'class=["\']state-num["\']>(\d+)</div>\s*<span>次比赛',
            text,
            re.I,
        )
        contest_count = _parse_int(count_match.group(1)) if count_match else None
        avatar_match = re.search(
            r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>',
            text[text.find("coder-info-wrap") : text.find("coder-info-wrap") + 5000],
            re.I,
        )
        avatar = avatar_match.group(1) if avatar_match else ""
        profile = AccountProfile(
            platform="nowcoder",
            handle=name,
            platform_user_id=uid,
            display_name=name,
            profile_url=profile_url,
            verification_value=signature,
            rating=rating,
            rating_rank=rank,
            contest_count=contest_count,
            school=school,
            color=self._nowcoder_color(rating),
            avatar_url=avatar,
            source_url=profile_url,
        )
        # rating-basic 的字段比页面展示更稳定；失败时保留页面解析结果。
        try:
            basic = await self._fetch_json(
                NOWCODER_RATING_BASIC_URL.format(uid=uid),
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            if isinstance(basic, dict) and str(basic.get("code")) in {"0", "None"}:
                data = basic.get("data") or {}
                profile.rating = _parse_int(data.get("rating")) or profile.rating
                profile.rating_rank = (
                    _parse_int(data.get("rank")) or profile.rating_rank
                )
                profile.contest_count = (
                    _parse_int(data.get("contestCount")) or profile.contest_count
                )
                profile.school = str(data.get("school") or profile.school)
                profile.avatar_url = str(
                    data.get("tinnyHeaderUrl") or profile.avatar_url
                )
        except AccountFetchError:
            pass
        if detail:
            try:
                history = await self._fetch_json(
                    NOWCODER_RATING_HISTORY_URL.format(uid=uid),
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )
                if isinstance(history, dict) and str(history.get("code")) in {"0", "None"}:
                    rows = history.get("data") or []
                    parsed_history = self._parse_nowcoder_history(rows)
                    profile.rating_history = parsed_history
                    profile.recent_contests = parsed_history[:5]
                    profile.max_rating = max(
                        (
                            item.get("rating")
                            for item in parsed_history
                            if isinstance(item.get("rating"), int)
                        ),
                        default=profile.rating,
                    )
                    if parsed_history:
                        profile.recent_delta = parsed_history[0].get("delta")
            except AccountFetchError:
                pass
        if detail and include_analysis:
            try:
                async with self._analysis_semaphore:
                    analysis = await self._fetch_nowcoder_analysis(uid)
            except AccountFetchError as exc:
                logger.warning(
                    "读取牛客用户 %s 题目分析失败：%s",
                    uid,
                    exc,
                )
                analysis = {
                    "source": "牛客公开练习页 + 牛客题库列表",
                    "coverage": "题目分析暂时无法读取，保留官方资料与 Rating 历史",
                    "analysis_status": "unavailable",
                }
            self._apply_analysis(profile, analysis)
        return profile

    @staticmethod
    def _apply_analysis(
        profile: AccountProfile,
        analysis: Optional[Dict[str, Any]],
    ) -> None:
        if not isinstance(analysis, dict):
            return
        profile.analysis = dict(analysis)
        difficulty = analysis.get("difficulty_distribution")
        if isinstance(difficulty, list):
            profile.difficulty_distribution = list(difficulty)
        solved_count = analysis.get("solved_count")
        if solved_count is not None:
            profile.solved_count = _parse_int(solved_count)
        rating_history = analysis.get("rating_history")
        if isinstance(rating_history, list):
            profile.rating_history = list(rating_history)
            profile.recent_contests = list(rating_history[:5])
            if rating_history:
                latest = rating_history[0]
                if isinstance(latest, dict):
                    profile.recent_delta = _parse_int(
                        latest.get("delta")
                    )
        max_rating = analysis.get("max_rating")
        if max_rating is not None:
            profile.max_rating = _parse_int(max_rating)
        contest_count = analysis.get("contest_count")
        if profile.contest_count is None and contest_count is not None:
            profile.contest_count = _parse_int(contest_count)
        profile.extra["analysis_source"] = str(
            analysis.get("source") or ""
        )
        profile.extra["analysis_coverage"] = str(
            analysis.get("coverage") or ""
        )

    async def _fetch_resource_json(
        self,
        key: str,
        url: str,
    ) -> object:
        now = time.time()
        cached = self._resource_cache.get(key)
        if cached and now - cached[0] < RESOURCE_CACHE_TTL:
            return cached[1]
        lock = self._resource_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._resource_cache.get(key)
            if cached and time.time() - cached[0] < RESOURCE_CACHE_TTL:
                return cached[1]
            value = await self._fetch_json(url, timeout=30.0)
            self._resource_cache[key] = (time.time(), value)
            return value

    @staticmethod
    def _parse_nowcoder_practice_page(
        text: str,
    ) -> Tuple[Dict[str, int], List[Dict[str, Any]], int]:
        stats: Dict[str, int] = {}
        for value_text, label_text in _NOWCODER_STATE_RE.findall(text):
            value = _parse_int(_clean_text(value_text))
            label = _clean_text(label_text)
            if value is None:
                continue
            if "题已挑战" in label:
                stats["challenged_count"] = value
            elif "题已通过" in label:
                stats["solved_count"] = value
            elif "次提交" in label:
                stats["submission_count"] = value

        page_match = _NOWCODER_PAGE_TOTAL_RE.search(text)
        page_total = _parse_int(page_match.group(1)) if page_match else 1
        rows: List[Dict[str, Any]] = []
        for row_html in _NOWCODER_PRACTICE_ROW_RE.findall(text):
            cells = re.findall(
                r"<td\b[^>]*>(.*?)</td>",
                row_html,
                re.I | re.S,
            )
            if len(cells) < 9:
                continue
            links = re.findall(
                r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
                row_html,
                re.I | re.S,
            )
            submission_id = ""
            problem_id = ""
            problem_name = ""
            for href, label in links:
                if not submission_id:
                    submission_match = re.search(
                        r"submissionId=(\d+)",
                        href,
                        re.I,
                    )
                    if submission_match:
                        submission_id = submission_match.group(1)
                problem_match = re.search(
                    r"/acm/problem/(\d+)",
                    href,
                    re.I,
                )
                if problem_match:
                    problem_id = problem_match.group(1)
                    if not problem_name:
                        problem_name = _clean_text(label)
            if not problem_id:
                continue
            result = _clean_text(cells[2])
            score_text = _clean_text(cells[3])
            score = None
            try:
                score = float(score_text)
            except (TypeError, ValueError):
                pass
            language = _clean_text(cells[7])
            submitted_at = _parse_timestamp(_clean_text(cells[8]))
            accepted = (
                result.casefold() in {"ac", "accepted", "答案正确", "通过"}
                or "答案正确" in result
            )
            rows.append(
                {
                    "submission_id": submission_id,
                    "problem_id": problem_id,
                    "problem_name": problem_name,
                    "result": result,
                    "score": score,
                    "language": language,
                    "timestamp": submitted_at,
                    "accepted": accepted,
                }
            )
        return stats, rows, max(1, page_total or 1)

    @staticmethod
    def _parse_nowcoder_problem_metadata(
        text: str,
        expected_problem_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        match = re.search(
            r'<tr\b[^>]*\bdata-problemid=["\'](\d+)["\'][^>]*>'
            r"(.*?)</tr>",
            text,
            re.I | re.S,
        )
        if not match:
            return None
        problem_id = match.group(1)
        if expected_problem_id and problem_id != str(expected_problem_id):
            return None
        row_html = match.group(2)
        cells = re.findall(
            r"<td\b[^>]*>(.*?)</td>",
            row_html,
            re.I | re.S,
        )
        if len(cells) < 4:
            return None
        title_match = re.search(
            r'<a\b[^>]*class=["\'][^"\']*\btitle\b[^"\']*["\'][^>]*>'
            r"(.*?)</a>",
            cells[1],
            re.I | re.S,
        )
        title = _clean_text(title_match.group(1)) if title_match else ""
        tags = [
            _clean_text(value)
            for value in re.findall(
                r'<a\b[^>]*class=["\'][^"\']*\btag-label\b[^"\']*["\'][^>]*>'
                r"(.*?)</a>",
                cells[1],
                re.I | re.S,
            )
        ]
        return {
            "problem_id": problem_id,
            "title": title,
            "difficulty": _parse_int(_clean_text(cells[2])),
            "tags": [tag for tag in tags if tag],
        }

    async def _fetch_nowcoder_problem_metadata(
        self,
        problem_ids: List[str],
    ) -> Dict[str, Dict[str, Any]]:
        unique_ids = list(dict.fromkeys(str(item) for item in problem_ids))
        unique_ids = unique_ids[:NOWCODER_PROBLEM_META_LIMIT]
        now = time.time()
        result: Dict[str, Dict[str, Any]] = {}
        missing = []
        for problem_id in unique_ids:
            cached = self._nowcoder_problem_cache.get(problem_id)
            if cached and now - cached[0] < RESOURCE_CACHE_TTL:
                result[problem_id] = cached[1]
            else:
                missing.append(problem_id)
        semaphore = asyncio.Semaphore(NOWCODER_PROBLEM_META_CONCURRENCY)

        async def fetch_one(problem_id: str):
            params = urlencode(
                {
                    "keyword": problem_id,
                    "pageSize": "1",
                    "page": "1",
                }
            )
            url = f"{NOWCODER_PROBLEM_LIST_URL}?{params}"
            async with semaphore:
                try:
                    text = await self._fetch_text(url)
                    metadata = self._parse_nowcoder_problem_metadata(
                        text,
                        problem_id,
                    )
                except AccountFetchError:
                    metadata = None
                return problem_id, metadata

        if missing:
            fetched = await asyncio.gather(
                *(fetch_one(problem_id) for problem_id in missing)
            )
            for problem_id, metadata in fetched:
                if not isinstance(metadata, dict):
                    continue
                self._nowcoder_problem_cache[problem_id] = (
                    time.time(),
                    metadata,
                )
                result[problem_id] = metadata
        return result

    async def _fetch_nowcoder_analysis(
        self,
        uid: str,
    ) -> Dict[str, Any]:
        base_params = {
            "pageSize": str(NOWCODER_ANALYSIS_PAGE_SIZE),
            "search": "",
            "statusTypeFilter": "-1",
            "languageCategoryFilter": "-1",
            "orderType": "DESC",
        }

        async def fetch_page(page: int) -> str:
            params = dict(base_params)
            params["page"] = str(page)
            return await self._fetch_text(
                f"{NOWCODER_PRACTICE_URL.format(uid=uid)}?"
                f"{urlencode(params)}",
                headers={"Accept-Language": "zh-CN,zh;q=0.9"},
            )

        first_text = await fetch_page(1)
        stats, first_rows, page_total = (
            self._parse_nowcoder_practice_page(first_text)
        )
        pages_to_fetch = min(
            max(1, page_total),
            NOWCODER_ANALYSIS_MAX_PAGES,
        )
        rows = list(first_rows)
        pages_missing = False
        if pages_to_fetch > 1:
            page_semaphore = asyncio.Semaphore(6)

            async def fetch_rest_page(page: int):
                async with page_semaphore:
                    try:
                        return await fetch_page(page)
                    except AccountFetchError:
                        return None

            rest = await asyncio.gather(
                *(
                    fetch_rest_page(page)
                    for page in range(2, pages_to_fetch + 1)
                )
            )
            for text in rest:
                if not text:
                    pages_missing = True
                    continue
                _, page_rows, _ = self._parse_nowcoder_practice_page(text)
                rows.extend(page_rows)

        submission_count = stats.get("submission_count", len(rows))
        solved_count = stats.get("solved_count")
        challenged_count = stats.get("challenged_count")
        accepted_rows = [
            row for row in rows if bool(row.get("accepted"))
        ]
        unique_solved = {
            str(row.get("problem_id"))
            for row in accepted_rows
            if row.get("problem_id")
        }
        language_counts: Dict[str, int] = {}
        for row in rows:
            language = str(row.get("language") or "").strip() or "未标注"
            language_counts[language] = language_counts.get(language, 0) + 1

        now = time.time()
        active_days_30 = set()
        active_days_90 = set()
        submissions_30 = 0
        submissions_90 = 0
        for row in rows:
            timestamp = row.get("timestamp")
            try:
                timestamp = float(timestamp)
            except (TypeError, ValueError):
                continue
            age = now - timestamp
            if 0 <= age <= 30 * 86400:
                submissions_30 += 1
                active_days_30.add(datetime.fromtimestamp(
                    timestamp, tz=CN_TZ
                ).date())
            if 0 <= age <= 90 * 86400:
                submissions_90 += 1
                active_days_90.add(datetime.fromtimestamp(
                    timestamp, tz=CN_TZ
                ).date())

        metadata = await self._fetch_nowcoder_problem_metadata(
            list(unique_solved)
        )
        difficulty_counts: Dict[str, int] = {}
        topic_counts: Dict[str, int] = {}
        for problem_id in unique_solved:
            item = metadata.get(problem_id)
            if not item:
                difficulty_label = "未标难度"
            else:
                difficulty = _parse_int(item.get("difficulty"))
                difficulty_label = "未标难度"
                for label, minimum, maximum in NOWCODER_DIFFICULTY_BUCKETS:
                    if (
                        difficulty is not None
                        and (minimum is None or difficulty >= minimum)
                        and (maximum is None or difficulty <= maximum)
                    ):
                        difficulty_label = label
                        break
                for tag in item.get("tags") or []:
                    topic_counts[str(tag)] = (
                        topic_counts.get(str(tag), 0) + 1
                    )
            difficulty_counts[difficulty_label] = (
                difficulty_counts.get(difficulty_label, 0) + 1
            )

        complete = (
            page_total <= NOWCODER_ANALYSIS_MAX_PAGES
            and not pages_missing
        )
        acceptance_rate = (
            round(len(accepted_rows) / max(1, len(rows)) * 100, 1)
            if complete and rows
            else None
        )
        problem_acceptance_rate = (
            round(len(unique_solved) / challenged_count * 100, 1)
            if challenged_count
            and challenged_count > 0
            else None
        )
        coverage = (
            f"练习页读取 {len(rows)}/{submission_count} 条提交；"
            f"题目元数据 {len(metadata)}/{len(unique_solved)}"
        )
        if not complete:
            if page_total > NOWCODER_ANALYSIS_MAX_PAGES:
                coverage += (
                    f"（最多读取 {NOWCODER_ANALYSIS_MAX_PAGES} 页）"
                )
            elif pages_missing:
                coverage += "（部分分页读取失败）"
        return {
            "source": "牛客公开练习页 + 牛客题库列表",
            "coverage": coverage,
            "submission_count": submission_count,
            "accepted_submission_count": (
                len(accepted_rows) if complete else None
            ),
            "challenged_count": challenged_count,
            "solved_count": solved_count or len(unique_solved),
            "acceptance_rate": acceptance_rate,
            "problem_acceptance_rate": problem_acceptance_rate,
            "active_days_30": len(active_days_30),
            "active_days_90": len(active_days_90),
            "submissions_30": submissions_30,
            "submissions_90": submissions_90,
            "difficulty_title": "牛客题目难度分布",
            "difficulty_distribution": [
                {"label": label, "count": difficulty_counts[label]}
                for label, _, _ in NOWCODER_DIFFICULTY_BUCKETS
                if difficulty_counts.get(label, 0) > 0
            ]
            + (
                [{"label": "未标难度", "count": difficulty_counts["未标难度"]}]
                if difficulty_counts.get("未标难度", 0) > 0
                else []
            ),
            "category_title": "牛客通过题知识点",
            "category_distribution": [
                {"label": label, "count": count}
                for label, count in sorted(
                    topic_counts.items(),
                    key=lambda item: (-item[1], item[0]),
                )[:8]
            ],
            "language_distribution": [
                {"label": label, "count": count}
                for label, count in sorted(
                    language_counts.items(),
                    key=lambda item: (-item[1], item[0]),
                )[:8]
            ],
            "summary": [
                item
                for item in (
                    (
                        {"label": "挑战题", "value": challenged_count}
                        if challenged_count is not None
                        else None
                    ),
                    {"label": "提交", "value": submission_count},
                    (
                        {
                            "label": "AC率",
                            "value": f"{acceptance_rate:.1f}%",
                        }
                        if acceptance_rate is not None
                        else None
                    ),
                    (
                        {
                            "label": "题目通过率",
                            "value": f"{problem_acceptance_rate:.1f}%",
                        }
                        if problem_acceptance_rate is not None
                        else None
                    ),
                    {"label": "30日活跃", "value": f"{len(active_days_30)}天"},
                    {"label": "90日活跃", "value": f"{len(active_days_90)}天"},
                )
                if item is not None
            ],
        }

    @staticmethod
    def _build_luogu_analysis(payload: object) -> Dict[str, Any]:
        data = None
        if isinstance(payload, dict):
            data = payload.get("data")
        if not isinstance(data, dict):
            return {
                "source": "洛谷公开个人页 #lentille-context",
                "coverage": "仅取得账号公开摘要，题目级难度待读取公开练习页",
            }

        daily_counts = data.get("dailyCounts")
        activity_dates: List[date] = []
        activity_values: List[int] = []
        if isinstance(daily_counts, dict):
            for date_text, value in daily_counts.items():
                try:
                    activity_date = datetime.strptime(
                        str(date_text),
                        "%Y-%m-%d",
                    ).date()
                except ValueError:
                    continue
                activity_dates.append(activity_date)
                if isinstance(value, (list, tuple)) and value:
                    count = _parse_int(value[0])
                elif isinstance(value, dict):
                    count = _parse_int(
                        value.get("count")
                        or value.get("submissionCount")
                        or value.get("value")
                    )
                else:
                    count = _parse_int(value)
                if count is not None:
                    activity_values.append(count)

        today = datetime.now(CN_TZ).date()
        active_days_30 = sum(
            1 for item in activity_dates
            if 0 <= (today - item).days <= 30
        )
        active_days_90 = sum(
            1 for item in activity_dates
            if 0 <= (today - item).days <= 90
        )
        month_counts: Dict[str, int] = {}
        for activity_date in activity_dates:
            age_days = (today - activity_date).days
            if 0 <= age_days <= 180:
                label = activity_date.strftime("%m月")
                month_counts[label] = month_counts.get(label, 0) + 1
        month_order = {
            label: index
            for index, label in enumerate(
                (
                    (today - timedelta(days=30 * offset)).strftime("%m月")
                    for offset in range(5, -1, -1)
                )
            )
        }

        scores = {}
        gu = data.get("gu")
        if isinstance(gu, dict) and isinstance(gu.get("scores"), dict):
            scores = gu["scores"]
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        solved_count = _parse_int(
            user.get("passedProblemCount")
            or user.get("passed")
            or user.get("solved")
            or user.get("ac")
        )
        submitted_problem_count = _parse_int(
            user.get("submittedProblemCount")
            or user.get("submitted")
            or user.get("attempted")
        )
        problem_acceptance_rate = (
            round(solved_count / submitted_problem_count * 100, 1)
            if solved_count is not None
            and submitted_problem_count
            and submitted_problem_count > 0
            else None
        )
        score_labels = {
            "basic": "基础信用",
            "practice": "练习情况",
            "social": "社区贡献",
            "contest": "比赛情况",
            "prize": "获得成就",
            "rating": "综合评分",
        }
        score_distribution = {}
        for key, label in score_labels.items():
            value = _parse_int(scores.get(key))
            if value is not None and value > 0:
                score_distribution[label] = value

        rating_history: List[Dict[str, Any]] = []
        elo = data.get("elo")
        if isinstance(elo, list):
            for row in elo[:RATING_HISTORY_LIMIT]:
                if not isinstance(row, dict):
                    continue
                contest = row.get("contest")
                contest_name = (
                    contest.get("name")
                    if isinstance(contest, dict)
                    else ""
                )
                rating = _parse_int(row.get("rating"))
                if rating is None:
                    continue
                previous = row.get("previous")
                old_rating = (
                    _parse_int(previous.get("rating"))
                    if isinstance(previous, dict)
                    else None
                )
                delta = _parse_int(row.get("prevDiff"))
                if delta is None and old_rating is not None:
                    delta = rating - old_rating
                rating_history.append(
                    {
                        "name": str(contest_name or "洛谷比赛"),
                        "rank": None,
                        "delta": delta,
                        "old_rating": old_rating,
                        "rating": rating,
                        "timestamp": _parse_timestamp(row.get("time")),
                        "url": (
                            f"https://www.luogu.com.cn/contest/"
                            f"{contest.get('id')}"
                            if isinstance(contest, dict)
                            and contest.get("id")
                            else ""
                        ),
                    }
                )

        return {
            "source": "洛谷公开个人页 #lentille-context",
            "coverage": (
                "账号摘要、公开活动日历和 Elo 历史；"
                "题目级通过记录待读取公开练习页"
            ),
            "active_days_total": len(set(activity_dates)),
            "active_days_30": active_days_30,
            "active_days_90": active_days_90,
            "activity_peak": max(activity_values, default=None),
            "solved_count": solved_count,
            "submitted_problem_count": submitted_problem_count,
            "problem_acceptance_rate": problem_acceptance_rate,
            "activity_title": "洛谷近半年活跃天数",
            "activity_distribution": [
                {"label": label, "count": count}
                for label, count in sorted(
                    month_counts.items(),
                    key=lambda item: (month_order.get(item[0], 99), item[0]),
                )
            ],
            "summary": [
                item
                for item in (
                    (
                        {
                            "label": "练习评分",
                            "value": scores.get("practice"),
                        }
                        if scores.get("practice") is not None
                        else None
                    ),
                    (
                        {
                            "label": "通过题",
                            "value": solved_count,
                        }
                        if solved_count is not None
                        else None
                    ),
                    (
                        {
                            "label": "提交题",
                            "value": submitted_problem_count,
                        }
                        if submitted_problem_count is not None
                        else None
                    ),
                    (
                        {
                            "label": "活跃天数",
                            "value": len(set(activity_dates)),
                        }
                        if activity_dates
                        else None
                    ),
                    {"label": "30日活跃", "value": f"{active_days_30}天"},
                    {"label": "90日活跃", "value": f"{active_days_90}天"},
                )
                if item is not None
            ],
            "score_title": "洛谷资料分项",
            "score_distribution": [
                {"label": label, "count": count}
                for label, count in score_distribution.items()
            ],
            "category_title": "洛谷资料分项",
            "category_distribution": [
                {"label": label, "count": count}
                for label, count in score_distribution.items()
            ],
            "rating_history": rating_history,
            "current_rating": (
                rating_history[0].get("rating")
                if rating_history
                else None
            ),
            "contest_count": len(rating_history) or None,
            "max_rating": max(
                (
                    item.get("rating")
                    for item in rating_history
                    if isinstance(item.get("rating"), int)
                ),
                default=None,
            ),
        }

    @staticmethod
    def _decode_luogu_payload(raw: str) -> Optional[object]:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            match = LENTILLE_RE.search(raw)
            if not match:
                return None
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                return None

    @staticmethod
    def _parse_luogu_practice_analysis(
        payload: object,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        if data.get("privacy") is True:
            return {
                "analysis_status": "unavailable",
                "practice_source": "洛谷公开练习页 #lentille-context",
                "practice_coverage": "练习记录受账号隐私保护",
            }
        passed = data.get("passed")
        if not isinstance(passed, list):
            return None

        unique_passed: Dict[str, Dict[str, Any]] = {}
        for item in passed:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("pid") or item.get("id") or "").strip()
            if pid:
                unique_passed[pid] = item
        if not unique_passed and passed:
            return None

        difficulty_counts: Dict[int, int] = {}
        type_counts: Dict[str, int] = {}
        for item in unique_passed.values():
            difficulty = _parse_int(item.get("difficulty"))
            if difficulty not in LUOGU_DIFFICULTY_LABELS:
                difficulty = 0
            difficulty_counts[difficulty] = (
                difficulty_counts.get(difficulty, 0) + 1
            )
            problem_type = str(item.get("type") or "").strip().upper()
            type_label = {
                "P": "普及/提高题库",
                "B": "入门题库",
                "T": "团队题库",
                "U": "用户题库",
            }.get(problem_type, "其他题库")
            type_counts[type_label] = type_counts.get(type_label, 0) + 1

        difficulty_distribution = [
            {
                "label": LUOGU_DIFFICULTY_LABELS[level],
                "count": difficulty_counts[level],
            }
            for level in sorted(difficulty_counts)
            if difficulty_counts.get(level, 0) > 0
        ]
        return {
            "solved_count": len(unique_passed),
            "difficulty_title": "洛谷通过题难度分组",
            "difficulty_distribution": difficulty_distribution,
            "category_title": "洛谷题库类型",
            "category_distribution": _distribution_rows(type_counts),
            "practice_source": "洛谷公开练习页 #lentille-context",
            "practice_coverage": f"已读取 {len(unique_passed)} 道通过题",
        }

    async def _fetch_luogu_practice_analysis(
        self,
        uid: str,
        *,
        preferred_source_url: str = "",
    ) -> Optional[Dict[str, Any]]:
        candidates = [
            LUOGU_PRACTICE_URL.format(uid=uid),
            LUOGU_LEGACY_PRACTICE_URL.format(uid=uid),
        ]
        if ".com.cn/" in preferred_source_url:
            candidates.reverse()
        for url in candidates:
            try:
                raw = await self._fetch_text(
                    url,
                    headers={
                        "Accept": "application/json,text/plain,text/html;q=0.9",
                        "X-Luogu-Type": "xhr",
                    },
                    timeout=20.0,
                )
                payload = self._decode_luogu_payload(raw)
                analysis = self._parse_luogu_practice_analysis(payload)
                if analysis is not None:
                    analysis["practice_url"] = url
                    return analysis
            except AccountFetchError:
                continue
        return None

    @staticmethod
    def _nowcoder_color(rating: Optional[int]) -> str:
        if rating is None:
            return "gray"
        if rating >= 2800:
            return "red"
        if rating >= 2400:
            return "orange"
        if rating >= 2000:
            return "yellow"
        if rating >= 1500:
            return "green"
        if rating >= 1100:
            return "blue"
        return "gray"

    @staticmethod
    def _parse_nowcoder_history(rows: list) -> List[Dict[str, Any]]:
        out = []
        for row in reversed(rows[-RATING_HISTORY_LIMIT:]):
            out.append(
                {
                    "name": str(row.get("contestName") or ""),
                    "rank": _parse_int(row.get("rank")),
                    "delta": _parse_int(row.get("changeValue")),
                    "old_rating": (
                        _parse_int(row.get("rating"))
                        - _parse_int(row.get("changeValue"))
                        if _parse_int(row.get("rating")) is not None
                        and _parse_int(row.get("changeValue")) is not None
                        else None
                    ),
                    "rating": _parse_int(row.get("rating")),
                    "timestamp": _parse_timestamp(row.get("time")),
                    "url": (
                        f"https://ac.nowcoder.com/acm/contest/{row.get('contestId')}"
                        if row.get("contestId")
                        else ""
                    ),
                }
            )
        return out

    async def _fetch_luogu(
        self,
        uid: str,
        detail: bool,
        *,
        include_analysis: bool = False,
    ) -> AccountProfile:
        candidates = [
            LUOGU_PROFILE_URL.format(uid=uid),
            LUOGU_LEGACY_PROFILE_URL.format(uid=uid),
            LUOGU_API_URL.format(uid=uid),
        ]
        last_error: Optional[Exception] = None
        payload: object = None
        source_url = LUOGU_PROFILE_URL.format(uid=uid)
        for url in candidates:
            try:
                raw = await self._fetch_text(
                    url,
                    headers={
                        "Accept": "application/json,text/plain,text/html;q=0.9",
                        "X-Luogu-Type": "xhr",
                    },
                )
                try:
                    candidate_payload = json.loads(raw)
                except json.JSONDecodeError:
                    match = LENTILLE_RE.search(raw)
                    if not match:
                        continue
                    try:
                        candidate_payload = json.loads(match.group(1))
                    except json.JSONDecodeError:
                        continue
                # 页面可能返回错误页/空壳 JSON；只有确认其中包含
                # 目标 UID 后才停止尝试，确保旧域名兜底真正生效。
                if self._find_luogu_user(candidate_payload, uid) is None:
                    continue
                payload = candidate_payload
                source_url = url
                if payload is not None:
                    break
            except AccountFetchError as exc:
                last_error = exc
        if payload is None:
            raise AccountFetchError(
                "洛谷个人资料暂时无法读取，暂时无法绑定"
            ) from last_error

        user = self._find_luogu_user(payload, uid)
        if user is None:
            raise AccountFetchError("未找到该洛谷用户", temporary=False)
        handle = str(
            user.get("name")
            or user.get("username")
            or user.get("handle")
            or uid
        )
        verification_fields = (
            "introduction",
            "motto",
            "bio",
            "description",
        )
        verification_field_present = any(
            field in user for field in verification_fields
        )
        intro = ""
        verification_field_source = ""
        for field in verification_fields:
            if field not in user:
                continue
            value = html.unescape(str(user.get(field) or ""))
            if value.strip():
                intro = value
                verification_field_source = field
                break
        verification_field_state = (
            "available"
            if intro.strip()
            else "empty"
            if verification_field_present
            else "missing"
        )
        avatar_value = (
            user.get("avatar")
            or user.get("avatarUrl")
            or f"https://cdn.luogu.com.cn/upload/usericon/{uid}.png"
        )
        profile = AccountProfile(
            platform="luogu",
            handle=handle,
            platform_user_id=str(user.get("uid") or user.get("id") or uid),
            display_name=handle,
            profile_url=LUOGU_PROFILE_URL.format(uid=uid),
            verification_value=intro,
            rating=_parse_int(
                user.get("eloValue")
                or user.get("elo")
                or user.get("rating")
            ),
            rating_rank=_parse_int(
                user.get("ranking") or user.get("rank")
            ),
            solved_count=_parse_int(
                user.get("passedProblemCount")
                or user.get("passed")
                or user.get("solved")
                or user.get("ac")
            ),
            school=str(user.get("school") or ""),
            color=str(user.get("color") or ""),
            avatar_url=str(avatar_value),
            source_url=source_url,
            extra={
                "ccf_level": str(user.get("ccfLevel") or ""),
                "xcpc_level": str(user.get("xcpcLevel") or ""),
                "slogan": str(user.get("slogan") or ""),
                "submitted_problem_count": _parse_int(
                    user.get("submittedProblemCount")
                ),
                "gist": _parse_int(user.get("gist")),
                "verification_field_present": verification_field_present,
                "verification_field_state": verification_field_state,
                "verification_field_source": verification_field_source,
            },
        )
        if detail and include_analysis:
            analysis = self._build_luogu_analysis(payload)
            practice_analysis = await self._fetch_luogu_practice_analysis(
                uid,
                preferred_source_url=source_url,
            )
            if (
                practice_analysis
                and practice_analysis.get("analysis_status")
                != "unavailable"
            ):
                analysis.update(practice_analysis)
                analysis["source"] = (
                    "洛谷公开个人页 + 洛谷公开练习页 #lentille-context"
                )
                analysis["coverage"] = (
                    "账号摘要、公开活动日历、Elo 历史；"
                    f"{practice_analysis.get('practice_coverage')}"
                )
            elif practice_analysis:
                analysis["analysis_status"] = "partial"
                analysis["coverage"] = (
                    "账号摘要、公开活动日历、Elo 历史；"
                    f"{practice_analysis.get('practice_coverage')}"
                )
            else:
                analysis["analysis_status"] = "partial"
            self._apply_analysis(profile, analysis)
        return profile

    @staticmethod
    def _find_luogu_user(payload: object, uid: str) -> Optional[Dict[str, Any]]:
        best = None
        best_score = -1
        for item in _nested_dicts(payload):
            score = 0
            if str(item.get("uid") or item.get("id") or "") == uid:
                score += 5
            if any(key in item for key in ("name", "username", "handle")):
                score += 2
            if any(
                key in item
                for key in ("introduction", "motto", "bio", "rating", "passed")
            ):
                score += 1
            if score > best_score:
                best = item
                best_score = score
        return best if best_score >= 2 else None

    async def _fetch_atcoder(
        self,
        handle: str,
        detail: bool,
        *,
        include_analysis: bool = False,
    ) -> AccountProfile:
        profile_url = ATCODER_PROFILE_URL.format(handle=quote(handle))
        text = await self._fetch_text(
            profile_url,
            headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        if "AtCoder" not in text or "/users/" not in text:
            raise AccountFetchError("未找到该 AtCoder 用户", temporary=False)
        canonical_match = re.search(
            r'<a[^>]+href="/users/([^"?]+)"[^>]*class="username"',
            text,
            re.I,
        )
        canonical = unquote(canonical_match.group(1)) if canonical_match else handle
        rating_cell = self._atcoder_table_value(text, "Rating")
        highest_cell = self._atcoder_table_value(text, "Highest Rating")
        rank_cell = self._atcoder_table_value(text, "Rank")
        rated_cell = self._atcoder_table_value(text, "Rated Matches")
        affiliation = self._atcoder_table_value(text, "Affiliation")
        avatar = self._extract_atcoder_avatar(text)
        rating_text = _clean_text(rating_cell)
        highest_text = _clean_text(highest_cell)
        rating = _parse_int(rating_text)
        max_rating = _parse_int(highest_text)
        color_match = re.search(
            r'<span[^>]+class=["\'][^"\']*user-([a-z-]+)',
            rating_cell,
            re.I,
        )
        color = color_match.group(1) if color_match else ""
        history = self._parse_atcoder_history_html(text)
        if detail and not history:
            try:
                data = await self._fetch_json(
                    ATCODER_HISTORY_JSON_URL.format(handle=quote(canonical)),
                    headers={"Accept-Language": "en-US,en;q=0.9"},
                )
                if isinstance(data, list):
                    history = self._parse_atcoder_history(data)
            except AccountFetchError:
                pass
        profile = AccountProfile(
            platform="atcoder",
            handle=canonical,
            platform_user_id=canonical,
            display_name=canonical,
            profile_url=f"https://atcoder.jp/users/{quote(canonical)}",
            verification_value=_clean_text(affiliation),
            rating=rating,
            rank_text=_clean_text(rank_cell),
            max_rating=max_rating,
            contest_count=_parse_int(rated_cell),
            color=color,
            recent_contests=history[:5],
            rating_history=history,
            recent_delta=(
                history[0].get("delta")
                if history and isinstance(history[0].get("delta"), int)
                else None
            ),
            avatar_url=avatar,
            source_url=profile_url,
        )
        if detail and include_analysis:
            try:
                async with self._analysis_semaphore:
                    analysis = await self._fetch_atcoder_analysis(canonical)
            except AccountFetchError as exc:
                logger.warning(
                    "读取 AtCoder 用户 %s 题目分析失败：%s",
                    canonical,
                    exc,
                )
                analysis = {
                    "source": "AtCoder Problems（估计难度）",
                    "coverage": "题目分析暂时无法读取，保留官方资料与 Rating 历史",
                    "analysis_status": "unavailable",
                }
            self._apply_analysis(profile, analysis)
        return profile

    async def _fetch_atcoder_analysis(
        self,
        handle: str,
    ) -> Dict[str, Any]:
        submissions: List[Dict[str, Any]] = []
        from_second = 0
        truncated = False
        while len(submissions) < ATCODER_SUBMISSION_SCAN_LIMIT:
            params = urlencode(
                {
                    "user": handle,
                    "from_second": str(from_second),
                }
            )
            data = await self._fetch_json(
                f"{ATCODER_SUBMISSIONS_URL}?{params}"
            )
            if not isinstance(data, list):
                raise AccountFetchError(
                    "AtCoder Problems 返回的数据格式异常"
                )
            if not data:
                break
            remaining = ATCODER_SUBMISSION_SCAN_LIMIT - len(submissions)
            submissions.extend(
                item for item in data[:remaining]
                if isinstance(item, dict)
            )
            if len(submissions) >= ATCODER_SUBMISSION_SCAN_LIMIT:
                truncated = True
                break
            timestamps = [
                _parse_int(item.get("epoch_second"))
                for item in data
                if isinstance(item, dict)
            ]
            timestamps = [item for item in timestamps if item is not None]
            if not timestamps:
                break
            next_from = max(timestamps) + 1
            if next_from <= from_second:
                break
            from_second = next_from
            if len(data) < ATCODER_SUBMISSION_PAGE_SIZE:
                break

        if not submissions:
            return {
                "source": "AtCoder Problems（提交记录 + 估计难度）",
                "coverage": "未取得公开提交记录",
                "submission_count": 0,
                "accepted_submission_count": 0,
                "solved_count": 0,
                "acceptance_rate": None,
                "difficulty_title": "AtCoder 估计难度分布",
                "difficulty_distribution": [],
                "category_title": "AtCoder 题目系列",
                "category_distribution": [],
                "language_distribution": [],
                "summary": [],
            }

        try:
            models_data = await self._fetch_resource_json(
                "atcoder-problem-models",
                ATCODER_PROBLEM_MODELS_URL,
            )
        except AccountFetchError:
            models_data = {}
        model_map = (
            {
                str(key): value
                for key, value in models_data.items()
                if isinstance(value, dict)
            }
            if isinstance(models_data, dict)
            else {}
        )

        accepted = [
            item
            for item in submissions
            if str(item.get("result") or "").upper() == "AC"
        ]
        unique_accepted: Dict[str, Dict[str, Any]] = {}
        for item in accepted:
            problem_id = str(item.get("problem_id") or "").strip()
            if problem_id:
                unique_accepted[problem_id] = item

        difficulty_counts: Dict[str, int] = {}
        series_counts: Dict[str, int] = {}
        language_counts: Dict[str, int] = {}
        modeled_count = 0
        for problem_id in unique_accepted:
            submission = unique_accepted[problem_id]
            model = model_map.get(problem_id) or {}
            difficulty = _parse_int(model.get("difficulty"))
            if difficulty is None:
                difficulty_label = "未建模"
            else:
                modeled_count += 1
                difficulty_label = "未建模"
                for label, minimum, maximum in ATCODER_DIFFICULTY_BUCKETS:
                    if (
                        (minimum is None or difficulty >= minimum)
                        and (maximum is None or difficulty <= maximum)
                    ):
                        difficulty_label = label
                        break
            difficulty_counts[difficulty_label] = (
                difficulty_counts.get(difficulty_label, 0) + 1
            )
            contest_id = str(
                submission.get("contest_id")
                or ""
            ).casefold()
            if contest_id.startswith("abc"):
                series = "ABC"
            elif contest_id.startswith("arc"):
                series = "ARC"
            elif contest_id.startswith("agc"):
                series = "AGC"
            elif contest_id.startswith("ahc"):
                series = "AHC"
            elif contest_id.startswith("joi"):
                series = "JOI"
            elif contest_id:
                series = "其他赛制"
            else:
                series = "未标注"
            series_counts[series] = series_counts.get(series, 0) + 1

        for item in submissions:
            language = str(item.get("language") or "").strip() or "未标注"
            language_counts[language] = language_counts.get(language, 0) + 1

        now = time.time()
        active_days_30 = set()
        active_days_90 = set()
        submissions_30 = 0
        submissions_90 = 0
        for item in submissions:
            timestamp = _parse_timestamp(item.get("epoch_second"))
            if timestamp is None:
                continue
            age = now - timestamp
            if 0 <= age <= 30 * 86400:
                submissions_30 += 1
                active_days_30.add(datetime.fromtimestamp(
                    timestamp,
                    tz=CN_TZ,
                ).date())
            if 0 <= age <= 90 * 86400:
                submissions_90 += 1
                active_days_90.add(datetime.fromtimestamp(
                    timestamp,
                    tz=CN_TZ,
                ).date())

        acceptance_rate = (
            round(len(accepted) / len(submissions) * 100, 1)
            if submissions and not truncated
            else None
        )
        difficulty_distribution = [
            {"label": label, "count": difficulty_counts[label]}
            for label, _, _ in ATCODER_DIFFICULTY_BUCKETS
            if difficulty_counts.get(label, 0) > 0
        ]
        if difficulty_counts.get("未建模", 0) > 0:
            difficulty_distribution.append(
                {"label": "未建模", "count": difficulty_counts["未建模"]}
            )
        coverage = (
            f"AtCoder Problems 提交记录 {len(submissions)} 条；"
            f"题目模型 {modeled_count}/{len(unique_accepted)}"
        )
        if truncated:
            coverage += f"（最多统计 {ATCODER_SUBMISSION_SCAN_LIMIT} 条提交）"
        if not model_map:
            coverage += "；题目模型资源暂不可用"
        return {
            "source": "AtCoder Problems（提交记录 + 估计难度）",
            "coverage": coverage,
            "submission_count": len(submissions),
            "accepted_submission_count": len(accepted),
            "solved_count": len(unique_accepted),
            "acceptance_rate": acceptance_rate,
            "active_days_30": len(active_days_30),
            "active_days_90": len(active_days_90),
            "submissions_30": submissions_30,
            "submissions_90": submissions_90,
            "difficulty_title": "AtCoder 估计难度分布",
            "difficulty_distribution": difficulty_distribution,
            "category_title": "AtCoder 题目系列",
            "category_distribution": _distribution_rows(series_counts),
            "language_distribution": _distribution_rows(
                language_counts,
                limit=8,
            ),
            "summary": [
                item
                for item in (
                    {"label": "提交", "value": len(submissions)},
                    {"label": "通过题", "value": len(unique_accepted)},
                    (
                        {
                            "label": "提交通过率",
                            "value": f"{acceptance_rate:.1f}%",
                        }
                        if acceptance_rate is not None
                        else None
                    ),
                    {"label": "30日活跃", "value": f"{len(active_days_30)}天"},
                    {"label": "90日活跃", "value": f"{len(active_days_90)}天"},
                )
                if item is not None
            ],
        }

    @staticmethod
    def _extract_atcoder_avatar(text: str) -> str:
        """读取 AtCoder 用户页中 class=avatar 的公开头像。"""
        match = re.search(
            r"<img\b(?=[^>]*\bclass\s*=\s*['\"][^'\"]*\bavatar\b[^'\"]*['\"])"
            r"[^>]*\bsrc\s*=\s*['\"]([^'\"]+)['\"]",
            text,
            re.I | re.S,
        )
        return _clean_text(match.group(1)) if match else ""
    @staticmethod
    def _atcoder_table_value(text: str, label: str) -> str:
        for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", text, re.I | re.S):
            heading = re.search(r"<th\b[^>]*>(.*?)</th>", row, re.I | re.S)
            cell = re.search(r"<td\b[^>]*>(.*?)</td>", row, re.I | re.S)
            if not heading or not cell:
                continue
            heading_text = _clean_text(heading.group(1))
            if heading_text == label or heading_text.startswith(f"{label} "):
                return cell.group(1)
        return ""

    @classmethod
    def _parse_atcoder_history_html(cls, text: str) -> List[Dict[str, Any]]:
        match = _ATCODER_HISTORY_RE.search(text)
        if not match:
            return []
        try:
            rows = json.loads(match.group(1))
        except json.JSONDecodeError:
            return []
        return cls._parse_atcoder_history(rows)

    @staticmethod
    def _parse_atcoder_history(rows: list) -> List[Dict[str, Any]]:
        out = []
        for row in reversed(rows[-RATING_HISTORY_LIMIT:]):
            old = _parse_int(row.get("OldRating"))
            new = _parse_int(row.get("NewRating"))
            out.append(
                {
                    "name": str(row.get("ContestName") or ""),
                    "rank": _parse_int(row.get("Place")),
                    "delta": new - old if old is not None and new is not None else None,
                    "old_rating": old,
                    "rating": new,
                    "timestamp": _parse_timestamp(row.get("EndTime")),
                    "url": (
                        "https://atcoder.jp"
                        + str(row.get("StandingsUrl") or "").split("?")[0]
                        if row.get("StandingsUrl")
                        else ""
                    ),
                }
            )
        return out

    @staticmethod
    def rating_delta_for_period(
        profile: AccountProfile,
        *,
        days: int = 7,
        now: Optional[float] = None,
    ) -> Optional[int]:
        """根据平台 Rating 历史计算最近一段时间的变化。"""
        entries = [
            item
            for item in profile.rating_history
            if isinstance(item, dict)
            and item.get("timestamp") is not None
            and item.get("rating") is not None
        ]
        if not entries:
            return None
        current_time = time.time() if now is None else now
        valid_entries = []
        for item in entries:
            try:
                timestamp = float(item["timestamp"])
            except (TypeError, ValueError):
                continue
            if timestamp <= current_time:
                valid_entries.append(item)
        if not valid_entries:
            return None
        valid_entries.sort(key=lambda item: float(item["timestamp"]))
        cutoff = current_time - max(1, int(days)) * 86400
        current = valid_entries[-1]
        try:
            current_rating = int(current["rating"])
        except (TypeError, ValueError):
            return None

        baseline = None
        for item in valid_entries:
            try:
                timestamp = float(item["timestamp"])
            except (TypeError, ValueError):
                continue
            if timestamp <= cutoff:
                baseline = item
            else:
                break
        if baseline is not None:
            try:
                return current_rating - int(baseline["rating"])
            except (TypeError, ValueError):
                return None

        # 新账号没有完整的周期历史时，使用周期内第一场比赛的 OldRating。
        recent = []
        for item in valid_entries:
            try:
                if float(item["timestamp"]) >= cutoff:
                    recent.append(item)
            except (TypeError, ValueError):
                continue
        if not recent:
            return None
        old_rating = recent[0].get("old_rating")
        try:
            return current_rating - int(old_rating) if old_rating is not None else None
        except (TypeError, ValueError):
            return None
