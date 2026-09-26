"""四个竞赛平台的公开账号资料抓取、绑定校验与缓存。"""
from __future__ import annotations

import asyncio
import bisect
import html
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
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

# 资料/详细资料新鲜窗口：按评审结论放宽到 30 分钟（各 OJ Rating 一天约变化
# 1~2 次，无需高敏感度），force 路径仍然即时抓取。
PROFILE_CACHE_TTL = 30 * 60
DETAIL_CACHE_TTL = 30 * 60
ANALYSIS_CACHE_TTL = 12 * 60 * 60
ANALYSIS_FAILURE_CACHE_TTL = 5 * 60
RESOURCE_CACHE_TTL = 24 * 60 * 60
# 账号级负缓存：永久性失败（账号不存在/格式错）短缓存 5 分钟；
# 临时性网络失败只缓存 2 分钟，避免每次排行 TTL 过期后重复重抓坏账号。
FETCH_FAILURE_PERMANENT_TTL = 5 * 60
FETCH_FAILURE_TEMP_TTL = 2 * 60
CF_MIN_REQUEST_INTERVAL = 2.1
# 各平台 Rating 历史上限：历史本来就在同一次请求里返回（CF user.rating、
# AtCoder history/json、牛客 rating-history、洛谷 elo），截断只发生在解析阶段，
# 因此放宽窗口是零网络成本的。1000 场足以覆盖任何活跃账号的完整生涯。
RATING_HISTORY_LIMIT = 1000
# 提交扫描上限：LGM/重度选手的提交数可超过 2 万，1 万会造成通过题数/提交次数
# 明显低估（实测 maspy 共 21459 条）。CF 单次请求文档上限为 1 万条，
# 因此按 CF_SUBMISSION_PAGE_SIZE 翻页到该上限；只有超大账号才会翻多页。
CF_SUBMISSION_SCAN_LIMIT = 50000
CF_SUBMISSION_PAGE_SIZE = 10000
# 分析缓存版本：扫描上限/分析口径变化时 +1，旧的持久化分析缓存会被自动忽略，
# 无需等待 12 小时 TTL 或手动清库。
# （v2 = CF 5万/AtCoder 2万 上限口径；v3 = 热力图；v4 = 牛客 2 万条 + 题库索引）
ANALYSIS_CACHE_VERSION = 5
# 分析结果里保留的"已通过题"上限（供每日一题/推荐补题使用）：
# 2000 个 ID 约 20KB，随 12h 分析缓存写入 SQLite，体积可接受。
SOLVED_PROBLEM_IDS_LIMIT = 2000
# 打卡热力图窗口：近 12 个月。
ACTIVITY_HEATMAP_DAYS = 365
# 牛客练习页：pageSize 服务端上限为 200（201 起返回 “pageSize is too big”），
# 按 200 条/页翻页到 2 万条（与 AtCoder 的提交扫描上限对齐）。
NOWCODER_ANALYSIS_PAGE_SIZE = 200
NOWCODER_ANALYSIS_SCAN_LIMIT = 20000
NOWCODER_ANALYSIS_MAX_PAGES = NOWCODER_ANALYSIS_SCAN_LIMIT // NOWCODER_ANALYSIS_PAGE_SIZE
# 并发抓分页时每批之间的间隔（秒），避免把公共页面打得太急。
NOWCODER_ANALYSIS_PAGE_INTERVAL = 0.15
NOWCODER_ANALYSIS_CONCURRENCY = 6
# pageSize 被对端收紧时的降级值（首页 0 行且无状态数据时自动重试一次）。
NOWCODER_ANALYSIS_FALLBACK_PAGE_SIZE = 100
# 牛客题库索引：难度与知识点从题库列表 JSON 接口整库抓取一次，
# 之后按题目 ID 本地查表，从而覆盖全部通过题（原先是按需逐题抓，截断到 300 题）。
NOWCODER_PROBLEM_LIST_JSON_URL = "https://ac.nowcoder.com/acm/problem/list/json"
NOWCODER_PROBLEM_INDEX_PAGE_SIZE = 50
NOWCODER_PROBLEM_INDEX_CONCURRENCY = 6
NOWCODER_PROBLEM_INDEX_TTL = 7 * 24 * 3600
NOWCODER_PROBLEM_INDEX_VERSION = 2
#: CF 全站 rating 榜（user.ratedList）：实测 3.8 万人 / 13.5MB / 约 20 秒，
#: 只落盘排序后的 rating 数组（约 150KB），24 小时有效。
CF_RATED_LIST_TTL = 24 * 3600
#: 全量榜单实测 13.7 万人 / 约 115 秒；绑定人数会持续增长，且 CF 会限速，
#: 这里给足 10 分钟（它是启动后台预热 + 24 小时缓存，慢一点不影响用户查询）。
CF_RATED_LIST_TIMEOUT = 600.0
#: 榜单拉取失败后的重试次数（CF 偶发超时/502）
CF_RATED_LIST_ATTEMPTS = 3
#: 榜单来源标记：full = 不带 activeOnly 的全量榜单（含不活跃的顶尖选手）
CF_RATED_LIST_SOURCE = "full"
# 整库约 1.4 万题；条目数明显偏少说明抓取残缺，直接丢弃重建。
NOWCODER_PROBLEM_INDEX_MIN_ENTRIES = 5000
NOWCODER_PROBLEM_INDEX_FILENAME = "nowcoder_problem_index.json"
# 索引不可用（首次部署尚未构建/构建失败）时的降级上限：沿用旧行为逐题抓。
NOWCODER_PROBLEM_META_FALLBACK_LIMIT = 300
# 索引可用时，对"索引里没有的题目"最多单题补查多少次（新题为主；
# 不在题库列表里的比赛题/定制自测题牛客未公开难度，查也查不到）。
NOWCODER_PROBLEM_META_LOOKUP_LIMIT = 100
# 负缓存容量：记录"确认不在题库列表里"的题目，避免每次分析重复补查。
NOWCODER_PROBLEM_ABSENT_MAX = 20000
# 题库 JSON 的 difficulty 用 1~5 / -1 表示“未评定”（同一题在题库列表页里
# 难度单元格为空），真实难度从 400 起；低于该值一律按未标难度处理。
NOWCODER_DIFFICULTY_MIN_VALID = 400
NOWCODER_PROBLEM_META_LIMIT = NOWCODER_PROBLEM_META_FALLBACK_LIMIT
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
ATCODER_SUBMISSION_SCAN_LIMIT = 20000
# kenkoooo 分页接口每页 500 条，2 万条需要 40 次请求；页间留一点间隔，
# 避免把公共接口打得太急（结果有 12 小时缓存，不会频繁发生）。
ATCODER_SUBMISSION_PAGE_INTERVAL = 0.3
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
        problem_index_path: Optional[str | Path] = None,
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
        # 牛客题库索引：{problem_id: {"d": difficulty|None, "t": [知识点]}}。
        # 整库抓取一次（约 1.4 万题）后本地查表，覆盖全部通过题。
        self._nowcoder_problem_index: Dict[str, Dict[str, Any]] = {}
        self._cf_rated_ratings: Optional[Tuple[float, List[int]]] = None
        #: {handle: 榜单位次}，榜单本身即排名（位次 = 名次）
        self._cf_rank_positions: Dict[str, int] = {}
        self._nowcoder_problem_index_loaded_at = 0.0
        self._nowcoder_problem_index_dirty = False
        self._nowcoder_problem_index_lock = asyncio.Lock()
        # 确认不在题库列表里的题目（牛客未公开难度）：随索引一起落盘。
        self._nowcoder_problem_absent: set = set()
        # 构建失败退避：{下次允许构建时间: float}
        self._nowcoder_problem_index_backoff_until = 0.0
        self._nowcoder_problem_index_consecutive_failures = 0
        self._problem_index_path = (
            Path(problem_index_path) if problem_index_path else None
        )
        self._cf_lock = asyncio.Lock()
        self._cf_last_request = 0.0
        self._cf_bulk_lock = asyncio.Lock()
        # 账号级负缓存：{(platform, handle_norm): (expires_at, message, temporary)}
        self._failure_cache: Dict[Tuple[str, str], Tuple[float, str, bool]] = {}
        # 可选持久化（AccountStore）：写缓存只标脏，周期批量 flush，避免请求路径变慢。
        self.cache_store = None
        self._profile_cache_dirty: set = set()
        self._failure_cache_dirty: set = set()

    async def initialize(
        self,
        session: Optional[aiohttp.ClientSession] = None,
        cache_store=None,
    ) -> None:
        if session is not None:
            self.session = session
            self._owns_session = False
        elif self.session is None:
            self.session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT}
            )
            self._owns_session = True
        if cache_store is not None:
            self.cache_store = cache_store
            await self._load_persistent_caches()
        # 牛客题库索引：命中磁盘快照就不必重新整库抓取。
        self.load_nowcoder_problem_index()

    async def _load_persistent_caches(self) -> None:
        """启动时把 SQLite 中的资料/负缓存恢复到内存，避免重启后全部重爬。"""
        if self.cache_store is None:
            return
        try:
            rows = await self.cache_store.load_profile_cache()
            loaded = 0
            for row in rows:
                key = self._key_from_kind(
                    str(row["platform"]),
                    str(row["handle_norm"]),
                    str(row["kind"]),
                )
                if key is None:
                    continue
                try:
                    payload = json.loads(str(row["payload"]))
                    profile = AccountProfile(**payload)
                    profile.fetched_at = float(row["fetched_at"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if key not in self._cache:
                    self._cache[key] = (profile.fetched_at, profile)
                    loaded += 1
            failures = await self.cache_store.load_fetch_failures()
            failed_loaded = 0
            now = time.time()
            for row in failures:
                if float(row["expires_at"]) <= now:
                    continue
                fkey = (str(row["platform"]), str(row["handle_norm"]))
                self._failure_cache[fkey] = (
                    float(row["expires_at"]),
                    str(row.get("reason") or "平台暂时无法访问，请稍后重试"),
                    bool(int(row.get("temporary", 1))),
                )
                failed_loaded += 1
            if loaded or failed_loaded:
                logger.info(
                    "账号持久化缓存已加载: profiles=%d failures=%d",
                    loaded,
                    failed_loaded,
                )
        except Exception as exc:  # noqa: BLE001 - 缓存加载失败不影响运行
            logger.warning("账号持久化缓存加载失败，忽略: %s", exc)

    @staticmethod
    def _kind_for_key(key: tuple) -> str:
        """内存缓存 key → 持久化 kind（basic/detail[/analysis] + submissions 变体）。

        分析类缓存带版本号后缀：当扫描上限或分析口径变化时（提升
        ANALYSIS_CACHE_VERSION）旧缓存会被自动忽略，不必等 12 小时 TTL，
        也不会继续展示旧的「最多读取 N 条」覆盖率文案。
        """
        _, _, detail, submissions, analysis = key[:5]
        if not detail:
            return "basic"
        suffix = "_s" if submissions else ""
        if analysis:
            return f"analysis{suffix}_v{ANALYSIS_CACHE_VERSION}"
        return "detail" + suffix

    @classmethod
    def _key_from_kind(
        cls, platform: str, handle_norm: str, kind: str
    ) -> Optional[tuple]:
        if kind == "basic":
            return (platform, handle_norm, False, False, False)
        if kind == "detail":
            return (platform, handle_norm, True, False, False)
        if kind == "detail_s":
            return (platform, handle_norm, True, True, False)
        # 分析类：只接受当前版本的 kind，旧版本直接丢弃（返回 None 即跳过）。
        for suffix in ("", "_s"):
            if kind == f"analysis{suffix}_v{ANALYSIS_CACHE_VERSION}":
                return (
                    platform,
                    handle_norm,
                    True,
                    bool(suffix),
                    True,
                )
        return None

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
        failure_key = (platform, normalized.casefold())
        async with lock:
            cached = self._cache.get(key)
            if (
                not force
                and cached
                and time.time() - cached[0]
                < self._cache_ttl_for_profile(cached[1], ttl)
            ):
                return cached[1]
            if not force:
                failure = self._failure_cache.get(failure_key)
                if failure is not None and failure[0] > time.time():
                    raise AccountFetchError(failure[1], temporary=failure[2])
            logger.debug(
                "account_fetch_miss platform=%s handle=%s detail=%s analysis=%s",
                platform,
                normalized,
                detail,
                analysis_requested,
            )
            try:
                profile = await self._fetch_profile(
                    platform,
                    normalized,
                    detail,
                    include_submissions=include_submissions,
                    include_difficulty=analysis_requested,
                    include_analysis=analysis_requested,
                )
            except AccountFetchError as exc:
                self._record_failure(failure_key, exc)
                raise
            profile.fetched_at = time.time()
            # 成功抓取后清除该账号的旧负缓存，避免后续命中过期 failure。
            if failure_key in self._failure_cache:
                self._failure_cache.pop(failure_key, None)
                self._failure_cache_dirty.discard(failure_key)
                if self.cache_store is not None:
                    try:
                        await self.cache_store.delete_fetch_failure(
                            platform, normalized.casefold(), ""
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "清除账号负缓存失败 %s: %s", failure_key, exc
                        )
            self._cache[key] = (profile.fetched_at, profile)
            self._profile_cache_dirty.add(key)
            # 详细资料可以复用为摘要资料，减少后续排行请求。
            if detail:
                basic_key = (
                    platform,
                    normalized.casefold(),
                    False,
                    False,
                    False,
                )
                self._cache[basic_key] = (
                    profile.fetched_at,
                    profile,
                )
                self._profile_cache_dirty.add(basic_key)
            return profile

    def _record_failure(
        self,
        failure_key: Tuple[str, str],
        exc: AccountFetchError,
    ) -> None:
        """记录账号级负缓存；永久失败 5 分钟、临时失败 2 分钟。"""
        ttl = (
            FETCH_FAILURE_PERMANENT_TTL
            if not getattr(exc, "temporary", True)
            else FETCH_FAILURE_TEMP_TTL
        )
        self._failure_cache[failure_key] = (
            time.time() + ttl,
            str(exc).strip() or "平台暂时无法访问，请稍后重试",
            bool(getattr(exc, "temporary", True)),
        )
        self._failure_cache_dirty.add(failure_key)

    def prune_cache(
        self,
        *,
        max_entries: int = 5000,
        resource_max_entries: int = 2000,
        failure_max_entries: int = 2000,
    ) -> None:
        """淘汰过期/超限的进程内缓存，避免长时间运行内存无界增长。

        纯同步方法，在 asyncio 事件循环内短调用；只删除条目，不打断
        正在进行的抓取（single-flight 锁对象仅在无等待者时清理）。
        """
        now = time.time()
        # 负缓存：先清过期项，再按条数兜底。
        self._failure_cache = {
            key: value
            for key, value in self._failure_cache.items()
            if value[0] > now
        }
        overflow = len(self._failure_cache) - failure_max_entries
        if overflow > 0:
            oldest = sorted(
                self._failure_cache.items(),
                key=lambda pair: pair[1][0],
            )[:overflow]
            for key in oldest:
                self._failure_cache.pop(key[0], None)
        self._failure_cache_dirty &= set(self._failure_cache.keys())

        # 资料缓存：按 fetched_at 保留最近 max_entries 条。
        if len(self._cache) > max_entries:
            newest = sorted(
                self._cache.items(),
                key=lambda pair: pair[1][0],
                reverse=True,
            )[:max_entries]
            self._cache = dict(newest)
        self._profile_cache_dirty &= set(self._cache.keys())
        self._locks = {
            key: lock
            for key, lock in self._locks.items()
            if key in self._cache or lock.locked()
        }

        # 资源缓存（题目模型等大对象）：先清过期，再限容。
        self._resource_cache = {
            key: value
            for key, value in self._resource_cache.items()
            if value[0] + RESOURCE_CACHE_TTL > now
        }
        if len(self._resource_cache) > resource_max_entries:
            newest = sorted(
                self._resource_cache.items(),
                key=lambda pair: pair[1][0],
                reverse=True,
            )[:resource_max_entries]
            self._resource_cache = dict(newest)
        self._resource_locks = {
            key: lock
            for key, lock in self._resource_locks.items()
            if key in self._resource_cache or lock.locked()
        }

        # 牛客题库索引：条目本身不过期（难度/知识点几乎不变），
        # 整体新鲜度由 NOWCODER_PROBLEM_INDEX_TTL 控制，这里不清理。

    async def flush_persistent_cache(self) -> None:
        """把标脏的 profile/failure 缓存批量写回 SQLite（失败保留脏标记重试）。"""
        if self._nowcoder_problem_index_dirty:
            # 索引与 SQLite 后端无关，独立落盘（单题兜底补充的新题在这里持久化）。
            self.save_nowcoder_problem_index()
        if self.cache_store is None:
            return
        if not self._profile_cache_dirty and not self._failure_cache_dirty:
            return
        profile_snapshot = set(self._profile_cache_dirty)
        failure_snapshot = set(self._failure_cache_dirty)
        try:
            profile_entries = []
            for key in profile_snapshot:
                item = self._cache.get(key)
                if item is None:
                    continue
                fetched_at, profile = item
                kind = self._kind_for_key(key)
                base_ttl = (
                    ANALYSIS_CACHE_TTL
                    if kind in {"analysis", "analysis_s"}
                    else max(self.cache_ttl, DETAIL_CACHE_TTL)
                )
                expires_at = fetched_at + self._cache_ttl_for_profile(
                    profile, base_ttl
                )
                profile_entries.append(
                    {
                        "platform": str(key[0]),
                        "handle_norm": str(key[1]),
                        "kind": kind,
                        "payload": json.dumps(
                            profile.public_dict(), ensure_ascii=False
                        ),
                        "fetched_at": float(fetched_at),
                        "expires_at": float(expires_at),
                    }
                )
            failure_entries = []
            for fkey in failure_snapshot:
                entry = self._failure_cache.get(fkey)
                if entry is None:
                    continue
                expires_at, message, temporary = entry
                failure_entries.append(
                    {
                        "platform": str(fkey[0]),
                        "handle_norm": str(fkey[1]),
                        "kind": "",
                        "reason": str(message),
                        "temporary": bool(temporary),
                        "expires_at": float(expires_at),
                    }
                )
            if profile_entries:
                await self.cache_store.upsert_profile_cache(profile_entries)
            if failure_entries:
                await self.cache_store.upsert_fetch_failures(failure_entries)
            # 只清除本次快照中的脏标记；并发期间再次写脏的 key 会保留到下一轮。
            self._profile_cache_dirty -= profile_snapshot
            self._failure_cache_dirty -= failure_snapshot
        except Exception as exc:  # noqa: BLE001 - flush 失败保留脏标记，下轮重试
            logger.warning("账号持久化缓存 flush 失败（稍后重试）: %s", exc)

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

        profiles = await self._cf_bulk_fetch(missing, result, force)
        await self._fill_codeforces_ranks(profiles)
        return profiles

    async def _fill_codeforces_ranks(self, profiles) -> None:
        """给一批 CF 资料补平台内排名。

        群排行走的是 **批量 user.info 路径**（一次请求取全群），它直接构造
        profile、不经过单账号路径，因此必须在这里单独补一次——否则 CF 在群排行里
        永远没有排名（线上实测：53 行只有 1 行有值）。
        """
        try:
            ratings = await self._codeforces_rated_ratings()
        except Exception as exc:  # noqa: BLE001 - 排名是附加信息
            logger.warning("读取 CF 全站 rating 榜失败：%s", exc)
            return
        if not ratings:
            return
        positions = self._cf_rank_positions
        total = len(ratings)
        for profile in profiles.values():
            handle = str(getattr(profile, "handle", "") or "").casefold()
            rank = positions.get(handle)
            # **必须覆盖**：资料缓存里可能存着旧算法算错的名次（例如"多个第 1"），
            # 只有每次都按榜单位次重写，缓存里的脏值才会被纠正。
            profile.rating_rank = rank
            profile.rating_rank_total = (total or None) if rank else None

    async def _cf_bulk_fetch(
        self,
        missing: List[str],
        result: Dict[str, AccountProfile],
        force: bool,
    ) -> Dict[str, AccountProfile]:
        """CF user.info 批量拉取；进程内单飞，避免多群刷新重复请求。"""
        async with self._cf_bulk_lock:
            for offset in range(0, len(missing), 50):
                chunk = missing[offset : offset + 50]
                await self._cf_resolve_chunk(chunk, result, force)
            return result

    async def _cf_resolve_chunk(
        self,
        identifiers: List[str],
        result: Dict[str, AccountProfile],
        force: bool,
    ) -> None:
        """递归二分定位 user.info 中的失效账号。

        CF user.info 是 all-or-nothing：任一失效 handle 会让整批 FAILED。
        对失败批次二分，只对最小可疑子集做单账号请求，避免整 chunk 串行风暴。
        """
        if not identifiers:
            return
        try:
            data = await self._cf_json(
                "user.info", {"handles": ";".join(identifiers)}
            )
        except AccountFetchError as exc:
            if not getattr(exc, "temporary", True):
                if len(identifiers) == 1:
                    identifier = identifiers[0]
                    try:
                        profile = await self.get_profile(
                            "codeforces", identifier, force=force
                        )
                    except AccountFetchError as err:
                        logger.warning(
                            "Codeforces 账号 %s 回退失败: %s",
                            identifier,
                            err,
                        )
                        return
                    result[profile.handle.casefold()] = profile
                    return
                mid = len(identifiers) // 2
                await self._cf_resolve_chunk(
                    identifiers[:mid], result, force
                )
                await self._cf_resolve_chunk(
                    identifiers[mid:], result, force
                )
                return
            # 网络/服务临时失败：跳过本批。
            logger.warning(
                "Codeforces 批量 user.info 临时失败，跳过该批: %s", exc
            )
            return
        if not isinstance(data, dict) or data.get("status") != "OK":
            comment = str(
                (data or {}).get("comment") or ""
            ) if isinstance(data, dict) else ""
            if "not found" in comment.casefold():
                # 批内含失效账号：继续二分。
                if len(identifiers) == 1:
                    identifier = identifiers[0]
                    try:
                        profile = await self.get_profile(
                            "codeforces", identifier, force=force
                        )
                    except AccountFetchError as err:
                        logger.warning(
                            "Codeforces 账号 %s 回退失败: %s",
                            identifier,
                            err,
                        )
                        return
                    result[profile.handle.casefold()] = profile
                    return
                mid = len(identifiers) // 2
                await self._cf_resolve_chunk(identifiers[:mid], result, force)
                await self._cf_resolve_chunk(identifiers[mid:], result, force)
                return
            logger.warning("Codeforces 用户信息暂时无法获取")
            return
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
            self._profile_cache_dirty.add(key)
            result[profile.handle.casefold()] = profile

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

    # ------------------------------------------------------------------
    # 平台内排名（rating_rank）
    # ------------------------------------------------------------------
    def cf_rank_cache_path(self) -> Path:
        """CF 全站 rating 数组的落盘位置（只存整数，约 150KB）。"""
        return self.index_path().with_name("codeforces_rated_ratings.json")

    async def codeforces_global_rank(self, rating: Optional[int]) -> Optional[int]:
        """按 CF 全站已评级用户算该 rating 的名次（1 = 分数最高）。"""
        if not rating:
            return None
        ratings = await self._codeforces_rated_ratings()
        if not ratings:
            return None
        negative = [-value for value in ratings]
        return bisect.bisect_left(negative, -int(rating)) + 1

    async def _codeforces_rated_ratings(self) -> List[int]:
        """CF 全站已评级用户的 rating（降序），带内存 + 落盘缓存。"""
        cached = self._cf_rated_ratings
        if cached is not None and time.time() - cached[0] < CF_RATED_LIST_TTL:
            return cached[1]
        path = self.cf_rank_cache_path()
        try:
            if path.is_file():
                payload = json.loads(path.read_text(encoding="utf-8"))
                fetched_at = float(payload.get("fetched_at") or 0)
                ratings = [int(item) for item in (payload.get("ratings") or [])]
                # source=full 之前的缓存来自 activeOnly 请求，榜单不完整，直接丢弃
                if payload.get("source") != CF_RATED_LIST_SOURCE:
                    ratings = []
                handles = [str(item).casefold() for item in (payload.get("handles") or [])]
                if handles:
                    self._cf_rank_positions = {
                        handle: index + 1 for index, handle in enumerate(handles)
                    }
                if ratings and time.time() - fetched_at < CF_RATED_LIST_TTL:
                    self._cf_rated_ratings = (fetched_at, ratings)
                    return ratings
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("读取 CF 全站 rating 缓存失败：%s", exc)
        # 注意：**不能**带 activeOnly=true —— 那会漏掉 jiangly 这类"不活跃"的顶尖选手，
        # 他们的 rating 高于榜单最高分，名次会被算成 1（线上真实故障）。
        payload = None
        last_error: Optional[Exception] = None
        for attempt in range(1, CF_RATED_LIST_ATTEMPTS + 1):
            try:
                payload = await self._cf_json(
                    "user.ratedList", {}, timeout=CF_RATED_LIST_TIMEOUT
                )
                if isinstance(payload, dict) and payload.get("status") == "OK":
                    break
                last_error = ValueError(
                    f"CF ratedList 返回异常：{payload.get('comment') if isinstance(payload, dict) else payload}"
                )
            except Exception as exc:  # noqa: BLE001 - 失败要重试，不要直接清空名次
                last_error = exc
            payload = None
            logger.warning(
                "CF 全站榜单第 %d/%d 次拉取失败：%s",
                attempt,
                CF_RATED_LIST_ATTEMPTS,
                last_error,
            )
            # 立即重试（不 sleep）：CF 偶发 502/超时，重试能救回来，
            # 且避免测试/启动路径因为退避等待而变慢。
            if attempt < CF_RATED_LIST_ATTEMPTS:
                await asyncio.sleep(0)
        if payload is None:
            # 拉不到就用磁盘上的旧榜单兜底：名次会略旧，但比"全空"好得多
            stale = self._load_stale_cf_rank_cache(path)
            if stale:
                logger.warning("CF 全站榜单拉取失败，回退到旧榜单（%d 人）", len(stale))
            return stale
        rows = payload.get("result") or []
        # 榜单本身就是排名：按返回顺序取 handle（CF 已按 rating 降序返回），
        # 位次即名次。**不能**用"实时 rating 去榜单里二分"——ratedList 里的
        # rating 是过期快照（实测 BenQ user.info=3857 / ratedList=3650），
        # 会导致名次算错（线上真实故障：jiangly 与 BenQ 都显示第 1）。
        ordered = [
            str(row.get("handle") or "").casefold()
            for row in rows
            if isinstance(row, dict) and row.get("handle")
        ]
        self._cf_rank_positions = {handle: index + 1 for index, handle in enumerate(ordered)}
        ratings = sorted(
            (int(row.get("rating") or 0) for row in rows if isinstance(row, dict)),
            reverse=True,
        )
        ratings = [value for value in ratings if value > 0]
        if not ordered:
            return []
        self._cf_rated_ratings = (time.time(), ratings)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "source": CF_RATED_LIST_SOURCE,
                        "fetched_at": time.time(),
                        "ratings": ratings,
                        "handles": ordered,
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("写入 CF 全站 rating 缓存失败：%s", exc)
        logger.info("CF 全站 rating 榜已更新：%d 人", len(ratings))
        return ratings

    def _load_stale_cf_rank_cache(self, path: Path) -> List[int]:
        """读取磁盘上的旧榜单（忽略 TTL / source），仅用于拉取失败时兜底。"""
        try:
            if not path.is_file():
                return []
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("读取旧 CF 榜单失败：%s", exc)
            return []
        ratings = [int(item) for item in (payload.get("ratings") or [])]
        handles = [str(item).casefold() for item in (payload.get("handles") or [])]
        if handles:
            self._cf_rank_positions = {
                handle: index + 1 for index, handle in enumerate(handles)
            }
        return ratings

    async def _cf_json(
        self,
        method: str,
        params: Dict[str, str],
        *,
        timeout: float = 10.0,
    ) -> object:
        # timeout 可调：contest.standings 实测单场约 10 秒（248KB），
        # 默认 10 秒会偶发超时，调用方按需放宽（见 src/settlement.py）。
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
                    async with self.session.get(url, timeout=timeout) as response:
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
        # 平台内排名：**轻量资料也要算**——群排行走 detail=False，
        # 之前只写在 detail 分支里，导致群排行/快照永远拿不到 CF 名次。
        await self._fill_codeforces_ranks({canonical.casefold(): profile})
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
                rows, scanned_all = await self._cf_scan_submissions(canonical)
                if rows or scanned_all:
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
                            scanned_all=scanned_all,
                        )
        return profile

    async def _cf_scan_submissions(self, canonical: str) -> Tuple[list, bool]:
        """翻页扫描 Codeforces 提交记录；返回 (rows, 是否读完全部公开记录)。

        CF 单次请求文档上限为 CF_SUBMISSION_PAGE_SIZE 条，因此按页抓取：
        - 普通账号第一页就不满 → 一次请求返回，开销与旧实现相同；
        - 重度账号（如 2 万+ 提交）会翻 2~5 页，直到读完或触达上限，
          结果有 12 小时缓存，且只在个人资料卡的分析路径触发，不影响群排行。

        注意：这里**不能**为了“打卡热力图只需一年”而提前停止翻页——提交次数、
        通过题数、难度分布等统计口径依赖全量扫描，提前收手会让重度账号的
        统计被低估（曾因此让 5 万上限失效）。
        """
        rows: list = []
        offset = 1
        scanned_all = False
        while len(rows) < CF_SUBMISSION_SCAN_LIMIT:
            page_size = min(
                CF_SUBMISSION_PAGE_SIZE,
                CF_SUBMISSION_SCAN_LIMIT - len(rows),
            )
            status_data = await self._cf_json(
                "user.status",
                {
                    "handle": canonical,
                    "from": str(offset),
                    "count": str(page_size),
                },
            )
            if not (
                isinstance(status_data, dict)
                and status_data.get("status") == "OK"
            ):
                break
            page = status_data.get("result") or []
            rows.extend(page)
            if len(page) < page_size:
                scanned_all = True
                break
            offset += len(page)
        return rows, scanned_all

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
    def _cf_solved_problem_ids(rows: list) -> List[str]:
        """已通过题的稳定标识：`<contestId><index>`（题集题用 `set:<name>:<index>`）。"""
        ids: List[str] = []
        seen: set = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("verdict") or "").upper() != "OK":
                continue
            problem = row.get("problem") or {}
            if not isinstance(problem, dict):
                problem = {}
            index = problem.get("index") or row.get("problemIndex") or ""
            contest_id = problem.get("contestId") or row.get("contestId")
            problemset_name = problem.get("problemsetName") or row.get(
                "problemsetName"
            )
            if contest_id and index:
                key = f"{contest_id}{index}"
            elif problemset_name and index:
                key = f"set:{problemset_name}:{index}"
            else:
                continue
            if key in seen:
                continue
            seen.add(key)
            ids.append(key)
            if len(ids) >= SOLVED_PROBLEM_IDS_LIMIT:
                break
        return ids

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
    def _build_activity_daily(
        timestamps: Iterable[Optional[float]],
        *,
        days: int = ACTIVITY_HEATMAP_DAYS,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """把提交时间戳聚合成「按天打卡」数据，供热力图与连续天数使用。

        - 分日统一按北京时间（CN_TZ），与卡片其它时间字段一致；
        - 只保留最近 days 天（含今天），更早的记录丢弃；
        - 计数口径为当天**提交条数**（与 Codeforces 官方热力图一致）。
        """
        current = time.time() if now is None else float(now)
        today = datetime.fromtimestamp(current, tz=CN_TZ).date()
        earliest = today - timedelta(days=max(1, int(days)) - 1)
        daily: Dict[str, int] = {}
        for value in timestamps:
            timestamp = _parse_timestamp(value)
            if timestamp is None:
                continue
            if timestamp > current + 86400:
                # 未来时间戳（时钟偏差）直接忽略，避免把今天之后的日子点亮。
                continue
            day = datetime.fromtimestamp(timestamp, tz=CN_TZ).date()
            if day < earliest or day > today:
                continue
            key = day.isoformat()
            daily[key] = daily.get(key, 0) + 1

        def _streak(anchor: date) -> int:
            length = 0
            cursor = anchor
            while cursor >= earliest and daily.get(cursor.isoformat()):
                length += 1
                cursor -= timedelta(days=1)
            return length

        # 当前连续：今天有提交就从今天数；今天还没提交则从昨天数（不归零）。
        anchor = today if daily.get(today.isoformat()) else today - timedelta(days=1)
        current_streak = _streak(anchor)

        longest = 0
        running = 0
        cursor = earliest
        while cursor <= today:
            if daily.get(cursor.isoformat()):
                running += 1
                longest = max(longest, running)
            else:
                running = 0
            cursor += timedelta(days=1)

        return {
            "daily": daily,
            "active_days": len(daily),
            "current_streak": current_streak,
            "longest_streak": longest,
            "start": earliest.isoformat(),
            "end": today.isoformat(),
        }

    @staticmethod
    def _build_cf_analysis(
        rows: list,
        difficulty_distribution: List[Dict[str, Any]],
        solved_count: Optional[int],
        *,
        scanned_all: Optional[bool] = None,
    ) -> Dict[str, Any]:
        accepted_rows = [
            row
            for row in rows
            if isinstance(row, dict)
            and str(row.get("verdict") or "").upper() == "OK"
        ]
        solved_problem_ids = AccountFetcher._cf_solved_problem_ids(rows)
        language_counts: Dict[str, int] = {}
        active_days_30 = set()
        active_days_90 = set()
        now = time.time()
        submit_timestamps: List[Optional[float]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            language = (
                str(row.get("programmingLanguage") or "").strip()
                or "未标注"
            )
            language_counts[language] = language_counts.get(language, 0) + 1
            timestamp = _parse_timestamp(row.get("creationTimeSeconds"))
            submit_timestamps.append(timestamp)
            if timestamp is None:
                continue
            age = now - timestamp
            day = datetime.fromtimestamp(timestamp, tz=CN_TZ).date()
            if 0 <= age <= 30 * 86400:
                active_days_30.add(day)
            if 0 <= age <= 90 * 86400:
                active_days_90.add(day)
        activity = AccountFetcher._build_activity_daily(
            submit_timestamps, now=now
        )
        # scanned_all=True 表示最后一页不满（已读完公开记录）；None 时按旧口径
        # 用 len(rows) < 上限推断，兼容其它调用方。
        complete = (
            bool(scanned_all)
            if scanned_all is not None
            else len(rows) < CF_SUBMISSION_SCAN_LIMIT
        )
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
            "activity_daily": activity["daily"],
            "activity_summary": {
                "active_days": activity["active_days"],
                "current_streak": activity["current_streak"],
                "longest_streak": activity["longest_streak"],
                "start": activity["start"],
                "end": activity["end"],
            },
            "solved_problem_ids": solved_problem_ids,
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
        # 页面对高排名会截断成「9999+」，这种不能当精确名次（精确值走 rating-basic 接口）
        rank = None
        if len(status_numbers) > 1 and "+" not in status_numbers[1]:
            rank = _parse_int(status_numbers[1])
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

    # ------------------------------------------------------------------
    # 牛客题库索引：整库抓一次，之后按题目 ID 本地查表
    # ------------------------------------------------------------------
    def index_path(self) -> Path:
        """题库索引落盘位置：默认与 SQLite 同目录，可注入覆盖。"""
        if self._problem_index_path is not None:
            return self._problem_index_path
        try:
            from .account_store import default_store_path

            base = default_store_path().parent
        except Exception:  # noqa: BLE001 - 未运行在 AstrBot 中时回退
            base = Path("data")
        return base / NOWCODER_PROBLEM_INDEX_FILENAME

    @staticmethod
    def _normalize_difficulty(value: object) -> Optional[int]:
        """题库 JSON 里 1~5 / -1 是“未评定”哨兵值，统一按未标难度处理。"""
        difficulty = _parse_int(value)
        if difficulty is None or difficulty < NOWCODER_DIFFICULTY_MIN_VALID:
            return None
        return difficulty

    @classmethod
    def _parse_nowcoder_problem_index_page(
        cls,
        payload: object,
    ) -> Tuple[Dict[str, Dict[str, Any]], Optional[int]]:
        """解析题库 JSON 页 → ({problem_id: {"d": …, "t": […]}}, problemCount)。"""
        if not isinstance(payload, dict):
            raise AccountFetchError("牛客题库接口返回格式异常")
        if str(payload.get("code")) not in ("0", "None"):
            raise AccountFetchError(
                f"牛客题库接口错误: {payload.get('msg')}"
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise AccountFetchError("牛客题库接口缺少 data")
        problems: Dict[str, Dict[str, Any]] = {}
        for item in data.get("problemSets") or []:
            if not isinstance(item, dict):
                continue
            problem_id = str(item.get("problemId") or "").strip()
            if not problem_id:
                continue
            tags: List[str] = []
            for tag in item.get("tagList") or []:
                if not isinstance(tag, dict):
                    continue
                name = _clean_text(tag.get("name"))
                if name and name not in tags:
                    tags.append(name)
            problems[problem_id] = {
                # n = 题目名（每日一题/推荐补题直接展示它）
                "n": _clean_text(item.get("name")),
                "d": cls._normalize_difficulty(item.get("difficulty")),
                "t": tags,
            }
        count = _parse_int(data.get("problemCount"))
        return problems, count

    @staticmethod
    def _meta_from_index(
        problem_id: str, entry: Dict[str, Any]
    ) -> Dict[str, Any]:
        """把索引条目转换成分析层使用的元数据结构。"""
        return {
            "problem_id": problem_id,
            "title": str(entry.get("n") or ""),
            "difficulty": entry.get("d"),
            "tags": list(entry.get("t") or []),
        }

    def load_nowcoder_problem_index(self, *, force: bool = False) -> int:
        """从磁盘加载题库索引；过期/损坏/版本不符时忽略。"""
        now = time.time()
        if (
            not force
            and self._nowcoder_problem_index
            and now - self._nowcoder_problem_index_loaded_at
            < NOWCODER_PROBLEM_INDEX_TTL
        ):
            return len(self._nowcoder_problem_index)
        path = self.index_path()
        try:
            if not path.is_file():
                return len(self._nowcoder_problem_index)
            with path.open("r", encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("读取牛客题库索引失败，将重建：%s", exc)
            return len(self._nowcoder_problem_index)
        if not isinstance(payload, dict):
            return len(self._nowcoder_problem_index)
        if payload.get("version") != NOWCODER_PROBLEM_INDEX_VERSION:
            logger.info("牛客题库索引版本不匹配，忽略旧索引：%s", path)
            return len(self._nowcoder_problem_index)
        problems = payload.get("problems")
        if not isinstance(problems, dict):
            return len(self._nowcoder_problem_index)
        if len(problems) < NOWCODER_PROBLEM_INDEX_MIN_ENTRIES:
            logger.warning(
                "牛客题库索引条目过少（%d），忽略并重建：%s",
                len(problems),
                path,
            )
            return len(self._nowcoder_problem_index)
        fetched_at = payload.get("fetched_at")
        try:
            fetched_at = float(fetched_at)
        except (TypeError, ValueError):
            fetched_at = 0.0
        if fetched_at and time.time() - fetched_at > NOWCODER_PROBLEM_INDEX_TTL:
            logger.info(
                "牛客题库索引已过期（%.1f 天），等待后台重建",
                (time.time() - fetched_at) / 86400,
            )
            return len(self._nowcoder_problem_index)
        self._nowcoder_problem_index = {
            str(key): value
            for key, value in problems.items()
            if isinstance(value, dict)
        }
        absent = payload.get("absent")
        if isinstance(absent, list):
            self._nowcoder_problem_absent = {
                str(item) for item in absent if str(item)
            }
        self._nowcoder_problem_index_loaded_at = fetched_at or now
        logger.info(
            "已加载牛客题库索引：%d 题，缓存年龄 %.1f 小时",
            len(self._nowcoder_problem_index),
            max(0.0, now - (fetched_at or now)) / 3600,
        )
        return len(self._nowcoder_problem_index)

    def save_nowcoder_problem_index(self) -> None:
        """原子写入题库索引（失败保留脏标记，下次再写）。"""
        if not self._nowcoder_problem_index:
            self._nowcoder_problem_index_dirty = False
            return
        path = self.index_path()
        payload = {
            "version": NOWCODER_PROBLEM_INDEX_VERSION,
            "fetched_at": self._nowcoder_problem_index_loaded_at or time.time(),
            "problems": self._nowcoder_problem_index,
            "absent": sorted(self._nowcoder_problem_absent)[
                :NOWCODER_PROBLEM_ABSENT_MAX
            ],
        }
        temp_path = path.with_name(f".{path.name}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with temp_path.open("w", encoding="utf-8") as file:
                json.dump(
                    payload,
                    file,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            os.replace(temp_path, path)
            self._nowcoder_problem_index_dirty = False
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("写入牛客题库索引失败：%s", exc)
            try:
                temp_path.unlink()
            except OSError:
                pass

    def nowcoder_problem_index_ready(self) -> bool:
        """索引是否可用于本地查表（不触发构建）。"""
        if not self._nowcoder_problem_index:
            self.load_nowcoder_problem_index()
        return bool(self._nowcoder_problem_index)

    async def _fetch_nowcoder_problem_index_page(
        self, page: int
    ) -> Tuple[Dict[str, Dict[str, Any]], Optional[int]]:
        """抓取题库 JSON 的第 page 页，返回 (题目字典, problemCount)。"""
        params = urlencode(
            {
                "keyword": "",
                "pageSize": str(NOWCODER_PROBLEM_INDEX_PAGE_SIZE),
                "page": str(page),
            }
        )
        text = await self._fetch_text(
            f"{NOWCODER_PROBLEM_LIST_JSON_URL}?{params}",
            timeout=20.0,
        )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AccountFetchError("牛客题库接口返回的不是 JSON") from exc
        return self._parse_nowcoder_problem_index_page(payload)

    async def build_nowcoder_problem_index(self) -> Dict[str, Dict[str, Any]]:
        """整库抓取题库索引（约 290 次请求 / 2.5MB 传输），失败抛 AccountFetchError。

        返回 ``{problem_id: {"d": …, "t": […]}}``，由调用方决定是否替换内存索引。
        """
        first, count = await self._fetch_nowcoder_problem_index_page(1)
        if not first:
            raise AccountFetchError("牛客题库首页为空，构建索引失败")
        # 用首页的 problemCount 推算总页数；接口没返回时只保留首页。
        pages = (
            max(
                1,
                (count + NOWCODER_PROBLEM_INDEX_PAGE_SIZE - 1)
                // NOWCODER_PROBLEM_INDEX_PAGE_SIZE,
            )
            if count
            else 1
        )
        problems: Dict[str, Dict[str, Any]] = dict(first)
        failed = 0
        semaphore = asyncio.Semaphore(NOWCODER_PROBLEM_INDEX_CONCURRENCY)

        async def fetch_page(page: int):
            async with semaphore:
                try:
                    page_problems, _ = await self._fetch_nowcoder_problem_index_page(
                        page
                    )
                    return page_problems
                except (AccountFetchError, ValueError) as exc:
                    logger.warning("牛客题库第 %d 页抓取失败：%s", page, exc)
                    return None

        remaining = list(range(2, pages + 1))
        for start in range(0, len(remaining), NOWCODER_PROBLEM_INDEX_CONCURRENCY):
            batch = remaining[start : start + NOWCODER_PROBLEM_INDEX_CONCURRENCY]
            results = await asyncio.gather(*(fetch_page(page) for page in batch))
            for result in results:
                if result is None:
                    failed += 1
                    continue
                problems.update(result)
            if start + NOWCODER_PROBLEM_INDEX_CONCURRENCY < len(remaining):
                await asyncio.sleep(NOWCODER_ANALYSIS_PAGE_INTERVAL)
        if failed / max(1, pages) > 0.1:
            raise AccountFetchError(
                f"牛客题库索引构建失败：{failed}/{pages} 页抓取失败"
            )
        if len(problems) < NOWCODER_PROBLEM_INDEX_MIN_ENTRIES:
            raise AccountFetchError(
                f"牛客题库索引条目过少（{len(problems)}），放弃本次结果"
            )
        return problems

    async def warm_nowcoder_problem_index(self, *, force: bool = False) -> bool:
        """后台巡检：索引缺失或过期时整库重建；失败按指数退避。

        由 scheduler 周期调用，**不在用户请求路径上同步构建**。
        """
        now = time.time()
        if not force:
            if self.nowcoder_problem_index_ready() and (
                now - self._nowcoder_problem_index_loaded_at
                < NOWCODER_PROBLEM_INDEX_TTL
            ):
                return False
            if now < self._nowcoder_problem_index_backoff_until:
                return False
        async with self._nowcoder_problem_index_lock:
            # 二次检查：等锁期间可能已被其他巡检构建完成。
            if not force and (
                self._nowcoder_problem_index
                and now - self._nowcoder_problem_index_loaded_at
                < NOWCODER_PROBLEM_INDEX_TTL
            ):
                return False
            try:
                problems = await self.build_nowcoder_problem_index()
            except Exception as exc:  # noqa: BLE001 - 巡检失败不影响用户请求
                self._nowcoder_problem_index_consecutive_failures += 1
                failures = min(
                    self._nowcoder_problem_index_consecutive_failures, 6
                )
                delay = min(300 * (2 ** (failures - 1)), 3600)
                self._nowcoder_problem_index_backoff_until = time.time() + delay
                logger.warning(
                    "牛客题库索引构建失败（第 %d 次，%.0f 秒后重试）：%s",
                    failures,
                    delay,
                    exc,
                )
                return False
            self._nowcoder_problem_index = problems
            self._nowcoder_problem_index_loaded_at = time.time()
            self._nowcoder_problem_index_consecutive_failures = 0
            self._nowcoder_problem_index_backoff_until = 0.0
            self._nowcoder_problem_index_dirty = True
            self.save_nowcoder_problem_index()
            logger.info("牛客题库索引已更新：%d 题", len(problems))
            return True

    async def _fetch_nowcoder_problem_meta_one(
        self, problem_id: str
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """按题目 ID 单题查询（索引未命中时的兜底）。

        返回 ``(metadata, status)``：
        - ``("ok", {...})``：拿到难度/知识点；
        - ``("absent", None)``：题库列表确认没有这道题（多为比赛题/定制自测题，
          牛客未公开难度），调用方应记入负缓存、不必再回退 HTML；
        - ``("error", None)``：接口异常/改版，调用方可以回退 HTML 再试。
        """
        params = urlencode(
            {"keyword": problem_id, "pageSize": "1", "page": "1"}
        )
        try:
            text = await self._fetch_text(
                f"{NOWCODER_PROBLEM_LIST_JSON_URL}?{params}"
            )
            payload = json.loads(text)
            problems, _ = self._parse_nowcoder_problem_index_page(payload)
        except (AccountFetchError, json.JSONDecodeError, ValueError):
            problems = None
        if problems is not None:
            entry = problems.get(str(problem_id))
            if entry is None and len(problems) == 1:
                # 关键词命中但返回的是另一道题：只接受 ID 完全一致的候选，
                # 否则会张冠李戴（宁可当作缺失，也不要错误难度）。
                only_id, only_entry = next(iter(problems.items()))
                entry = only_entry if str(only_id) == str(problem_id) else None
            if entry is not None:
                return self._meta_from_index(problem_id, entry), "ok"
            return None, "absent"
        # 回退：题库列表页 HTML（旧实现，接口改版时仍可用）
        try:
            text = await self._fetch_text(
                f"{NOWCODER_PROBLEM_LIST_URL}?{params}"
            )
            metadata = self._parse_nowcoder_problem_metadata(text, problem_id)
        except AccountFetchError:
            return None, "error"
        if isinstance(metadata, dict):
            return (
                {
                    "problem_id": problem_id,
                    "title": metadata.get("title") or "",
                    "difficulty": self._normalize_difficulty(
                        metadata.get("difficulty")
                    ),
                    "tags": list(metadata.get("tags") or []),
                },
                "ok",
            )
        return None, "absent"

    async def _fetch_nowcoder_problem_metadata(
        self,
        problem_ids: List[str],
    ) -> Dict[str, Dict[str, Any]]:
        """取题目难度/知识点：优先本地题库索引查表，缺失才单题抓取。

        - 索引可用：命中即本地查表（零网络）；未命中的题目（新题/不在题库里的
          比赛题）做**限量**单题补查，并记住"确认不在题库"的题目，避免每次分析
          重复补查。
        - 索引不可用（首次部署尚未构建/构建失败）：退回逐题抓取，
          并沿用 NOWCODER_PROBLEM_META_FALLBACK_LIMIT 的降级上限。
        """
        unique_ids = list(dict.fromkeys(str(item) for item in problem_ids))
        result: Dict[str, Dict[str, Any]] = {}
        if self.nowcoder_problem_index_ready():
            missing: List[str] = []
            for problem_id in unique_ids:
                entry = self._nowcoder_problem_index.get(problem_id)
                if entry is None:
                    missing.append(problem_id)
                else:
                    result[problem_id] = self._meta_from_index(problem_id, entry)
            if missing:
                # 先过滤掉已确认不在题库里的题目（多为比赛题/定制自测题），
                # 再按上限限量补查，避免每次分析都为同一批查不到的题白等。
                lookup = [
                    problem_id
                    for problem_id in missing
                    if problem_id not in self._nowcoder_problem_absent
                ][:NOWCODER_PROBLEM_META_LOOKUP_LIMIT]
                if len(missing) > len(lookup):
                    logger.info(
                        "牛客题库索引未覆盖 %d 道题，本次补查 %d 道",
                        len(missing),
                        len(lookup),
                    )
                freshly = await self._fetch_nowcoder_problem_meta_many(lookup)
                for problem_id, metadata in freshly.items():
                    entry = {
                        "d": metadata.get("difficulty"),
                        "t": list(metadata.get("tags") or []),
                    }
                    self._nowcoder_problem_index[problem_id] = entry
                    self._nowcoder_problem_index_dirty = True
                    result[problem_id] = self._meta_from_index(problem_id, entry)
            return result

        limited = unique_ids[:NOWCODER_PROBLEM_META_FALLBACK_LIMIT]
        if len(unique_ids) > len(limited):
            logger.info(
                "牛客题库索引不可用，本次仅抓取前 %d/%d 道题的难度与知识点",
                len(limited),
                len(unique_ids),
            )
        return await self._fetch_nowcoder_problem_meta_many(limited)

    async def _fetch_nowcoder_problem_meta_many(
        self, problem_ids: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        """并发单题抓取（索引未命中/索引不可用时的实际网络路径）。

        "确认不在题库里"的题目会记入负缓存（随索引落盘），后续分析不再补查。
        """
        if not problem_ids:
            return {}
        semaphore = asyncio.Semaphore(NOWCODER_PROBLEM_META_CONCURRENCY)

        async def fetch_one(problem_id: str):
            async with semaphore:
                metadata, status = await self._fetch_nowcoder_problem_meta_one(
                    problem_id
                )
                return problem_id, metadata, status

        fetched = await asyncio.gather(
            *(fetch_one(problem_id) for problem_id in problem_ids)
        )
        result: Dict[str, Dict[str, Any]] = {}
        absent_before = len(self._nowcoder_problem_absent)
        for problem_id, metadata, status in fetched:
            if status == "absent":
                self._nowcoder_problem_absent.add(problem_id)
                continue
            if isinstance(metadata, dict):
                result[problem_id] = metadata
        if (
            len(self._nowcoder_problem_absent) != absent_before
            and len(self._nowcoder_problem_absent) <= NOWCODER_PROBLEM_ABSENT_MAX
        ):
            self._nowcoder_problem_index_dirty = True
        return result

    @staticmethod
    def _nowcoder_practice_pages(
        submission_count: Optional[int],
        data_total: Optional[int],
        page_size: int = NOWCODER_ANALYSIS_PAGE_SIZE,
        max_pages: int = NOWCODER_ANALYSIS_MAX_PAGES,
    ) -> int:
        """按提交数与分页总数决定要翻多少页（纯函数，便于单测）。

        提交数来自首页状态「次提交」，是权威口径；缺失时退回 ``data-total``
        （注意它是**当前 pageSize 下的页数**）。
        """
        size = max(1, int(page_size))
        limit = max(1, int(max_pages))
        if submission_count is not None and int(submission_count) > 0:
            needed = (int(submission_count) + size - 1) // size
        elif data_total:
            needed = int(data_total)
        else:
            needed = 1
        return max(1, min(needed, limit))

    @staticmethod
    async def _fetch_nowcoder_practice_page_safe(
        fetch_page, page: int, page_size: int
    ):
        """抓取单页；失败返回 None（由调用方标记 pages_missing）。"""
        try:
            return await fetch_page(page, page_size)
        except AccountFetchError:
            return None

    @staticmethod
    def _dedupe_nowcoder_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按 submission_id 去重（无 ID 的行原样保留，保持抓取顺序）。"""
        seen: set = set()
        unique: List[Dict[str, Any]] = []
        for row in rows:
            submission_id = str(row.get("submission_id") or "").strip()
            if submission_id:
                if submission_id in seen:
                    continue
                seen.add(submission_id)
            unique.append(row)
        return unique

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

        async def fetch_page(page: int, page_size: int) -> str:
            params = dict(base_params)
            params["pageSize"] = str(page_size)
            params["page"] = str(page)
            return await self._fetch_text(
                f"{NOWCODER_PRACTICE_URL.format(uid=uid)}?"
                f"{urlencode(params)}",
                headers={"Accept-Language": "zh-CN,zh;q=0.9"},
            )

        first_text = await fetch_page(1, NOWCODER_ANALYSIS_PAGE_SIZE)
        stats, first_rows, page_total = (
            self._parse_nowcoder_practice_page(first_text)
        )
        if not first_rows and not stats:
            # 对端可能收紧了 pageSize 上限：降级到 100 再试一次。
            logger.warning(
                "牛客练习页首页无数据（pageSize=%d），降级到 %d 重试",
                NOWCODER_ANALYSIS_PAGE_SIZE,
                NOWCODER_ANALYSIS_FALLBACK_PAGE_SIZE,
            )
            first_text = await fetch_page(
                1, NOWCODER_ANALYSIS_FALLBACK_PAGE_SIZE
            )
            stats, first_rows, page_total = (
                self._parse_nowcoder_practice_page(first_text)
            )
            base_params["pageSize"] = str(
                NOWCODER_ANALYSIS_FALLBACK_PAGE_SIZE
            )
        page_size = int(base_params["pageSize"])
        submission_count = stats.get("submission_count")
        pages_to_fetch = self._nowcoder_practice_pages(
            submission_count, page_total, page_size
        )
        rows = list(first_rows)
        pages_missing = False
        # 分批并发翻页：每批结束后检查是否出现空页（提交数被清空/隐私账号），
        # 出现即停止，避免无谓地打满 100 页。
        for start in range(2, pages_to_fetch + 1, NOWCODER_ANALYSIS_CONCURRENCY):
            batch = list(
                range(
                    start,
                    min(
                        start + NOWCODER_ANALYSIS_CONCURRENCY,
                        pages_to_fetch + 1,
                    ),
                )
            )
            results = await asyncio.gather(
                *(
                    self._fetch_nowcoder_practice_page_safe(
                        fetch_page, page, page_size
                    )
                    for page in batch
                )
            )
            empty_page = False
            for text in results:
                if not text:
                    pages_missing = True
                    continue
                _, page_rows, _ = self._parse_nowcoder_practice_page(text)
                if not page_rows:
                    empty_page = True
                    continue
                rows.extend(page_rows)
            if empty_page:
                break
            if batch[-1] < pages_to_fetch:
                await asyncio.sleep(NOWCODER_ANALYSIS_PAGE_INTERVAL)

        # 跨页按 submission_id 去重：分页边界在抓取期间发生偏移时会出现重复行，
        # 重复会让 AC 率与语言分布失真。
        rows = self._dedupe_nowcoder_rows(rows)
        total_submissions = (
            submission_count if submission_count is not None else len(rows)
        )
        truncated = (
            submission_count is not None
            and submission_count > NOWCODER_ANALYSIS_SCAN_LIMIT
        )
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

        expected_rows = min(total_submissions, NOWCODER_ANALYSIS_SCAN_LIMIT)
        complete = (
            not pages_missing
            and not truncated
            and len(rows) >= expected_rows
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
        solved_problem_ids = sorted(
            item for item in unique_solved if item
        )[:SOLVED_PROBLEM_IDS_LIMIT]
        index_ready = self.nowcoder_problem_index_ready()
        coverage = (
            f"练习页读取 {len(rows)}/{total_submissions} 条提交；"
            f"题目元数据 {len(metadata)}/{len(unique_solved)}"
        )
        if truncated:
            coverage += (
                f"（最多读取 {NOWCODER_ANALYSIS_SCAN_LIMIT} 条 / "
                f"{NOWCODER_ANALYSIS_MAX_PAGES} 页）"
            )
        elif pages_missing:
            coverage += "（部分分页读取失败）"
        if not index_ready:
            coverage += "（题目索引构建中）"
        return {
            "source": "牛客公开练习页 + 牛客题库列表",
            "coverage": coverage,
            "submission_count": total_submissions,
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
            "solved_problem_ids": solved_problem_ids,
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
                    {"label": "提交", "value": total_submissions},
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
        rated_cell = self._atcoder_table_value(text, "Rated Matches")
        affiliation = self._atcoder_table_value(text, "Affiliation")
        # 用户页 Rank 行形如「48th (Top 0.04%)」→ 平台内排名
        rank_cell_html = self._atcoder_table_value(text, "Rank")
        rank_cell_text = _clean_text(rank_cell_html)
        rank_number_match = re.search(r"(\d+)", rank_cell_text)
        rating_rank = (
            int(rank_number_match.group(1)) if rank_number_match else None
        )
        # 形如「48th (Top 0.04%)」→ 取出平台自带的百分位文案
        percentile_match = re.search(r"(Top\s*[\d.]+%)", rank_cell_text, re.I)
        rating_rank_note = percentile_match.group(1) if percentile_match else "" 
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
            rating_rank=rating_rank,
            rating_rank_note=rating_rank_note,
            # 用户页的「Rank」行是"全站名次（百分位）"，不是段位；
            # 段位用颜色类（gray/brown/…/red），否则会和平台排名重复且被截断。
            rank_text="",
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
            # 页间留一点间隔，避免 2 万条（40 页）时把 kenkoooo 打得太急。
            await asyncio.sleep(ATCODER_SUBMISSION_PAGE_INTERVAL)

        if not submissions:
            return {
                "source": "AtCoder Problems（提交记录 + 估计难度）",
                "coverage": "未取得公开提交记录",
                "submission_count": 0,
                "accepted_submission_count": 0,
                "solved_count": 0,
                "acceptance_rate": None,
                # 没有公开提交 → 已通过题集合为空（此前误用了未定义的变量）
                "solved_problem_ids": [],
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
        activity = AccountFetcher._build_activity_daily(
            (item.get("epoch_second") for item in submissions if isinstance(item, dict)),
            now=now,
        )

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
            "activity_daily": activity["daily"],
            "activity_summary": {
                "active_days": activity["active_days"],
                "current_streak": activity["current_streak"],
                "longest_streak": activity["longest_streak"],
                "start": activity["start"],
                "end": activity["end"],
            },
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
