"""各平台比赛数据抓取（aiohttp + 公开接口/页面）。"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import aiohttp

from astrbot.api import logger

from .models import CN_TZ, Contest, DEFAULT_PLATFORMS, PLATFORM_LABELS
try:
    from .models import OfflineContest
except ImportError:  # 兼容旧版本文件未同步完成的临时状态
    @dataclass
    class OfflineContest:  # type: ignore[no-redef]
        """旧部署缺少新模型时的兼容模型，完整更新后会使用 models.py 版本。"""

        name: str
        start_date: date
        end_date: Optional[date] = None
        venue: str = ""
        organizer: str = ""
        problem_setter: str = ""
        official_url: str = ""
        allocation_plan: str = ""
        source_url: str = ""

        def event_end_date(self) -> date:
            return self.end_date or self.start_date

        def is_upcoming(self) -> bool:
            return self.event_end_date() >= datetime.now(CN_TZ).date()

        def date_text(self) -> str:
            if self.end_date and self.end_date != self.start_date:
                return f"{self.start_date:%Y-%m-%d} 至 {self.end_date:%Y-%m-%d}"
            return f"{self.start_date:%Y-%m-%d}"

        def format_detail(self) -> str:
            lines = [
                "🏟 线下赛",
                f"🏷 {self.name}",
                f"🕐 日期：{self.date_text()}（北京时间）",
            ]
            if self.venue:
                lines.append(f"📍 赛站/地点：{self.venue}")
            if self.organizer:
                lines.append(f"🏫 主办方：{self.organizer}")
            if self.problem_setter:
                lines.append(f"🧩 出题组：{self.problem_setter}")
            if self.allocation_plan:
                lines.append(f"📌 名额/规则：{self.allocation_plan}")
            if self.official_url:
                lines.append(f"🔗 官方通知：{self.official_url}")
            lines.append(f"📚 数据源：XCPC Link（{self.source_url}）")
            return "\n".join(lines)

from .utils import LENTILLE_RE, USER_AGENT, fetch_text_with_retry

CF_API_URL = "https://codeforces.com/api/contest.list"
NC_CALENDAR_URL = "https://ac.nowcoder.com/acm/calendar/contest"
NC_REFERER = "https://ac.nowcoder.com/acm/contest/calendar"
LUOGU_CONTEST_URL = "https://www.luogu.com.cn/contest/list"
ATCODER_CONTESTS_URL = "https://atcoder.jp/contests/"
# 牛客日历按自然月返回，且只收录"已排期"的比赛；多取几个月可以在牛客提前
# 排期时（暑期多校、国庆/寒假集训营等成系列赛事）更早看到完整赛程。
# 0 = 只抓当前月（旧行为）。
NC_MONTH_LOOKAHEAD = 3
# 当前月与下月属于"必须拿到"：失败即抛错，由 fetch_platform 回退到旧缓存，
# 与历史语义保持一致；更远的月份属于尽力而为，单个空月份抖动不应该让
# 整个牛客刷新失败。
NC_REQUIRED_MONTHS = 2
# 牛客赛事口径：all = 日历里全部 NowCoder 赛事（含高校校赛/同步赛/自主创建赛）；
# series_only = 仅比赛名含"牛客"的系列赛（1.11.0 之前的旧口径）。
NOWCODER_SCOPE_ALL = "all"
NOWCODER_SCOPE_SERIES_ONLY = "series_only"
NOWCODER_SCOPES = (NOWCODER_SCOPE_ALL, NOWCODER_SCOPE_SERIES_ONLY)
NC_SERIES_KEYWORD = "牛客"
# 洛谷比赛列表按开始时间倒序、每页 20 条，未开始的比赛都在第 1 页；
# 只有第 1 页被填满且最后一条仍未开始时才继续翻页（隐含页上限的保险）。
LUOGU_CONTEST_PAGE_SIZE = 20
LUOGU_CONTEST_MAX_PAGES = 3
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
OFFLINE_PLATFORM = "offline"
QUERY_PLATFORMS = [*DEFAULT_PLATFORMS, OFFLINE_PLATFORM]
PLATFORM_LABELS.setdefault(OFFLINE_PLATFORM, "线下赛")

# 在线平台数据变化较快，保留原来的短缓存；线下赛程通常按天/周更新，
# 使用更长缓存并落盘，避免插件重载后第一次查询又重新下载首页和 JS chunk。
DEFAULT_CACHE_TTL = 5 * 60
OFFLINE_CACHE_TTL = 30 * 60
CACHE_VERSION = 1
CACHE_FILE_NAME = "contest_cache.json"
OFFLINE_SCRIPT_BATCH_SIZE = 8
MAX_OFFLINE_SCRIPTS = 32


class ContestFetcher:
    """比赛抓取器，带内存缓存、持久化缓存和并发请求合并。"""

    def __init__(
        self,
        cache_ttl: int = DEFAULT_CACHE_TTL,
        offline_cache_ttl: int = OFFLINE_CACHE_TTL,
        cache_path: Optional[str | Path] = None,
    ) -> None:
        self.cache_ttl = max(0, int(cache_ttl))
        self.offline_cache_ttl = max(0, int(offline_cache_ttl))
        # 牛客赛事展示口径（由插件设置同步）：缓存里始终保存全量，
        # 只在返回给调用方时过滤，因此切换口径无需重新抓取。
        self.nowcoder_scope = NOWCODER_SCOPE_ALL
        self._cache: Dict[str, Tuple[float, list]] = {}
        self._source_urls: Dict[str, str] = {}
        self._fetch_locks: Dict[str, asyncio.Lock] = {}
        # 预热失败退避：{platform: (下次允许预热时间, 连续失败次数)}，
        # 避免某个平台持续抓取失败时每 30 秒重试、打爆对端。
        self._warm_backoff: Dict[str, Tuple[float, int]] = {}
        self._cache_loaded = False
        self.cache_path = Path(cache_path) if cache_path else (
            Path(__file__).resolve().parent.parent / "data" / CACHE_FILE_NAME
        )
        self.session: Optional[aiohttp.ClientSession] = None

    async def initialize(self) -> None:
        self._load_persistent_cache()
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
        """抓取指定平台，返回 (比赛列表, 错误提示)。

        普通查询优先命中缓存；过期后同一平台只允许一个协程实际抓取，
        其他并发查询会复用这次结果。网络失败时保留旧缓存作为兜底。
        牛客赛事会按 ``nowcoder_scope`` 过滤后再返回（缓存内始终是全量）。
        """
        self._load_persistent_cache()
        request_started = time.time()
        if not force:
            cached = self._cache.get(platform)
            if cached and self._is_cache_fresh(platform, cached, request_started):
                self._log_cache_hit(platform, cached[0])
                return self._apply_scope(platform, cached[1]), None

        lock = self._fetch_locks.get(platform)
        if lock is None:
            lock = asyncio.Lock()
            self._fetch_locks[platform] = lock

        async with lock:
            # 二次检查很重要：多个群同时触发时，排队中的协程直接复用
            # 前一个协程刚写入的结果。force=True 时不再复用“本次请求之前
            # 就存在的旧缓存”，但保留对并发刷新的复用（cached[0] >= 请求开始）。
            cached = self._cache.get(platform)
            if cached and (
                (not force and self._is_cache_fresh(platform, cached))
                or cached[0] >= request_started
            ):
                self._log_cache_hit(platform, cached[0], joined=True)
                return self._apply_scope(platform, cached[1]), None

            label = PLATFORM_LABELS.get(platform, platform)
            logger.info("开始抓取比赛数据：%s（缓存已过期或不存在）", label)
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
                error = f"⏰ {label} 数据获取超时，请稍后重试"
                return self._fallback_to_stale_cache(platform, error)
            except aiohttp.ClientError as exc:
                logger.error("比赛数据网络错误(%s): %s", platform, exc)
                error = f"🌐 {label} 网络连接失败，请检查网络后重试"
                return self._fallback_to_stale_cache(platform, error)
            except (ValueError, KeyError, json.JSONDecodeError, re.error) as exc:
                logger.error("比赛数据解析失败(%s): %s", platform, exc)
                error = f"🔧 {label} 数据格式有变化，请联系管理员修复"
                return self._fallback_to_stale_cache(platform, error)
            except Exception as exc:  # noqa: BLE001
                logger.error("比赛数据未知错误(%s): %s", platform, exc, exc_info=True)
                error = f"❌ 获取 {label} 数据失败，请稍后重试"
                return self._fallback_to_stale_cache(platform, error)

            fetched_at = time.time()
            self._cache[platform] = (fetched_at, contests)
            self._save_persistent_cache()
            scoped = self._apply_scope(platform, contests)
            logger.info(
                "比赛数据抓取完成：%s，共 %d 场；缓存有效期 %d 秒",
                label,
                len(scoped),
                self._cache_ttl(platform),
            )
            return scoped, None

    def _apply_scope(self, platform: str, contests: list) -> list:
        """按当前牛客口径过滤赛事；其他平台/口径原样返回。

        缓存里始终保存全量牛客赛事，只有这里做展示口径过滤，
        因此管理员切换 ``nowcoder_scope`` 后立即生效、无需重新抓取。
        """
        if platform != "nowcoder" or self.nowcoder_scope != NOWCODER_SCOPE_SERIES_ONLY:
            return contests
        return [
            contest
            for contest in contests
            if NC_SERIES_KEYWORD in str(getattr(contest, "name", "") or "")
        ]

    def _cache_ttl(self, platform: str) -> int:
        return self.offline_cache_ttl if platform == OFFLINE_PLATFORM else self.cache_ttl

    def _is_cache_fresh(
        self,
        platform: str,
        cached: Tuple[float, list],
        now: Optional[float] = None,
    ) -> bool:
        current = time.time() if now is None else now
        return current - cached[0] < self._cache_ttl(platform)

    def _is_cache_expiring_soon(
        self,
        platform: str,
        window: float,
        now: Optional[float] = None,
    ) -> bool:
        """缓存是否将在 window 秒内过期（用于后台预热）。"""
        cached = self._cache.get(platform)
        if cached is None:
            return True
        current = time.time() if now is None else now
        remaining = self._cache_ttl(platform) - (current - cached[0])
        return remaining <= window

    async def warm(
        self,
        platforms: Optional[list] = None,
        *,
        window: float = 90.0,
        max_platforms: int = 1,
    ) -> int:
        """后台预热：把即将过期的平台提前刷新，避免用户请求时同步等待。

        返回本次实际刷新成功的平台数。失败按平台做指数退避，且每个 tick
        的尝试次数受 max_platforms 限制，避免失败风暴打爆对端。
        并发/失败都由 fetch_platform 内部的 single-flight 与旧缓存兜底处理。
        """
        candidates = list(platforms) if platforms else list(QUERY_PLATFORMS)
        limit = max(1, int(max_platforms))
        now = time.time()
        warmed = 0
        attempts = 0
        for platform in candidates:
            if attempts >= limit:
                break
            if platform not in self._cache:
                # 从未抓取过：交给首次用户请求，避免启动瞬间打满网络。
                continue
            if not self._is_cache_expiring_soon(platform, window):
                continue
            backoff = self._warm_backoff.get(platform)
            if backoff is not None and now < backoff[0]:
                continue
            label = PLATFORM_LABELS.get(platform, platform)
            attempts += 1
            before = self._cache.get(platform)
            before_ts = before[0] if before else None
            try:
                # force=True：绕过尚未过期的缓存真正刷新，但 single-flight
                # 仍然生效，多个并发预热不会重复抓取。
                contests, error = await self.fetch_platform(platform, force=True)
                after = self._cache.get(platform)
                after_ts = after[0] if after else None
                # 注意：抓取失败时 fetch_platform 会回退旧缓存并返回 error=None，
                # 因此必须用“缓存时间戳是否前进”判断是否真的刷新成功。
                refreshed = after_ts is not None and after_ts != before_ts
                if error or not refreshed:
                    self._record_warm_failure(platform)
                    logger.warning(
                        "后台预热 %s 未刷新（%s），进入退避",
                        label,
                        error or "回退旧缓存",
                    )
                else:
                    self._warm_backoff.pop(platform, None)
                    logger.info(
                        "后台预热完成：%s，共 %d 场", label, len(contests)
                    )
                    warmed += 1
            except Exception as exc:  # noqa: BLE001 - 预热失败不影响主流程
                self._record_warm_failure(platform)
                logger.warning("后台预热 %s 异常：%s", label, exc)
        return warmed

    def _record_warm_failure(self, platform: str) -> None:
        """记录预热失败并设置指数退避（5 分钟起，最长 30 分钟）。"""
        _, failures = self._warm_backoff.get(platform, (0.0, 0))
        failures = min(failures + 1, 6)
        delay = min(300 * (2 ** (failures - 1)), 1800)
        self._warm_backoff[platform] = (time.time() + delay, failures)

    def _log_cache_hit(
        self, platform: str, fetched_at: float, *, joined: bool = False
    ) -> None:
        age = max(0.0, time.time() - fetched_at)
        label = PLATFORM_LABELS.get(platform, platform)
        suffix = "（并发请求复用）" if joined else ""
        # 线下赛查询频率通常较低，保留 info 日志便于确认是否命中缓存；
        # 定时推送中的在线平台命中则降为 debug，避免刷屏。
        if platform == OFFLINE_PLATFORM:
            logger.info("%s 命中缓存，缓存年龄 %.1f 秒%s", label, age, suffix)
        else:
            logger.debug("%s 命中缓存，缓存年龄 %.1f 秒%s", label, age, suffix)

    def _fallback_to_stale_cache(
        self, platform: str, error: str
    ) -> Tuple[list, Optional[str]]:
        cached = self._cache.get(platform)
        if cached is None:
            return [], error
        age = max(0.0, time.time() - cached[0])
        label = PLATFORM_LABELS.get(platform, platform)
        logger.warning(
            "%s 数据刷新失败，使用过期缓存（缓存年龄 %.1f 秒）：%s",
            label,
            age,
            error,
        )
        return self._apply_scope(platform, cached[1]), None

    def _load_persistent_cache(self) -> None:
        """从插件 data 目录加载上次成功抓取的结果。"""
        if self._cache_loaded:
            return
        self._cache_loaded = True
        try:
            if not self.cache_path.is_file():
                return
            with self.cache_path.open("r", encoding="utf-8") as file:
                payload = json.load(file)
            if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
                logger.info("比赛缓存版本不匹配，忽略旧缓存：%s", self.cache_path)
                return
            platforms = payload.get("platforms")
            if not isinstance(platforms, dict):
                return
            for platform, entry in platforms.items():
                if platform not in QUERY_PLATFORMS or not isinstance(entry, dict):
                    continue
                try:
                    fetched_at = float(entry["fetched_at"])
                    contests = self._deserialize_contests(
                        platform, entry.get("contests", [])
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    logger.warning("比赛缓存条目无效（%s）：%s", platform, exc)
                    continue
                self._cache[platform] = (fetched_at, contests)
                source_url = str(entry.get("source_url") or "")
                if source_url:
                    self._source_urls[platform] = source_url
                logger.info(
                    "已加载比赛缓存：%s，共 %d 场，缓存年龄 %.1f 秒",
                    PLATFORM_LABELS.get(platform, platform),
                    len(contests),
                    max(0.0, time.time() - fetched_at),
                )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("读取比赛持久化缓存失败，将重新抓取：%s", exc)

    def _deserialize_contests(self, platform: str, raw: object) -> list:
        if not isinstance(raw, list):
            raise ValueError("contests 不是列表")
        contests = []
        model = OfflineContest if platform == OFFLINE_PLATFORM else Contest
        for item in raw:
            if not isinstance(item, dict):
                continue
            if hasattr(model, "model_validate"):
                contests.append(model.model_validate(item))
                continue
            # 兼容旧部署中临时使用的 dataclass OfflineContest。
            item_copy = dict(item)
            for field in ("start_date", "end_date"):
                value = item_copy.get(field)
                if value and isinstance(value, str):
                    item_copy[field] = date.fromisoformat(value)
            contests.append(model(**item_copy))
        return contests

    @staticmethod
    def _serialize_contest(contest: object) -> dict:
        if hasattr(contest, "model_dump"):
            return contest.model_dump(mode="json")
        if hasattr(contest, "__dataclass_fields__"):
            return asdict(contest)
        raise TypeError(f"不支持的比赛模型：{type(contest)!r}")

    def _save_persistent_cache(self) -> None:
        """原子写入缓存文件，避免进程中断留下半个 JSON。"""
        payload = {"version": CACHE_VERSION, "platforms": {}}
        for platform, (fetched_at, contests) in self._cache.items():
            try:
                serialized = [self._serialize_contest(item) for item in contests]
            except (TypeError, ValueError) as exc:
                logger.warning("跳过无法序列化的比赛缓存（%s）：%s", platform, exc)
                continue
            payload["platforms"][platform] = {
                "fetched_at": fetched_at,
                "source_url": self._source_urls.get(platform, ""),
                "contests": serialized,
            }

        temp_path = self.cache_path.with_name(f".{self.cache_path.name}.tmp")
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with temp_path.open("w", encoding="utf-8") as file:
                json.dump(
                    payload,
                    file,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=lambda value: (
                        value.isoformat()
                        if isinstance(value, (date, datetime))
                        else str(value)
                    ),
                )
                file.write("\n")
            os.replace(temp_path, self.cache_path)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("写入比赛持久化缓存失败：%s", exc)
            try:
                temp_path.unlink()
            except OSError:
                pass

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

    @staticmethod
    def _nc_months(
        now: Optional[datetime] = None,
        lookahead: int = NC_MONTH_LOOKAHEAD,
    ) -> List[str]:
        """返回 ``[当前月, 当前月 + lookahead]`` 的月份串（自动跨年）。

        日历接口按自然月返回整月比赛，因此窗口越大、能提前看到的赛程越远。
        """
        current = now or datetime.now(timezone.utc)
        months: List[str] = []
        for offset in range(max(0, int(lookahead)) + 1):
            index = current.month - 1 + offset
            months.append(f"{current.year + index // 12}-{index % 12 + 1}")
        return months

    @staticmethod
    def _nc_platform_of_item(item: dict) -> Optional[str]:
        """按 OJ 归属判定一条牛客日历条目的平台。

        牛客日历同时收录 NowCoder / AtCoder / CodeForces 的比赛，
        ``ojName`` 的真实取值是英文 ``NowCoder``（不是中文"牛客"），
        因此必须按 OJ 身份判定；``ojName`` 缺失或改版时回退比赛名。
        """
        oj_name = str(item.get("ojName") or "").strip().lower()
        name = str(item.get("contestName") or "")
        if "nowcoder" in oj_name or "牛客" in oj_name:
            return "nowcoder"
        if "atcoder" in oj_name:
            return "atcoder"
        if "codeforces" in oj_name:
            return None
        if NC_SERIES_KEYWORD in name:
            return "nowcoder"
        if "atcoder" in name.lower():
            return "atcoder"
        return None

    @staticmethod
    def _nc_item_to_contest(platform: str, item: dict) -> Optional[Contest]:
        """把一条日历条目转成 Contest；时间字段异常时返回 None。"""
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
            return None
        duration = int((end - start).total_seconds() // 60) if end else 0
        return Contest(
            platform=platform,
            name=str(item.get("contestName") or ""),
            start_time=start,
            end_time=end,
            duration_minutes=duration,
            url=str(item.get("link") or ""),
            contest_id=str(item.get("contestId") or ""),
        )

    async def _fetch_nowcoder_calendar(self, platform: str) -> List[Contest]:
        """牛客比赛日历：滚动取多个自然月，再按 OJ 归属筛出目标平台赛事。

        日历里混排了牛客 / AtCoder / CodeForces 的比赛，必须按 ``ojName``
        判定归属：AtCoder 与 CodeForces 由各自官方源提供（更全、更新更快），
        这里只保留目标平台的比赛。
        """
        months = self._nc_months()
        required = max(1, min(NC_REQUIRED_MONTHS, len(months)))
        results = await asyncio.gather(
            *(self._fetch_nc_month(month) for month in months),
            return_exceptions=True,
        )

        merged: List[Contest] = []
        seen: set = set()
        raw_total = 0
        unknown = 0
        by_platform: Dict[str, int] = {}
        for index, (month, result) in enumerate(zip(months, results)):
            if isinstance(result, BaseException):
                if index < required:
                    # 必需月失败：保持历史语义——整体失败，由调用方回退旧缓存。
                    raise result
                logger.warning(
                    "牛客日历 %s 抓取失败（非必需月，忽略该月）：%s",
                    month,
                    result,
                )
                continue
            raw_total += len(result)
            for item in result:
                item_platform = self._nc_platform_of_item(item)
                if item_platform is None:
                    unknown += 1
                    continue
                by_platform[item_platform] = by_platform.get(item_platform, 0) + 1
                if item_platform != platform:
                    continue
                contest = self._nc_item_to_contest(platform, item)
                if contest is None:
                    continue
                key = (contest.platform, contest.contest_id)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(contest)
        merged.sort(key=lambda c: c.start_time)
        label = PLATFORM_LABELS.get(platform, platform)
        others = "、".join(
            f"{PLATFORM_LABELS.get(name, name)} {count}"
            for name, count in sorted(by_platform.items())
            if name != platform
        )
        logger.info(
            "牛客日历 %s ~ %s：原始 %d 场，保留 %s %d 场%s%s",
            months[0],
            months[-1],
            raw_total,
            label,
            len(merged),
            f"（其他 OJ：{others}）" if others else "",
            f"（未识别 OJ {unknown} 场）" if unknown else "",
        )
        return merged

    async def _fetch_nc_month(self, month: str) -> List[dict]:
        """取某个月的牛客日历原始条目（不做平台筛选）。

        日历接口同时返回牛客 / AtCoder / CodeForces 的比赛，归属判定由
        ``_nc_platform_of_item`` 完成；AtCoder 自 1.1.1 起改用官网赛程表，
        本函数实际只服务于牛客。
        """
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
        items = data.get("data") or []
        return [item for item in items if isinstance(item, dict)]

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
        """洛谷比赛列表：页面公开可访问，无需登录 Cookie。

        列表按开始时间倒序，未开始的比赛必然落在第 1 页；只有第 1 页被
        填满（= ``perPage`` 条）且最后一条仍未开始时，才继续翻页，
        用于兜住"未来赛事超过一页"的隐含上限。
        """
        if self.session is None:
            raise RuntimeError("session 未初始化")
        now_ts = int(time.time())
        rows: List[dict] = []
        seen_ids: set = set()
        for page in range(1, LUOGU_CONTEST_MAX_PAGES + 1):
            url = (
                LUOGU_CONTEST_URL
                if page == 1
                else f"{LUOGU_CONTEST_URL}?page={page}"
            )
            text = await fetch_text_with_retry(self.session, url)
            match = LENTILLE_RE.search(text)
            if not match:
                raise ValueError("未找到 lentille-context 数据")
            payload = json.loads(match.group(1))
            listing = payload["data"]["contests"]
            page_rows = listing.get("result") or []
            per_page = int(listing.get("perPage") or LUOGU_CONTEST_PAGE_SIZE)
            for item in page_rows:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("id") or "")
                if key and key in seen_ids:
                    continue
                if key:
                    seen_ids.add(key)
                rows.append(item)
            if len(page_rows) < max(1, per_page):
                break
            last_start = int(page_rows[-1].get("startTime") or 0) if page_rows else 0
            if last_start <= now_ts:
                break
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
        # 同一层 chunk 并行下载，避免逐个等待网络超时拖慢首次查询。
        while pending and len(visited) < MAX_OFFLINE_SCRIPTS:
            batch = []
            while (
                pending
                and len(batch) < OFFLINE_SCRIPT_BATCH_SIZE
                and len(visited) < MAX_OFFLINE_SCRIPTS
            ):
                script_url = pending.pop(0)
                if script_url in visited:
                    continue
                visited.add(script_url)
                batch.append(script_url)

            async def load_script(script_url: str):
                try:
                    return script_url, await fetch_text_with_retry(
                        self.session, script_url
                    ), None
                except Exception as exc:  # noqa: BLE001 -继续尝试其他脚本
                    return script_url, "", exc

            tasks = [asyncio.create_task(load_script(url)) for url in batch]
            try:
                # 赛程 chunk 找到后立即返回，不必等待统计脚本等无关资源的
                # 超时/重试；这对首次查询的响应速度影响很明显。
                for completed in asyncio.as_completed(tasks):
                    script_url, script, error = await completed
                    if error is not None:
                        last_error = error
                        continue
                    try:
                        rows = self._parse_xcpc_schedule(script, domain)
                        if rows:
                            rows.sort(key=lambda item: item.start_date)
                            return rows
                    except Exception as exc:  # noqa: BLE001 -继续检查其他 chunk
                        last_error = exc
                    for imported_url in self._xcpc_import_urls(script, script_url):
                        if imported_url not in visited and imported_url not in pending:
                            pending.append(imported_url)
            finally:
                unfinished = [task for task in tasks if not task.done()]
                for task in unfinished:
                    task.cancel()
                if unfinished:
                    await asyncio.gather(*unfinished, return_exceptions=True)
        if last_error:
            raise last_error
        raise ValueError("赛程脚本中未找到线下赛事数据")

    @staticmethod
    def _xcpc_import_urls(script: str, script_url: str):
        """只保留同源业务 chunk，跳过 Vercel 统计等无关脚本。"""
        script_origin = urlparse(script_url).netloc
        for imported in XCPC_SCRIPT_IMPORT_RE.findall(script):
            if imported.startswith("${"):
                continue
            # Vite 业务 chunk 使用 ./ 或 / 引用；裸的 assets/foo.js 在
            # /assets/index.js 下会被拼成 /assets/assets/foo.js，通常是无效
            # 路径，也会额外触发多次重试。
            if not imported.startswith((".", "/", "http://", "https://")):
                continue
            imported_url = urljoin(script_url, imported)
            parsed = urlparse(imported_url)
            if parsed.netloc and parsed.netloc != script_origin:
                continue
            if parsed.path.startswith("/_vercel/"):
                continue
            yield imported_url

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
