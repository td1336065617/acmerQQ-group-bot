"""近 7 日轻量活跃统计（A3 群训练周报）。

设计/实现文档：`docs/下一阶段功能设计.md` §A3、`docs/下一阶段功能实现文档.md` §A3.1。

只覆盖 Codeforces 与 AtCoder（两者都有轻量公开接口，1~2 请求/成员）：

- **Codeforces**：`user.status?handle=<h>&from=1&count=1000`，走
  `AccountFetcher._cf_json`（CF 限速锁已在该方法内），一次请求足够覆盖一周；
- **AtCoder**：kenkoooo `user/submissions?from_second=<ts>&user=<h>`
  （500 条/页，最多取 2 页）。

牛客/洛谷本次**不统计活跃**（没有等价的轻量接口，且详细分析缓存里的
`activity_daily` 只在含提交扫描的详细查询时产生，周报依赖它会非常贵），
对应成员返回 `source="unavailable"`，由上层在文案里如实标注。

结果按 `(platform, handle)` 缓存 6 小时（`WEEKLY_STATS_TTL`，多群共享）；
单个成员抓取失败只记 `logger.warning` 并跳过，不阻塞其他成员与整份周报。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

from astrbot.api import logger

from .models import CN_TZ

#: 周报统计窗口（天）
WEEKLY_WINDOW_DAYS = 7
#: 活跃统计结果缓存时长（秒）
WEEKLY_STATS_TTL = 6 * 3600
#: 支持"活跃统计"的平台；其余平台返回 source="unavailable"
ACTIVITY_PLATFORMS = ("codeforces", "atcoder")

CF_USER_STATUS_URL = "https://codeforces.com/api/user.status"
ATCODER_SUBMISSIONS_URL = (
    "https://kenkoooo.com/atcoder/atcoder-api/v3/user/submissions"
)

#: CF 单次取回的最大提交条数（一周内 1000 条足够覆盖绝大多数活跃成员）
CF_STATUS_COUNT = 1000
#: CF user.status 实测响应可达数百 KB，超时放宽到 30 秒
CF_STATUS_TIMEOUT = 30.0
#: kenkoooo 每页 500 条；只取最近 7 天，1~2 页足够
ATCODER_SUBMISSION_PAGE_SIZE = 500
ATCODER_MAX_PAGES = 2
#: 翻页间隔（秒），避免把 kenkoooo 打得太急
ATCODER_PAGE_INTERVAL = 0.5
#: 单群并发（CF 限速锁会自然串行化 CF 请求）
WEEKLY_FETCH_CONCURRENCY = 4
#: 进程内缓存条目上限，防止长时间运行无限增长
ACTIVITY_CACHE_MAX_ENTRIES = 512

#: source 取值：cf-user-status / atcoder-submissions / unavailable
SOURCE_UNAVAILABLE = "unavailable"


@dataclass
class WeeklyActivity:
    """一名成员在某个平台近 7 天的活跃度。"""

    platform: str
    user_id: str
    active_days: int = 0
    submissions: int = 0
    solved: int = 0
    #: 数据来源标记（cf-user-status / atcoder-submissions / unavailable）
    source: str = SOURCE_UNAVAILABLE


#: {(platform, handle): (写入时间, WeeklyActivity)}；user_id 由调用方填充
_ACTIVITY_CACHE: Dict[Tuple[str, str], Tuple[float, WeeklyActivity]] = {}


def clear_weekly_activity_cache() -> None:
    """清空进程内活跃统计缓存（测试与排障用）。"""
    _ACTIVITY_CACHE.clear()


def _cache_get(platform: str, handle: str) -> Optional[WeeklyActivity]:
    entry = _ACTIVITY_CACHE.get((platform, handle.casefold()))
    if entry is None:
        return None
    written_at, activity = entry
    if time.monotonic() - written_at >= WEEKLY_STATS_TTL:
        _ACTIVITY_CACHE.pop((platform, handle.casefold()), None)
        return None
    return replace(activity)


def _cache_put(platform: str, handle: str, activity: WeeklyActivity) -> None:
    if len(_ACTIVITY_CACHE) >= ACTIVITY_CACHE_MAX_ENTRIES:
        # 简单淘汰：丢掉最早写入的一批，避免缓存无限增长。
        oldest = sorted(_ACTIVITY_CACHE.items(), key=lambda item: item[1][0])
        for key, _value in oldest[: max(1, ACTIVITY_CACHE_MAX_ENTRIES // 4)]:
            _ACTIVITY_CACHE.pop(key, None)
    _ACTIVITY_CACHE[(platform, handle.casefold())] = (
        time.monotonic(),
        replace(activity, user_id=""),
    )


def _as_int(value: object) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _member_fields(member: object) -> Optional[Tuple[str, str, str]]:
    """把成员项归一成 ``(user_id, display_name, handle)``；无效项返回 None。"""
    if not isinstance(member, (tuple, list)):
        return None
    if len(member) >= 3:
        user_id, display, handle = member[0], member[1], member[2]
    elif len(member) == 2:
        user_id, display, handle = member[0], member[0], member[1]
    else:
        return None
    handle_text = str(handle or "").strip()
    if not handle_text:
        return None
    user_text = str(user_id or "").strip() or handle_text
    return user_text, str(display or handle_text).strip(), handle_text


def _cf_problem_key(problem: dict) -> str:
    """CF 题目去重键：与 account_fetcher 的难度统计保持同一套口径（BUG-050）。

    题库题（acmsguru 等）没有 contestId、只有 problemsetName，旧实现会把
    「同 index 的不同题库题」折叠成一条 → 周报「通过题数」少算。
    """
    contest_id = problem.get("contestId")
    index = problem.get("index")
    problemset_name = problem.get("problemsetName")
    if contest_id and index:
        return f"contest:{contest_id}:{index}"
    if problemset_name and index:
        return f"set:{problemset_name}:{index}"
    name = str(problem.get("name") or "").strip()
    if name:
        return f"name:{name}"
    return "row:unknown"


def summarize_cf_rows(rows: Iterable[object], since_ts: float) -> WeeklyActivity:
    """把 CF ``user.status`` 的提交行汇总成近 7 天活跃度（纯函数，便于单测）。"""
    days = set()
    submissions = 0
    solved = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        timestamp = _as_int(row.get("creationTimeSeconds"))
        if timestamp is None or timestamp < since_ts:
            continue
        submissions += 1
        days.add(datetime.fromtimestamp(timestamp, tz=CN_TZ).date())
        if str(row.get("verdict") or "").strip().upper() != "OK":
            continue
        problem = row.get("problem")
        if isinstance(problem, dict):
            solved.add(_cf_problem_key(problem))
    return WeeklyActivity(
        platform="codeforces",
        user_id="",
        active_days=len(days),
        submissions=submissions,
        solved=len(solved),
        source="cf-user-status",
    )


def summarize_atcoder_rows(
    rows: Iterable[object], since_ts: float
) -> WeeklyActivity:
    """把 kenkoooo 提交行汇总成近 7 天活跃度（纯函数，便于单测）。"""
    days = set()
    submissions = 0
    solved = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        timestamp = _as_int(row.get("epoch_second"))
        if timestamp is None or timestamp < since_ts:
            continue
        submissions += 1
        days.add(datetime.fromtimestamp(timestamp, tz=CN_TZ).date())
        if str(row.get("result") or "").strip().upper() != "AC":
            continue
        problem_id = str(row.get("problem_id") or "").strip()
        if problem_id:
            solved.add(problem_id)
    return WeeklyActivity(
        platform="atcoder",
        user_id="",
        active_days=len(days),
        submissions=submissions,
        solved=len(solved),
        source="atcoder-submissions",
    )


async def _fetch_cf_activity(
    fetcher, handle: str, since_ts: float
) -> WeeklyActivity:
    """取单个 CF handle 的近 7 天活跃度（1 次请求）。"""
    fetch_json = getattr(fetcher, "_cf_json", None)
    if not callable(fetch_json):
        raise RuntimeError("账号抓取器不支持 Codeforces 提交接口")
    payload = await fetch_json(
        "user.status",
        {"handle": handle, "from": "1", "count": str(CF_STATUS_COUNT)},
        timeout=CF_STATUS_TIMEOUT,
    )
    if not isinstance(payload, dict):
        raise ValueError("Codeforces 返回的数据格式异常")
    if str(payload.get("status") or "") != "OK":
        raise ValueError(str(payload.get("comment") or "Codeforces 接口返回失败"))
    rows = payload.get("result")
    if not isinstance(rows, list):
        raise ValueError("Codeforces 提交列表缺失")
    return summarize_cf_rows(rows, since_ts)


async def _fetch_atcoder_activity(
    fetcher, handle: str, since_ts: float
) -> WeeklyActivity:
    """取单个 AtCoder handle 的近 7 天活跃度（1~2 页）。"""
    fetch_json = getattr(fetcher, "_fetch_json", None)
    if not callable(fetch_json):
        raise RuntimeError("账号抓取器不支持 AtCoder 提交接口")
    collected: List[object] = []
    from_second = max(0, int(since_ts))
    for _page in range(ATCODER_MAX_PAGES):
        params = urlencode(
            {"user": handle, "from_second": str(from_second)}
        )
        data = await fetch_json(f"{ATCODER_SUBMISSIONS_URL}?{params}")
        if not isinstance(data, list):
            raise ValueError("AtCoder Problems 返回的数据格式异常")
        collected.extend(data)
        if len(data) < ATCODER_SUBMISSION_PAGE_SIZE:
            break
        timestamps = [
            _as_int(item.get("epoch_second"))
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
        await asyncio.sleep(ATCODER_PAGE_INTERVAL)
    return summarize_atcoder_rows(collected, since_ts)


async def collect_weekly_activity(
    plugin,
    platform: str,
    members: Sequence[object],
    since_ts: float,
) -> Dict[str, WeeklyActivity]:
    """采集本群成员在 ``platform`` 上近 7 天的活跃度。

    - 返回 ``{user_id: WeeklyActivity}``；抓取失败的成员**不会**出现在结果里；
    - 牛客/洛谷等不支持的平台返回 ``source="unavailable"`` 的占位记录，
      由上层决定文案（本模块不臆造 0 活跃）；
    - 结果按 ``(platform, handle)`` 缓存 ``WEEKLY_STATS_TTL`` 秒（多群共享）。
    """
    normalized = [
        item
        for item in (_member_fields(member) for member in members or [])
        if item is not None
    ]
    result: Dict[str, WeeklyActivity] = {}
    if not normalized:
        return result

    if platform not in ACTIVITY_PLATFORMS:
        for user_id, _display, _handle in normalized:
            result[user_id] = WeeklyActivity(
                platform=platform, user_id=user_id, source=SOURCE_UNAVAILABLE
            )
        return result

    fetcher = getattr(plugin, "account_fetcher", None)
    if fetcher is None:
        logger.warning("周报活跃统计不可用：账号抓取器未初始化（%s）", platform)
        for user_id, _display, _handle in normalized:
            result[user_id] = WeeklyActivity(
                platform=platform, user_id=user_id, source=SOURCE_UNAVAILABLE
            )
        return result

    semaphore = asyncio.Semaphore(WEEKLY_FETCH_CONCURRENCY)

    async def fetch_one(
        item: Tuple[str, str, str]
    ) -> Tuple[str, Optional[WeeklyActivity]]:
        user_id, _display, handle = item
        cached = _cache_get(platform, handle)
        if cached is not None:
            return user_id, replace(cached, user_id=user_id)
        async with semaphore:
            try:
                if platform == "codeforces":
                    activity = await _fetch_cf_activity(
                        fetcher, handle, since_ts
                    )
                else:
                    activity = await _fetch_atcoder_activity(
                        fetcher, handle, since_ts
                    )
            except Exception as exc:  # noqa: BLE001 - 单成员失败不阻塞整体
                logger.warning(
                    "周报活跃统计失败（%s %s）：%s", platform, handle, exc
                )
                return user_id, None
        _cache_put(platform, handle, activity)
        return user_id, replace(activity, user_id=user_id)

    for user_id, activity in await asyncio.gather(
        *(fetch_one(item) for item in normalized)
    ):
        if activity is not None:
            result[user_id] = activity
    return result
