"""四个竞赛平台的公开账号资料抓取、绑定校验与缓存。"""
from __future__ import annotations

import asyncio
import html
import json
import re
import time
from datetime import datetime, timezone
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
from .utils import LENTILLE_RE, USER_AGENT, fetch_text_with_retry

CF_API_URL = "https://codeforces.com/api"
NOWCODER_PROFILE_URL = "https://ac.nowcoder.com/acm/contest/profile/{uid}"
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
LUOGU_PROFILE_URL = "https://www.luogu.com.cn/user/{uid}"
LUOGU_API_URL = "https://www.luogu.com.cn/api/user/show?uid={uid}"
LUOGU_ME_API_URL = "https://api.luogu.me/user/query/{uid}"
ATCODER_PROFILE_URL = "https://atcoder.jp/users/{handle}?lang=en"
ATCODER_HISTORY_JSON_URL = "https://atcoder.jp/users/{handle}/history/json"

PROFILE_CACHE_TTL = 10 * 60
DETAIL_CACHE_TTL = 10 * 60
LUOGU_ME_INTRO_CACHE_TTL = 10 * 60
CF_MIN_REQUEST_INTERVAL = 2.1
RATING_HISTORY_LIMIT = 200

_CF_HANDLE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ATCODER_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_NOWCODER_UID_RE = re.compile(r"^\d{1,20}$")
_LUOGU_UID_RE = re.compile(r"^\d{1,20}$")
_HTML_TAG_RE = re.compile(r"<[^>]+>", re.S)
_ATCODER_HISTORY_RE = re.compile(
    r"var\s+rating_history\s*=\s*(\[.*?\])\s*;\s*</script>",
    re.S,
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
        return None


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
            Tuple[str, str, bool, bool],
            Tuple[float, AccountProfile],
        ] = {}
        self._locks: Dict[
            Tuple[str, str, bool, bool],
            asyncio.Lock,
        ] = {}
        self._luogu_me_intro_cache: Dict[str, Tuple[float, str]] = {}
        self._luogu_me_intro_locks: Dict[str, asyncio.Lock] = {}
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
    ) -> AccountProfile:
        if platform not in ACCOUNT_PLATFORMS:
            raise AccountFetchError("不支持的平台")
        normalized = normalize_account_identifier(platform, identifier)
        if not normalized:
            raise AccountFetchError(self.invalid_identifier_message(platform), temporary=False)
        key = (
            platform,
            normalized.casefold(),
            detail,
            bool(detail and include_submissions),
        )
        now = time.time()
        cached = self._cache.get(key)
        ttl = DETAIL_CACHE_TTL if detail else self.cache_ttl
        if not force and cached and now - cached[0] < ttl:
            return cached[1]

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._cache.get(key)
            if not force and cached and time.time() - cached[0] < ttl:
                return cached[1]
            profile = await self._fetch_profile(
                platform,
                normalized,
                detail,
                include_submissions=include_submissions,
            )
            profile.fetched_at = time.time()
            self._cache[key] = (profile.fetched_at, profile)
            # 详细资料可以复用为摘要资料，减少后续排行请求。
            if detail:
                self._cache[
                    (platform, normalized.casefold(), False, False)
                ] = (
                    profile.fetched_at,
                    profile,
                )
            return profile

    async def get_profiles(
        self,
        platform: str,
        identifiers: List[str],
        *,
        detail: bool = False,
        force: bool = False,
        include_submissions: bool = True,
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
            key = ("codeforces", identifier.casefold(), False, False)
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
                key = ("codeforces", profile.handle.casefold(), False, False)
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
        """读取绑定校验字段；洛谷个人介绍使用 luogu.me。"""
        if platform != "luogu":
            return str(
                getattr(profile, "verification_value", "") or ""
            )
        uid = normalize_account_identifier(platform, identifier)
        if not uid:
            raise AccountFetchError(
                self.invalid_identifier_message(platform),
                temporary=False,
            )
        return await self._fetch_luogu_me_introduction(uid, force=force)

    async def _fetch_luogu_me_introduction(
        self,
        uid: str,
        *,
        force: bool = False,
    ) -> str:
        """从洛谷保存站 API 读取稳定的个人介绍，用于绑定验证码。"""
        key = str(uid)
        now = time.time()
        cached = self._luogu_me_intro_cache.get(key)
        if (
            not force
            and cached
            and now - cached[0] < LUOGU_ME_INTRO_CACHE_TTL
        ):
            return cached[1]

        lock = self._luogu_me_intro_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._luogu_me_intro_cache.get(key)
            if (
                not force
                and cached
                and time.time() - cached[0] < LUOGU_ME_INTRO_CACHE_TTL
            ):
                return cached[1]
            payload = await self._fetch_json(
                LUOGU_ME_API_URL.format(uid=quote(key)),
                headers={
                    "Accept": "application/json",
                    "Origin": "https://www.luogu.me",
                    "Referer": f"https://www.luogu.me/user/{quote(key)}",
                },
            )
            if (
                not isinstance(payload, dict)
                or str(payload.get("code") or "") != "200"
            ):
                message = (
                    str(payload.get("message") or "")
                    if isinstance(payload, dict)
                    else ""
                )
                raise AccountFetchError(
                    "洛谷个人资料暂时无法读取"
                    + (f"：{message}" if message else "")
                )
            data = payload.get("data")
            if not isinstance(data, dict):
                raise AccountFetchError("洛谷个人资料暂时无法读取")
            returned_uid = str(data.get("id") or "").strip()
            if returned_uid and returned_uid != key:
                raise AccountFetchError("洛谷个人资料暂时无法读取")
            introduction = data.get("introduction")
            value = html.unescape(str(introduction or ""))
            self._luogu_me_intro_cache[key] = (time.time(), value)
            return value

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
    ) -> AccountProfile:
        if platform == "codeforces":
            return await self._fetch_codeforces(
                identifier,
                detail,
                include_submissions=include_submissions,
            )
        if platform == "nowcoder":
            return await self._fetch_nowcoder(identifier, detail)
        if platform == "luogu":
            return await self._fetch_luogu(identifier, detail)
        if platform == "atcoder":
            return await self._fetch_atcoder(identifier, detail)
        raise AccountFetchError("不支持的平台", temporary=False)

    async def _fetch_text(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        retries: int = 2,
    ) -> str:
        if self.session is None:
            raise AccountFetchError("账号抓取器尚未初始化")
        try:
            return await fetch_text_with_retry(
                self.session,
                url,
                retries=retries,
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
    ) -> object:
        text = await self._fetch_text(url, headers=headers, retries=retries)
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
            if include_submissions:
                status_data = await self._cf_json(
                    "user.status",
                    {"handle": canonical, "from": "1", "count": "5"},
                )
                if (
                    isinstance(status_data, dict)
                    and status_data.get("status") == "OK"
                ):
                    profile.recent_submissions = self._parse_cf_submissions(
                        status_data.get("result") or []
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

    async def _fetch_nowcoder(
        self, uid: str, detail: bool
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
        return profile

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
        self, uid: str, detail: bool
    ) -> AccountProfile:
        candidates = [
            LUOGU_PROFILE_URL.format(uid=uid),
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
                source_url = url
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    match = LENTILLE_RE.search(raw)
                    payload = json.loads(match.group(1)) if match else None
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
        intro = str(
            user.get("introduction")
            or user.get("motto")
            or user.get("bio")
            or user.get("description")
            or ""
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
            verification_value=html.unescape(intro),
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
            },
        )
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
        self, handle: str, detail: bool
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
        return profile

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
