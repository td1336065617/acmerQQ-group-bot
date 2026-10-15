"""ACMer AI 只读工具的纯逻辑（不 import astrbot，可直接单测）。"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Sequence

PLATFORM_LABELS = {
    "codeforces": "Codeforces",
    "nowcoder": "牛客",
    "luogu": "洛谷",
    "atcoder": "AtCoder",
    "offline": "线下赛",
}

#: 别名 → 真实平台键（线下赛的真实键是 offline，不是 xcpc）
PLATFORM_ALIASES = {
    "": "all",
    "all": "all",
    "全部": "all",
    "比赛": "all",
    "cf": "codeforces",
    "codeforces": "codeforces",
    "牛客": "nowcoder",
    "nk": "nowcoder",
    "nowcoder": "nowcoder",
    "洛谷": "luogu",
    "lg": "luogu",
    "luogu": "luogu",
    "atc": "atcoder",
    "atcoder": "atcoder",
    "offline": "offline",
    "xcpc": "offline",
    "线下": "offline",
    "线下赛": "offline",
}


def resolve_platform(value: Any, allowed: Sequence[str], default: str | None = None) -> str | None:
    """别名 → 平台键；不在 allowed 里则返回 default。"""
    key = str(value or "").strip().casefold()
    if not key:
        return default
    resolved = PLATFORM_ALIASES.get(key)
    if resolved is None:
        resolved = PLATFORM_ALIASES.get(str(value or "").strip())
    if resolved is None or resolved == "all":
        return default
    return resolved if resolved in allowed else default


def resolve_platforms(value: Any, allowed: Sequence[str], default: Sequence[str]) -> list:
    """all/空 → 全部可用平台；单个平台 → 只查它；未知 → default。"""
    key = str(value or "").strip().casefold()
    raw = PLATFORM_ALIASES.get(key) or PLATFORM_ALIASES.get(str(value or "").strip())
    if raw is None:
        return list(default)
    if raw == "all":
        return list(default)
    return [raw] if raw in allowed else list(default)


def select_upcoming(
    entries: Iterable[tuple],
    *,
    start_of: Callable[[Any], Any],
    now: float,
    days: int,
    limit: int,
    end_of: Callable[[Any], Any] | None = None,
) -> list:
    """从 (platform, contests, err) 里挑出未来 N 天内/进行中的比赛，按时间升序。"""
    picked: list = []
    deadline = float(now) + max(1, int(days)) * 86400
    for platform, contests, _err in entries or []:
        for contest in contests or []:
            try:
                start = start_of(contest)
            except Exception:
                start = None
            if start is None:
                continue
            stamp = float(start.timestamp())
            if stamp > deadline:
                continue
            if end_of is not None:
                try:
                    end = end_of(contest)
                except Exception:
                    end = None
                if end is not None and float(end.timestamp()) < float(now):
                    continue   # 早已结束的历史比赛不算近期
            picked.append((stamp, str(platform), contest))
    picked.sort(key=lambda item: item[0])
    return [(platform, contest) for _stamp, platform, contest in picked[: max(1, int(limit))]]


def _row_get(row: Any, *keys: str) -> Any:
    """从 dict 或对象里按候选键取值。

    真实排行行是 **dict**（main.py 里 rows.append({...})），旧实现只认对象属性，
    于是每一行都渲染成「- | 未知成员」。
    """
    for key in keys:
        value = row.get(key) if isinstance(row, dict) else getattr(row, key, None)
        if value not in (None, ""):
            return value
    return None


def rank_lines(rows: Iterable[Any], limit: int = 10, *, page: int = 1) -> list:
    """把排行行渲染成纯文本（dict 与对象都支持；字段缺失就跳过，不抛异常）。"""
    lines: list = []
    items = list(rows or [])
    page = max(1, int(page))
    limit = max(1, int(limit))
    start = (page - 1) * limit
    for offset, row in enumerate(items[start : start + limit], start + 1):
        rank = _row_get(row, "rank") or offset
        name = _row_get(row, "display_name", "handle", "name") or "未知成员"
        bits = ["%s | %s" % (rank, name)]
        value = _row_get(row, "display_value", "value")
        label = _row_get(row, "metric_label")
        if value is not None:
            bits.append("%s %s" % (label or "成绩", value))
        delta = _row_get(row, "delta")
        if delta is not None:
            try:
                if int(delta) != 0:
                    bits.append("近7日 %+d" % int(delta))
            except (TypeError, ValueError):
                pass
        lines.append("，".join(bits))
    return lines



def problem_text(problem: Any) -> str:
    """每日一题的纯文本（复用题目对象自己的 display()）。"""
    if problem is None:
        return ""
    display = getattr(problem, "display", None)
    title = ""
    if callable(display):
        try:
            title = str(display())
        except Exception:
            title = ""
    title = title or str(getattr(problem, "title", "") or getattr(problem, "problem_id", "") or "")
    url = str(getattr(problem, "url", "") or "")
    return (title + " " + url).strip()


def _profile_parts(item: Any) -> tuple:
    """把各种形态统一成 (platform, profile, err)。

    真实 `_load_bound_profiles` 返回 (accounts, profiles, errors)，其中 profiles 是 profile 对象列表
    AI 工具另有一条 (platform, profile, err) 形态，两种都要认。
    """
    if isinstance(item, (tuple, list)):
        values = list(item) + [None, None, None]
        return values[0], values[1], values[2]
    if isinstance(item, dict):
        return item.get("platform"), item.get("profile") or item, None
    platform = getattr(item, "platform", None) or getattr(item, "source", None)
    return platform, item, None


def profile_lines(profiles: Iterable[Any], metric_of: Callable[[Any], Any]) -> list:
    """渲染战绩行：接受 profile 对象 / (platform, profile, err) / dict。"""
    lines: list = []
    for item in profiles or []:
        platform, profile, err = _profile_parts(item)
        label = PLATFORM_LABELS.get(str(platform or ""), str(platform or "") or "平台")
        if profile is None:
            lines.append("%s：%s" % (label, err or "无数据"))
            continue
        try:
            metric = metric_of(profile)
        except Exception:
            metric = None
        if isinstance(metric, dict):
            bits = ["%s %s" % (key, value) for key, value in metric.items() if value not in (None, "")]
            lines.append("%s：%s" % (label, "，".join(bits) or "已绑定，暂无数据"))
        else:
            lines.append("%s：%s" % (label, metric or "已绑定，暂无数据"))
    return lines