"""赛后赛果采集：把"本群成员在某场比赛的名次"取回来。

设计/实现文档：`docs/下一阶段功能设计.md` §A1、`docs/下一阶段功能实现文档.md` §2 A1。

四个平台的数据源（均为公开、无需登录，实测于 2026-09-18）：

- **Codeforces**：`contest.standings`（**只能带 contestId 一个参数**），
  行内含 `party/rank/problemResults`；`problemResults[].type == PRELIMINARY`
  表示系统重测未完成，名次可能微调。
- **AtCoder**：`contests/<slug>/results/json`，行内含 `Place`；该平台不提供
  题目级结果（`standings` 需登录），因此不显示"通过题数"。
- **牛客**：个人参赛记录 `/acm-heavy/acm/contest/profile/contest-joined-history`
  （**必须带 /acm-heavy/ 前缀**，否则返回的 dataList 不含 rank/ac 字段），
  按 `contestId` 精确匹配；名次与通过题数与评分**解耦**（未评分的比赛同样有）。
- **洛谷**：无公开榜单（计分板接口需登录），只能判断"是否参赛"。

本模块只做"取数与匹配"，不负责推送与渲染；失败一律返回 None 并记日志，
由调用方按"未就绪 → 下一 tick 重试"处理。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

from astrbot.api import logger

from .models import Contest
from .plugin_paths import plugin_cache_root

SETTLE_PLATFORMS = ("codeforces", "atcoder", "nowcoder", "luogu")
#: 卡片每平台最多展示的行数（与渲染层一致）
SETTLE_MAX_ROWS = 10
#: 赛果结果的进程内缓存时长（多群共享，官方数据变化很慢）
SETTLE_RESULT_TTL = 6 * 3600
#: 牛客个人参赛记录的缓存时长（列表本身变化缓慢）
NOWCODER_HISTORY_TTL = 30 * 60
#: 单成员历史记录翻页数（最新在前，第 1 页足够覆盖刚结束的比赛）
NOWCODER_HISTORY_PAGES = 1
#: 牛客逐成员请求的并发
NOWCODER_FETCH_CONCURRENCY = 8
#: CF standings 实测单场 248KB / 约 10 秒，需要比默认 10 秒更宽的超时
CF_STANDINGS_TIMEOUT = 30.0
#: AtCoder results.json 实测约 300KB
ATCODER_RESULTS_TIMEOUT = 30.0
#: 缓存条目上限（防止长时间运行无限增长）
CACHE_MAX_ENTRIES = 128
#: 最近比赛记录：赛程接口只返回"未开始"的比赛，比赛一结束就从列表消失，
#: 因此必须把见过的比赛记下来，等它结束后再结算。保留 48 小时：严格完整门禁下
#: CF 最长等待 24 小时，比赛记录必须晚于门禁窗口才被淘汰。
RECENT_CONTEST_KEEP_SECONDS = 48 * 3600
RECENT_CONTEST_FILE = "settle_recent.json"
RECENT_CONTEST_MAX_ENTRIES = 400

CF_API_METHOD = "contest.standings"
ATCODER_RESULTS_URL = "https://atcoder.jp/contests/{slug}/results/json"
NOWCODER_HEAVY_HISTORY_URL = (
    "https://ac.nowcoder.com/acm-heavy/acm/contest/profile/contest-joined-history"
)
NOWCODER_PROFILE_REFERER = "https://ac.nowcoder.com/acm/contest/profile/{uid}"
#: 牛客日历里的 contestId 是"日历行 ID"，真实比赛 ID 在链接里
#: （实测：日历 contestId=1139935，链接 /acm/contest/139935）。
NOWCODER_CONTEST_ID_RE = re.compile(r"/acm/contest/(\d+)")

# ----------------------------------------------------------------------
# 严格完整门禁（settle strict）：只在平台侧"结算完成"后才允许推送
# 设计文档：docs/赛果卡严格完整门禁-设计文档.md
# ----------------------------------------------------------------------
#: 轮询间隔（分钟，按序推进，最后一档固定循环）
PROBE_INTERVALS: Dict[str, Tuple[int, ...]] = {
    "codeforces": (5, 5, 10, 10, 15, 20, 30, 60, 120, 180),
    "atcoder": (3, 3, 5, 5, 10, 15, 20, 30, 60, 120),
    "nowcoder": (10, 15, 20, 30, 60, 120),
    "luogu": (10, 15, 30, 60, 120),
}
#: 最长等待（分钟）：超过即放弃本场推送（改为群内"数据结算异常"提示）
MAX_WAIT_MINUTES: Dict[str, int] = {
    "codeforces": 1440,
    "atcoder": 720,
    "nowcoder": 720,
    "luogu": 360,
}
#: 最小等待（分钟）：仅作下限，不代表"完成期望"；CF 以 phase 为准故为 0
MIN_AGE_MINUTES: Dict[str, int] = {
    "codeforces": 0,
    "atcoder": 45,
    "nowcoder": 0,
    "luogu": 15,
}
#: 采样"稳定窗口"（分钟）：指纹必须保持不变的时长。
#: 生产实测：ABC477 在 T+37min 仍是 3594/11547 行的"自洽半截数据"，
#: 只靠"连续两次一致"会误判，因此必须加上时间跨度要求。
STABLE_SPAN_MINUTES: Dict[str, int] = {
    "codeforces": 0,
    "atcoder": 30,
    "nowcoder": 15,
    "luogu": 15,
}
#: 判定"稳定"所需的连续相同采样次数
STABLE_SAMPLES_DEFAULT = 2
#: 每 tick 最多发起几次探测（防止多场同时轮询打满网络）
MAX_PROBES_PER_TICK = 3
#: 探测结果短缓存（只用于同一 tick 内多群复用，绝不参与完整性判定）
SETTLE_PROBE_TTL = 30
#: 采样轨迹保留条数（防 KV 键膨胀）
POLL_SAMPLE_KEEP = 20
#: READY 状态的复评间隔（分钟）：已就绪但本群没内容可推（例如无人参赛）时，
#: 不再每 tick 复评——否则会重复探测（CF 榜单 307KB/次、约 36MB/小时）并刷日志
#: （生产实测 2026-09-27：12:00-22:18 产生 1617 行「已结算完成」）。
READY_RECHECK_MINUTES = 30
#: 结算候选窗口（小时）：严格门禁下按平台区分
SETTLE_WINDOW_HOURS: Dict[str, float] = {
    "codeforces": 24.0,
    "atcoder": 12.0,
    "nowcoder": 12.0,
    "luogu": 6.0,
}


def _as_int(value: object) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Sample:
    """一次探测的采样结果：严格完整门禁的判定输入。

    rows        ：榜单行数（AtCoder）/ 命中集合大小（牛客、洛谷）
    fingerprint ：内容指纹（判定"两次采样是否一致"）
    hint_ready  ：平台自带的完成信号（CF：phase==FINISHED 且无 PRELIMINARY）
    payload     ：原始数据，仅就绪后交给采集/渲染使用，不写入轮询状态
    """

    rows: int = 0
    fingerprint: str = ""
    hint_ready: Optional[bool] = None
    payload: Any = None

    def brief(self) -> Dict[str, Any]:
        """采样轨迹（写进轮询状态的精简结构）。"""
        return {
            "ts": int(time.time()),
            "rows": int(self.rows),
            "fp": str(self.fingerprint)[:64],
        }


def new_poll_state(now: float) -> Dict[str, Any]:
    """新建一场比赛的轮询状态。"""
    return {
        "state": "POLLING",
        "attempts": 0,
        "samples": [],
        "first_seen_at": float(now),
        "next_poll_at": 0.0,
        "stable_count": 0,
        "stable_since": None,
        "last_fp": "",
        "ready_at": None,
        "elapsed_minutes": None,
        "abandoned_reason": None,
    }


def poll_key(platform: str, contest_id: str, group_id: Optional[str] = None) -> str:
    """轮询状态键：CF/AtCoder 采样与群无关（跨群共享）；牛客/洛谷按群隔离。"""
    platform, contest_id = str(platform), str(contest_id)
    if platform in ("nowcoder", "luogu") and group_id:
        return f"settle_poll_{group_id}_{platform}_{contest_id}"
    return f"settle_poll_{platform}_{contest_id}"


def evaluate_readiness(
    platform: str,
    sample: Sample,
    state: Dict[str, Any],
    elapsed_minutes: float,
    *,
    stable_samples: int = STABLE_SAMPLES_DEFAULT,
    stable_span_minutes: Optional[float] = None,
    min_age_minutes: Optional[float] = None,
    now_ts: Optional[float] = None,
) -> Tuple[bool, str]:
    """判定"平台侧是否已结算完成"；就地更新 state 的稳定计数与稳定起点。

    - Codeforces：以 phase / 重测标记为准（hint_ready）
    - AtCoder / 牛客 / 洛谷：需要在**稳定窗口**内保持指纹不变，且满足最小等待：
      仅"连续两次一致"不足以判定——生产实测出现过自洽的半截数据
      （ABC477 T+37min 仍是 3594/11547 行）。
    """
    if platform == "codeforces":
        if sample.hint_ready is not True:
            state["stable_count"] = 0
            state["stable_since"] = None
            return False, "cf:phase-not-finished"
        state["stable_count"] = max(1, int(state.get("stable_count") or 0))
        return True, "cf:finished"
    moment = float(now_ts) if now_ts is not None else None
    same = bool(sample.fingerprint) and state.get("last_fp") == sample.fingerprint
    if same:
        state["stable_count"] = int(state.get("stable_count") or 0) + 1
    else:
        state["stable_count"] = 0
        state["stable_since"] = moment
    state["last_fp"] = sample.fingerprint
    needed = max(0, int(stable_samples) - 1)
    min_age = (
        float(min_age_minutes)
        if min_age_minutes is not None
        else float(MIN_AGE_MINUTES.get(str(platform), 0))
    )
    span_need = (
        float(stable_span_minutes)
        if stable_span_minutes is not None
        else float(STABLE_SPAN_MINUTES.get(str(platform), 0))
    )
    span_ok = True
    since = state.get("stable_since")
    if span_need > 0:
        if moment is None or since is None:
            span_ok = False
        else:
            span_ok = (moment - float(since)) >= span_need * 60
    if (
        state["stable_count"] >= needed
        and float(elapsed_minutes) >= min_age
        and span_ok
    ):
        return True, f"{platform}:stable"
    return False, (
        f"{platform}:unstable(rows={sample.rows}, stable={state['stable_count']},"
        f" span_ok={span_ok})"
    )


def next_poll_delay_minutes(
    platform: str,
    attempts: int,
    *,
    night: bool = False,
    night_scale: float = 1.0,
    max_delay_minutes: Optional[float] = None,
) -> float:
    """按下标取轮询间隔（超出序列则固定最后一档），夜间可整体放大。

    max_delay_minutes：未就绪期间的上限，必须 ≤ 稳定窗口，否则「看起来 30 分钟没变」
    可能只由两次相隔很远的采样支撑，稳定性不可信。
    """
    seq = PROBE_INTERVALS.get(str(platform)) or (10,)
    index = max(0, min(int(attempts), len(seq) - 1))
    delay = float(seq[index])
    if night:
        delay *= max(1.0, float(night_scale))
    if max_delay_minutes is not None:
        delay = min(delay, float(max_delay_minutes))
    return delay


@dataclass
class SettleRow:
    """一名成员在某场比赛的赛果。"""

    platform: str
    user_id: str
    display_name: str
    handle: str
    rank: Optional[int] = None
    user_count: Optional[int] = None
    solved: Optional[int] = None
    total_problems: Optional[int] = None
    #: 仅 CF：本场未通过的题目编号（如 ["D", "F"]）
    unsolved: List[str] = field(default_factory=list)
    #: 数据来源标记，用于卡片底部说明口径
    source: str = ""
    #: 打星/榜外成员：有实际参赛与成绩，但不在官方榜单（无名次）
    unofficial: bool = False

    def to_card_row(self) -> Dict[str, Any]:
        """转成渲染层使用的行结构（见实现文档 §A1.2）。"""
        total = self.total_problems or 0
        return {
            "rank": self.rank,
            "user_count": self.user_count,
            "unofficial": bool(self.unofficial),
            "display_name": self.display_name,
            "handle": self.handle,
            "solved": self.solved,
            "total_problems": self.total_problems,
            "ak": bool(self.solved is not None and total and self.solved >= total),
            "unsolved": list(self.unsolved),
            "source": self.source,
            "platform": self.platform,
        }


@dataclass
class SettleResult:
    """一场比赛在本群的赛果。rows 为空但有 extra_note 时仍可推送（洛谷场景）。"""

    platform: str
    contest_id: str
    contest_name: str
    rows: List[SettleRow] = field(default_factory=list)
    note: str = ""
    extra_note: str = ""

    def has_content(self) -> bool:
        return bool(self.rows or self.extra_note)


class SettlementService:
    """赛果采集服务；由插件持有一个实例，多群共享缓存。"""

    def __init__(self, account_fetcher) -> None:
        self.fetcher = account_fetcher
        #: 结果缓存：键含"成员指纹"，否则 A 群算出的结果会被 B 群复用
        #: （线上真实故障：5 个群都推了主群那一个人的赛果）。
        self._result_cache: Dict[Tuple[str, str, str], Tuple[float, SettleResult]] = {}
        #: 原始数据缓存（榜单等与"哪些群"无关的数据，跨群共享，避免每个群重复拉取）。
        #: 严格门禁下它只服务"已就绪"的最终数据；探测样本必须绕过它（见 probe()）。
        self._raw_cache: Dict[Tuple[str, str], Tuple[float, Any]] = {}
        #: 探测短缓存（30 秒）：只用于同一 tick 内多群复用，绝不参与完整性判定。
        #: 键含作用域：牛客/洛谷的样本依赖"本群成员"，必须按群隔离，否则会串群。
        self._probe_cache: Dict[Tuple[str, str, str], Tuple[float, Sample]] = {}
        self._nowcoder_history: Dict[str, Tuple[float, List[dict]]] = {}
        self._locks: Dict[Tuple[str, str], asyncio.Lock] = {}
        #: {(platform, contest_id): {...}}：赛程接口只给未开始的比赛，
        #: 结束后会从列表消失，因此必须自己记住见过的比赛。
        self._recent: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._recent_dirty = False

    # ------------------------------------------------------------------
    # 最近比赛记录（结算候选的唯一来源）
    # ------------------------------------------------------------------
    def remember_contests(self, platform: str, contests, *, now: Optional[float] = None) -> None:
        """把"见过的比赛"记入最近列表（赛程接口只给未开始的比赛，结束后会消失）。

        每个 tick 都会把当前赛程与上一次缓存里的赛程一起记入，因此
        "比赛结束前最后一次抓取"与"结束后第一次抓取"都能覆盖到。
        """
        moment = time.time() if now is None else now
        changed = False
        for contest in contests or []:
            contest_id = str(getattr(contest, "contest_id", "") or "").strip()
            if not contest_id:
                continue
            end = getattr(contest, "end_time", None)
            start = getattr(contest, "start_time", None)
            duration = int(getattr(contest, "duration_minutes", 0) or 0)
            if end is None and start is not None and duration > 0:
                end = start + timedelta(minutes=duration)
            if end is None:
                continue
            key = (str(platform), contest_id)
            payload = {
                "platform": str(platform),
                "contest_id": contest_id,
                "name": str(getattr(contest, "name", "") or ""),
                "url": str(getattr(contest, "url", "") or ""),
                "start_time": start.timestamp() if isinstance(start, datetime) else None,
                "end_time": end.timestamp() if isinstance(end, datetime) else None,
                "duration_minutes": duration,
                "seen_at": moment,
            }
            if payload["end_time"] is None:
                continue
            previous = self._recent.get(key)
            if previous is None or previous.get("seen_at") != moment:
                self._recent[key] = payload
                changed = True
        if self._prune_recent(moment):
            changed = True
        if changed:
            self._recent_dirty = True

    def _prune_recent(self, moment: float) -> bool:
        before = len(self._recent)
        self._recent = {
            key: value
            for key, value in self._recent.items()
            if moment - float(value.get("end_time") or 0) <= RECENT_CONTEST_KEEP_SECONDS
        }
        if len(self._recent) > RECENT_CONTEST_MAX_ENTRIES:
            newest = sorted(
                self._recent.items(),
                key=lambda pair: float(pair[1].get("end_time") or 0),
                reverse=True,
            )[:RECENT_CONTEST_MAX_ENTRIES]
            self._recent = dict(newest)
        return len(self._recent) != before

    def settlement_candidates(
        self,
        platform: str,
        now: datetime,
        delay_minutes: int,
        *,
        window_hours: Optional[float] = None,
    ) -> List[Contest]:
        """从最近记录里挑出"刚结束、仍在补推窗口内"的比赛。

        严格完整门禁下窗口按平台取值（CF 24h / AtCoder 12h / 牛客 12h / 洛谷 6h），
        否则 CF 的系统重测还没结束，比赛记录就先被淘汰了。
        """
        if window_hours is None:
            window_hours = SETTLE_WINDOW_HOURS.get(str(platform), 2.0)
        moment = now.timestamp() if isinstance(now, datetime) else float(now)
        out: List[Contest] = []
        for (item_platform, _contest_id), payload in self._recent.items():
            if item_platform != str(platform):
                continue
            end_ts = float(payload.get("end_time") or 0)
            if not end_ts:
                continue
            elapsed = moment - end_ts
            if elapsed < delay_minutes * 60 or elapsed > window_hours * 3600:
                continue
            start_ts = payload.get("start_time")
            out.append(
                Contest(
                    platform=str(platform),
                    name=str(payload.get("name") or ""),
                    start_time=datetime.fromtimestamp(
                        float(start_ts) if start_ts else end_ts, tz=timezone.utc
                    ),
                    end_time=datetime.fromtimestamp(end_ts, tz=timezone.utc),
                    duration_minutes=int(payload.get("duration_minutes") or 0),
                    url=str(payload.get("url") or ""),
                    contest_id=str(payload.get("contest_id") or ""),
                )
            )
        out.sort(key=lambda contest: contest.end_time or contest.start_time)
        return out

    def load_recent_contests(self, path: Optional[Path] = None) -> int:
        """从磁盘加载最近比赛记录（重启后仍能结算"结束前已见过"的比赛）。"""
        target = Path(path) if path else self.recent_path()
        try:
            if not target.is_file():
                return 0
            with target.open("r", encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("读取最近比赛记录失败：%s", exc)
            return 0
        items = payload.get("contests") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return 0
        moment = time.time()
        for item in items:
            if not isinstance(item, dict):
                continue
            contest_id = str(item.get("contest_id") or "")
            platform = str(item.get("platform") or "")
            if not contest_id or not platform:
                continue
            self._recent[(platform, contest_id)] = item
        self._prune_recent(moment)
        return len(self._recent)

    def save_recent_contests(self, path: Optional[Path] = None) -> None:
        """原子写入最近比赛记录（只在有变化时写）。"""
        if not self._recent_dirty:
            return
        target = Path(path) if path else self.recent_path()
        payload = {
            "version": 1,
            "saved_at": time.time(),
            "contests": list(self._recent.values()),
        }
        temp_path = target.with_name(f".{target.name}.tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with temp_path.open("w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, separators=(",", ":"))
            os.replace(temp_path, target)
            self._recent_dirty = False
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("写入最近比赛记录失败：%s", exc)
            try:
                temp_path.unlink()
            except OSError:
                pass

    def recent_path(self) -> Path:
        """最近比赛记录落盘位置：与题库索引同目录。"""
        fetcher = getattr(self, "fetcher", None)
        if fetcher is not None and hasattr(fetcher, "index_path"):
            try:
                return fetcher.index_path().parent / RECENT_CONTEST_FILE
            except Exception:  # noqa: BLE001 - 回退到插件 data 目录
                pass
        return plugin_cache_root() / RECENT_CONTEST_FILE

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    async def collect(
        self,
        platform: str,
        contest,
        members: Sequence[Tuple[str, str, str]],
        *,
        include_unofficial: bool = True,
    ) -> Optional[SettleResult]:
        """采集本群成员在该场比赛的赛果。

        Args:
            platform: 平台标识（SETTLE_PLATFORMS 之一）。
            contest: `src.models.Contest`，需要 `contest_id/name/start_time/end_time`。
            members: `[(user_id, display_name, handle), ...]`，已按平台过滤。

        Returns:
            SettleResult；无人参赛或数据尚未就绪时返回 None（调用方据此重试）。
        """
        if platform not in SETTLE_PLATFORMS or not members:
            return None
        contest_id = str(getattr(contest, "contest_id", "") or "").strip()
        if not contest_id:
            return None

        cache_key = (platform, contest_id, self._member_fingerprint(members))
        cached = self._result_cache.get(cache_key)
        if cached is not None and time.time() - cached[0] < SETTLE_RESULT_TTL:
            return cached[1]

        lock = self._locks.get(cache_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[cache_key] = lock
        async with lock:
            cached = self._result_cache.get(cache_key)
            if cached is not None and time.time() - cached[0] < SETTLE_RESULT_TTL:
                return cached[1]
            try:
                if platform == "codeforces":
                    result = await self._collect_codeforces(
                        contest, members, include_unofficial=include_unofficial
                    )
                elif platform == "atcoder":
                    result = await self._collect_atcoder(contest, members)
                elif platform == "nowcoder":
                    result = await self._collect_nowcoder(contest, members)
                else:
                    result = await self._collect_luogu(contest, members)
            except Exception as exc:  # noqa: BLE001 - 赛果失败不能影响推送主流程
                logger.warning(
                    "赛果采集失败（%s %s）：%s", platform, contest_id, exc
                )
                return None
            finally:
                self._locks.pop(cache_key, None)

        # 只缓存"有内容"的结果（空结果不缓存，留给调用方按终局处理）；
        # 返回值必须原样透传：空结果（无人参赛）是终局信号，归一成 None
        # 会把它重新打回「未就绪重试」——1.21.2 实施时踩过这个接缝：
        # 采集层改对了，结果却被这里的缓存尾巴吞掉（生产断言抓获，已补 collect 级用例）。
        if result is not None and result.has_content():
            self._cache_put(cache_key, result)
        return result

    def _cache_put(self, key: Tuple[str, str], value: SettleResult) -> None:
        self._result_cache[key] = (time.time(), value)
        if len(self._result_cache) > CACHE_MAX_ENTRIES:
            newest = sorted(
                self._result_cache.items(),
                key=lambda pair: pair[1][0],
                reverse=True,
            )[: CACHE_MAX_ENTRIES // 2]
            self._result_cache = dict(newest)

    # ------------------------------------------------------------------
    # 严格门禁：探测（settle strict probe）
    # ------------------------------------------------------------------
    async def probe(
        self,
        platform: str,
        contest: Any,
        members: Sequence[Tuple[str, str, str]],
        *,
        force: bool = False,
        group_id: Optional[str] = None,
    ) -> Sample:
        """探测一次"平台侧是否已结算完成"（严格完整门禁的输入）。

        同一 tick 内多群复用 30 秒的 probe_cache；探测路径**不读 final_cache**，
        否则重测前后的数据会互相污染。牛客/洛谷的样本与"本群成员"有关，按群隔离。
        """
        platform = str(platform)
        cid = str(getattr(contest, "contest_id", "") or "")
        scope = str(group_id or "") if platform in ("nowcoder", "luogu") else ""
        key = (platform, cid, scope)
        cached = self._probe_cache.get(key)
        if cached is not None and not force and time.time() - cached[0] < SETTLE_PROBE_TTL:
            return cached[1]
        sample = await self._probe_platform(platform, contest, members)
        self._probe_cache[key] = (time.time(), sample)
        if len(self._probe_cache) > CACHE_MAX_ENTRIES:
            newest = sorted(
                self._probe_cache.items(), key=lambda pair: pair[1][0], reverse=True
            )[: CACHE_MAX_ENTRIES // 2]
            self._probe_cache = dict(newest)
        return sample

    async def _probe_platform(
        self, platform: str, contest: Any, members: Sequence[Tuple[str, str, str]]
    ) -> Sample:
        """按平台取一次样本：AtCoder 用榜单行数，CF 用 phase，牛客/洛谷用命中集合。"""
        cid = str(getattr(contest, "contest_id", "") or "").strip()
        if not cid:
            return Sample()
        if platform == "atcoder":
            index, total_rows = await self._atcoder_results_index(cid, fresh=True)
            if not index and not total_rows:
                return Sample()
            rows = int(total_rows or len(index))
            # payload 留作 final_cache 复用：避免"判定用一份、渲染再抓一份"
            return Sample(rows=rows, fingerprint=f"rows:{rows}", payload=(index, total_rows))
        if platform == "codeforces":
            data = await self._cf_standings_meta(cid, fresh=True)
            if not data["rows"]:
                return Sample(hint_ready=False)
            phase = str(data["phase"] or "")
            ready = bool(phase == "FINISHED" and not data["preliminary"])
            return Sample(
                rows=len(data["rows"]),
                fingerprint=f"rows:{len(data['rows'])}:{phase}",
                hint_ready=ready,
                payload=data,
            )
        if platform == "nowcoder":
            result = await self._collect_nowcoder(contest, members)
        else:
            result = await self._collect_luogu(contest, members)
        if result is None:
            return Sample(rows=0, fingerprint="empty")
        keys = sorted(
            str(row.handle or row.display_name).casefold() for row in result.rows
        ) or [str(result.extra_note or "")]
        digest = hashlib.sha1("|".join(keys).encode("utf-8")).hexdigest()[:12]
        return Sample(rows=len(keys), fingerprint=f"set:{digest}", payload=result)

    def adopt_probe(self, platform: str, contest_id: str, sample: Sample) -> None:
        """就绪后把探测拿到的数据写进 final_cache。

        好处：① 省掉一次重复抓取（AtCoder ~460KB / CF ~307KB）；
        ② 保证"被判定为完整的那份数据"就是渲染用的那份，不会中途换成新样本。
        """
        payload = getattr(sample, "payload", None)
        if payload is None:
            return
        cid = str(contest_id)
        if platform == "atcoder":
            index, total_rows = payload
            if index:
                self._raw_cache[("atcoder", cid)] = (time.time(), (index, total_rows))
        elif platform == "codeforces":
            self._raw_cache[("codeforces", cid)] = (time.time(), payload)

    # ------------------------------------------------------------------
    # Codeforces
    # ------------------------------------------------------------------
    async def _cf_standings_meta(
        self, contest_id: str, *, fresh: bool = False
    ) -> Dict[str, Any]:
        """CF 榜单原始数据 + 结算状态（phase / 是否仍有 PRELIMINARY）。

        fresh=True 供严格门禁探测使用：**必须绕过 6 小时缓存**，
        否则重测前后两份数据会互相污染。
        """
        key = ("codeforces", str(contest_id))
        cached = None if fresh else self._raw_cache.get(key)
        if cached is not None and time.time() - cached[0] < SETTLE_RESULT_TTL:
            return cached[1]
        # 注意：CF 对非管理员只允许"匿名 GET 且不带任何额外参数"，
        # 传 from/count/showUnofficial 会直接报错。
        payload = await self.fetcher._cf_json(
            CF_API_METHOD,
            {"contestId": str(contest_id)},
            timeout=CF_STANDINGS_TIMEOUT,
        )
        if not isinstance(payload, dict) or payload.get("status") != "OK":
            comment = payload.get("comment") if isinstance(payload, dict) else payload
            raise ValueError(f"CF standings 返回异常：{comment}")
        result = payload.get("result") or {}
        rows = result.get("rows") or []
        contest_meta = result.get("contest") or {}
        preliminary = any(
            str(item.get("type") or "").upper() == "PRELIMINARY"
            for row in rows
            if isinstance(row, dict)
            for item in (row.get("problemResults") or [])
        )
        data: Dict[str, Any] = {
            "rows": rows,
            "problems": result.get("problems") or [],
            "phase": str(contest_meta.get("phase") or ""),
            "preliminary": preliminary,
            "name": str(contest_meta.get("name") or ""),
            "duration": _as_int(contest_meta.get("durationSeconds")) or 0,
        }
        if not fresh:
            self._raw_cache[key] = (time.time(), data)
            if len(self._raw_cache) > CACHE_MAX_ENTRIES:
                newest = sorted(
                    self._raw_cache.items(), key=lambda pair: pair[1][0], reverse=True
                )[: CACHE_MAX_ENTRIES // 2]
                self._raw_cache = dict(newest)
        return data

    async def _cf_rating_changes(self, contest_id: str) -> Dict[str, Dict[str, Any]]:
        """CF 计分参赛全员名单（handle → {old,new}）。

        contest.standings 在 Div2/Div3 会少于实际计分人数（实测 2269：7474 vs 10453），
        打星/榜外成员只能靠这份名单判断；拿不到时返回 {}（不影响正式行）。
        """
        key = ("codeforces", f"changes:{contest_id}")
        cached = self._raw_cache.get(key)
        if cached is not None and time.time() - cached[0] < SETTLE_RESULT_TTL:
            return cached[1]
        out: Dict[str, Dict[str, Any]] = {}
        try:
            payload = await self.fetcher._cf_json(
                "contest.ratingChanges",
                {"contestId": str(contest_id)},
                timeout=CF_STANDINGS_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - 名单不可用不该影响正式行
            logger.warning("CF 计分名单获取失败（%s）：%s", contest_id, exc)
            return {}
        if isinstance(payload, dict) and payload.get("status") == "OK":
            for item in payload.get("result") or []:
                if not isinstance(item, dict):
                    continue
                handle = str(item.get("handle") or "").strip().casefold()
                if handle:
                    out[handle] = {
                        "old": _as_int(item.get("oldRating")),
                        "new": _as_int(item.get("newRating")),
                    }
        if out:
            self._raw_cache[key] = (time.time(), out)
        return out

    async def _cf_status_solved(
        self, contest_id: str, duration_seconds: int
    ) -> Dict[str, int]:
        """比赛窗口内提交过的成员 → 通过题数（打星成员没有官方名次，用它补成绩）。

        窗口过滤 relativeTimeSeconds ≤ duration + 60，避免把赛后练习/虚拟参赛算进来。
        """
        key = ("codeforces", f"status:{contest_id}")
        cached = self._raw_cache.get(key)
        if cached is not None and time.time() - cached[0] < SETTLE_RESULT_TTL:
            return cached[1]
        try:
            payload = await self.fetcher._cf_json(
                "contest.status",
                {"contestId": str(contest_id)},
                timeout=CF_STANDINGS_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - 明细拿不到时打星行只显示名字
            logger.warning("CF 提交明细获取失败（%s）：%s", contest_id, exc)
            return {}
        solved: Dict[str, set] = {}
        if isinstance(payload, dict) and payload.get("status") == "OK":
            for item in payload.get("result") or []:
                if not isinstance(item, dict):
                    continue
                try:
                    rel = int(item.get("relativeTimeSeconds"))
                except (TypeError, ValueError):
                    continue
                if duration_seconds and rel > duration_seconds + 60:
                    continue
                author = item.get("author") or {}
                for member in author.get("members") or []:
                    if not isinstance(member, dict):
                        continue
                    handle = str(member.get("handle") or "").strip().casefold()
                    if not handle:
                        continue
                    solved.setdefault(handle, set())
                    if str(item.get("verdict") or "") == "OK":
                        index = str((item.get("problem") or {}).get("index") or "")
                        if index:
                            solved[handle].add(index)
        out = {handle: len(items) for handle, items in solved.items()}
        if out:
            self._raw_cache[key] = (time.time(), out)
        return out

    async def _cf_unofficial_rows(
        self,
        contest_id: str,
        problems: Sequence[dict],
        meta: Dict[str, Any],
        members: Sequence[Tuple[str, str, str]],
        official_handles: set,
    ) -> List[SettleRow]:
        """榜外/打星成员：在计分名单或比赛窗口内有提交，但不在官方榜单。"""
        pending = [
            (user_id, display, handle)
            for user_id, display, handle in members
            if str(handle or "").strip()
            and str(handle).casefold() not in official_handles
        ]
        if not pending:
            return []
        changes = await self._cf_rating_changes(contest_id)
        status = await self._cf_status_solved(
            contest_id, int(meta.get("duration") or 0)
        )
        out: List[SettleRow] = []
        for user_id, display, handle in pending:
            key = str(handle).casefold()
            if key not in changes and key not in status:
                continue
            out.append(
                SettleRow(
                    platform="codeforces",
                    user_id=str(user_id),
                    display_name=str(display or handle),
                    handle=str(handle),
                    rank=None,
                    user_count=None,
                    solved=status.get(key),
                    total_problems=len(problems),
                    source="cf-ratingChanges",
                    unofficial=True,
                )
            )
        return out

    def _cf_official_rows(
        self,
        raw_rows: Sequence[dict],
        problems: Sequence[dict],
        members: Sequence[Tuple[str, str, str]],
    ) -> List[SettleRow]:
        """官方榜单里的本群成员（名次/通过题数，逻辑与旧版一致）。"""
        lookup = {
            str(handle).casefold(): (str(user_id), str(display or handle), str(handle))
            for user_id, display, handle in members
            if handle
        }
        rows: List[SettleRow] = []
        for raw in raw_rows:
            if not isinstance(raw, dict):
                continue
            party = raw.get("party") or {}
            for member in party.get("members") or []:
                if not isinstance(member, dict):
                    continue
                handle = str(member.get("handle") or "")
                hit = lookup.get(handle.casefold())
                if hit is None:
                    continue
                problem_results = [
                    item
                    for item in (raw.get("problemResults") or [])
                    if isinstance(item, dict)
                ]
                solved = sum(
                    1 for item in problem_results if (item.get("points") or 0) > 0
                )
                unsolved: List[str] = []
                for index, item in enumerate(problem_results):
                    if (item.get("points") or 0) > 0:
                        continue
                    unsolved.append(self._problem_index(problems, index))
                rows.append(
                    SettleRow(
                        platform="codeforces",
                        user_id=hit[0],
                        display_name=hit[1],
                        handle=hit[2],
                        rank=_as_int(raw.get("rank")),
                        user_count=len(raw_rows),
                        solved=solved,
                        total_problems=len(problem_results) or len(problems),
                        unsolved=unsolved,
                        source="cf-standings",
                    )
                )
        return rows

    async def _collect_codeforces(
        self,
        contest,
        members: Sequence[Tuple[str, str, str]],
        *,
        include_unofficial: bool = True,
    ) -> Optional[SettleResult]:
        contest_id = str(contest.contest_id)
        meta = await self._cf_standings_meta(contest_id)
        raw_rows = meta["rows"]
        problems = meta["problems"]
        if not raw_rows:
            return None
        rows = self._cf_official_rows(raw_rows, problems, members)
        unofficial: List[SettleRow] = []
        if include_unofficial:
            unofficial = await self._cf_unofficial_rows(
                contest_id,
                problems,
                meta,
                members,
                {str(row.handle).casefold() for row in rows},
            )
        if not rows and not unofficial:
            # 榜已就绪但成员全不在 = 无人参赛（终局信号）：返回空结果交调用方
            # 写终局标记；raw 榜为空（910 行）仍返回 None 表示未就绪。
            return SettleResult(
                platform="codeforces",
                contest_id=contest_id,
                contest_name=str(getattr(contest, "name", "") or ""),
                rows=[],
                note="CF standings 已就绪，本群绑定成员均未参加该场比赛",
            )
        rows.extend(unofficial)
        # 正式行按名次在前；打星行统一置尾
        rows.sort(key=lambda row: (row.unofficial, row.rank is None, row.rank or 0))
        note = "数据源：Codeforces 官方 standings（已结算）"
        if unofficial:
            note += f" · 另有 {len(unofficial)} 人打星（榜外），不计官方名次"
        return SettleResult(
            platform="codeforces",
            contest_id=contest_id,
            contest_name=str(getattr(contest, "name", "") or ""),
            rows=rows[:SETTLE_MAX_ROWS],
            note=note,
        )

    @staticmethod
    def _problem_index(problems: Iterable[dict], index: int) -> str:
        items = list(problems)
        if 0 <= index < len(items) and isinstance(items[index], dict):
            label = str(items[index].get("index") or "").strip()
            if label:
                return label
        return str(index + 1)

    # ------------------------------------------------------------------
    # AtCoder
    # ------------------------------------------------------------------
    async def _collect_atcoder(
        self, contest, members: Sequence[Tuple[str, str, str]]
    ) -> Optional[SettleResult]:
        slug = str(getattr(contest, "contest_id", "") or "").strip()
        if not slug:
            return None
        index, total_rows = await self._atcoder_results_index(slug)
        if not index:
            return None

        rows: List[SettleRow] = []
        for user_id, display, handle in members:
            item = index.get(str(handle).strip().casefold())
            if item is None:
                continue
            rows.append(
                SettleRow(
                    platform="atcoder",
                    user_id=str(user_id),
                    display_name=str(display or handle),
                    handle=str(handle),
                    rank=_as_int(item.get("Place")),
                    user_count=total_rows or len(index),
                    solved=None,
                    total_problems=None,
                    source="atcoder-results",
                )
            )
        if not rows:
            # 榜已就绪但成员全不在结果里 = 无人参赛（终局信号）。
            # 不能折成 None——None 表示「数据未就绪、继续重试」，会每 30 秒刷一条
            # 误导性日志（2026-09-28 AGC078 实测连刷 9.6 小时，见实现文档）。
            return SettleResult(
                platform="atcoder",
                contest_id=slug,
                contest_name=str(getattr(contest, "name", "") or ""),
                rows=[],
                note="AtCoder results 已就绪，本群绑定成员均未参加该场比赛",
            )
        rows.sort(key=lambda row: (row.rank is None, row.rank or 0))
        return SettleResult(
            platform="atcoder",
            contest_id=slug,
            contest_name=str(getattr(contest, "name", "") or ""),
            rows=rows[:SETTLE_MAX_ROWS],
            note="数据源：AtCoder 官方 results（该平台不公开题目级结果）",
        )

    # ------------------------------------------------------------------
    # 牛客
    # ------------------------------------------------------------------
    @staticmethod
    def _member_fingerprint(members: Sequence[Tuple[str, str, str]]) -> str:
        """成员集合指纹：参与结果缓存键，避免跨群复用别群的赛果。"""
        keys = sorted(
            {
                str(handle).strip().casefold()
                for _user_id, _display, handle in members
                if handle
            }
        )
        return hashlib.sha1("|".join(keys).encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _nowcoder_contest_ids(contest) -> set:
        """牛客比赛的候选 ID 集合：日历行 ID + 链接里的真实比赛 ID。

        两者必须都试——个人参赛记录里的 contestId 是**真实**比赛 ID，
        而赛程缓存里存的是日历行 ID（实测两者不相等）。
        """
        ids = set()
        raw_id = str(getattr(contest, "contest_id", "") or "").strip()
        if raw_id:
            ids.add(raw_id)
        match = NOWCODER_CONTEST_ID_RE.search(str(getattr(contest, "url", "") or ""))
        if match:
            ids.add(match.group(1))
        return ids

    async def _collect_nowcoder(
        self, contest, members: Sequence[Tuple[str, str, str]]
    ) -> Optional[SettleResult]:
        contest_id = str(contest.contest_id)
        contest_ids = self._nowcoder_contest_ids(contest)
        semaphore = asyncio.Semaphore(NOWCODER_FETCH_CONCURRENCY)

        async def fetch_member(user_id: str, display: str, handle: str):
            async with semaphore:
                try:
                    history = await self._nowcoder_member_history(handle)
                except Exception as exc:  # noqa: BLE001 - 单成员失败不影响其他成员
                    logger.warning("牛客赛果：读取 %s 的参赛记录失败：%s", handle, exc)
                    return None
                return (user_id, display, handle, history)

        fetched = await asyncio.gather(
            *(fetch_member(*member) for member in members)
        )
        rows: List[SettleRow] = []
        for item in fetched:
            if item is None:
                continue
            user_id, display, handle, history = item
            row = self._pick_nowcoder_row(history, contest_ids)
            if row is None:
                continue
            accepted = _as_int(row.get("acceptedCount"))
            total = _as_int(row.get("problemCount"))
            # 组队参赛时把队名附在昵称后（与昵称相同则忽略，避免"张三（张三）"）
            team_name = str(row.get("teamName") or "").strip()
            if (
                row.get("isTeamSignUp")
                and team_name
                and team_name.casefold() not in {str(display).casefold(), str(handle).casefold()}
            ):
                display = f"{display}（{team_name}）"
            rows.append(
                SettleRow(
                    platform="nowcoder",
                    user_id=str(user_id),
                    display_name=str(display or handle),
                    handle=str(handle),
                    rank=_as_int(row.get("rank")),
                    user_count=_as_int(row.get("userCount")),
                    solved=accepted,
                    total_problems=total,
                    source="nowcoder-joined",
                )
            )
        if not rows:
            # 所有人都还没在个人参赛记录里看到这场比赛 → 视为"未就绪"，
            # 由调用方在时间窗口内重试（不写幂等键）。
            return None
        rows.sort(key=lambda row: (row.rank is None, row.rank or 0))
        return SettleResult(
            platform="nowcoder",
            contest_id=contest_id,
            contest_name=str(getattr(contest, "name", "") or ""),
            rows=rows[:SETTLE_MAX_ROWS],
            note="数据源：牛客个人参赛记录",
        )

    @staticmethod
    def _pick_nowcoder_row(history: List[dict], contest_ids) -> Optional[dict]:
        """按候选 ID 集合匹配个人参赛记录（兼容日历行 ID 与真实比赛 ID）。"""
        wanted = (
            {str(item) for item in contest_ids}
            if not isinstance(contest_ids, str)
            else {contest_ids}
        )
        if not wanted:
            return None
        for row in history:
            if not isinstance(row, dict):
                continue
            if str(row.get("contestId") or "") in wanted:
                return row
        return None

    async def _nowcoder_member_history(self, uid: str) -> List[dict]:
        """取某个牛客用户的比赛记录（已结束的，最新在前），带 30 分钟缓存。"""
        key = str(uid).strip()
        if not key:
            return []
        cached = self._nowcoder_history.get(key)
        if cached is not None and time.time() - cached[0] < NOWCODER_HISTORY_TTL:
            return cached[1]
        rows: List[dict] = []
        page = 1
        while page <= NOWCODER_HISTORY_PAGES:
            params = {
                "token": "",
                "uid": key,
                "page": str(page),
                "onlyJoinedFilter": "true",
                "searchContestName": "",
                "onlyRatingFilter": "false",
                "contestEndFilter": "true",
                "_": str(int(time.time() * 1000)),
            }
            text = await self.fetcher._fetch_text(
                f"{NOWCODER_HEAVY_HISTORY_URL}?{urlencode(params)}",
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": NOWCODER_PROFILE_REFERER.format(uid=key),
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
                timeout=20.0,
            )
            payload = json.loads(text)
            data = payload.get("data") if isinstance(payload, dict) else None
            page_rows = (data or {}).get("dataList") or []
            rows.extend(item for item in page_rows if isinstance(item, dict))
            page_info = (data or {}).get("pageInfo") or {}
            page_count = _as_int(page_info.get("pageCount"))
            if not page_rows or (page_count is not None and page >= page_count):
                break
            page += 1
        self._nowcoder_history[key] = (time.time(), rows)
        if len(self._nowcoder_history) > CACHE_MAX_ENTRIES:
            newest = sorted(
                self._nowcoder_history.items(),
                key=lambda pair: pair[1][0],
                reverse=True,
            )[: CACHE_MAX_ENTRIES // 2]
            self._nowcoder_history = dict(newest)
        return rows

    # ------------------------------------------------------------------
    # 洛谷
    # ------------------------------------------------------------------
    async def _atcoder_results_index(
        self, slug: str, *, fresh: bool = False
    ) -> Tuple[Dict[str, dict], int]:
        """AtCoder 官方 results 的"用户名 → 行"索引（跨群共享缓存）。

        fresh=True 供严格门禁探测使用：绕过 6 小时缓存，避免把"半截榜单"
        当成最终数据（生产实测：T+37min 只有 3594/11547 行）。
        """
        key = ("atcoder", str(slug))
        cached = None if fresh else self._raw_cache.get(key)
        if cached is not None and time.time() - cached[0] < SETTLE_RESULT_TTL:
            return cached[1]
        payload = await self.fetcher._fetch_json(
            ATCODER_RESULTS_URL.format(slug=str(slug)),
            timeout=ATCODER_RESULTS_TIMEOUT,
        )
        index: Dict[str, dict] = {}
        total_rows = 0
        if isinstance(payload, list):
            total_rows = len(payload)
            for item in payload:
                if not isinstance(item, dict):
                    continue
                for name_key in ("UserName", "UserScreenName"):
                    name = str(item.get(name_key) or "").strip()
                    if name:
                        index.setdefault(name.casefold(), item)
        if index and not fresh:
            self._raw_cache[key] = (time.time(), (index, total_rows))
        return index, total_rows

    async def _collect_luogu(
        self, contest, members: Sequence[Tuple[str, str, str]]
    ) -> Optional[SettleResult]:
        """洛谷无公开榜单：只判断"是否参赛"，把名单放进 extra_note。"""
        joined: List[str] = []
        for user_id, display, handle in members:
            try:
                profile = await self.fetcher.get_profile(
                    "luogu",
                    handle,
                    detail=True,
                    include_submissions=False,
                    include_difficulty=False,
                    include_analysis=False,
                )
            except Exception as exc:  # noqa: BLE001 - 单成员失败不影响其他成员
                logger.warning("洛谷赛果：读取 %s 失败：%s", handle, exc)
                continue
            if self._history_contains_contest(
                getattr(profile, "rating_history", None) or [], contest
            ):
                joined.append(str(display or handle))
        if not joined:
            return None
        return SettleResult(
            platform="luogu",
            contest_id=str(getattr(contest, "contest_id", "") or ""),
            contest_name=str(getattr(contest, "name", "") or ""),
            rows=[],
            note="洛谷不公开比赛名次",
            extra_note="洛谷参赛：" + "、".join(joined),
        )

    @staticmethod
    def _history_contains_contest(history: Iterable[dict], contest) -> bool:
        """个人历史里是否有这场比赛（按名称或时间窗匹配）。"""
        name = str(getattr(contest, "name", "") or "").strip()
        start = getattr(contest, "start_time", None)
        end = getattr(contest, "end_time", None)
        if end is None and start is not None:
            end = start + timedelta(
                minutes=int(getattr(contest, "duration_minutes", 0) or 0)
            )
        low = (start - timedelta(hours=1)) if isinstance(start, datetime) else None
        high = (end + timedelta(hours=6)) if isinstance(end, datetime) else None
        for row in history:
            if not isinstance(row, dict):
                continue
            row_name = str(row.get("name") or "").strip()
            if name and row_name and (
                name == row_name or name in row_name or row_name in name
            ):
                return True
            timestamp = row.get("timestamp")
            if low is None or high is None or timestamp is None:
                continue
            try:
                moment = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                continue
            if low <= moment <= high:
                return True
        return False
