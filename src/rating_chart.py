"""战绩卡 Rating 曲线几何：HTML 与 Pillow 两条渲染路径的唯一坐标来源。

本模块只做「点 → 像素坐标」的纯计算，不依赖 CSS、字体、Pillow 或渲染器。
调用方必须显式传入绘图区 `width` / `height`，模块内不读任何布局常量。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .models import CN_TZ

# ---- 几何常量（唯一来源，account_cards 只能 import，不得重定义）----
RATING_CHART_SVG_HEIGHT = 120      # 绘图区像素高
RATING_CHART_AXIS_WIDTH = 46       # 左侧刻度列宽
RATING_CHART_AXIS_GAP = 8          # 刻度列与绘图区间距
RATING_CHART_HTML_WIDTH_SINGLE = 926   # 单平台卡图表内容宽 980 - 46 - 8
RATING_CHART_HTML_WIDTH_MULTI = 387    # 双平台卡图表内容宽 441 - 46 - 8
RATING_CHART_HEIGHT = 186          # 整块卡片高度预算（标题+绘图+底栏+内边距）
RATING_CHART_DISPLAY_LIMIT = 8     # 取最近 N 场
PLOT_INSET_Y = 6                   # 上下留白，防圆点被裁
RATING_CHART_TIME_AXIS_DAYS = 90   # 跨度超过则改用真实时间轴
RATING_CHART_MIN_DOT_GAP = 8       # 相邻点 Δx 小于此值则不画后一个点
RATING_CHART_MIN_TS = 1262304000   # 2010-01-01 UTC；早于此视为脏时间戳

_TS_EPSILON = 1e-9


@dataclass(frozen=True)
class RatingPoint:
    value: int
    timestamp: Optional[float]   # 仅记录真实的平台时间戳；缺失为 None
    delta: Optional[int]
    x: float                     # 相对绘图区左上角
    y: float
    show_dot: bool = True

    @property
    def is_flat(self) -> bool:
        """参赛但 rating 未变（delta == 0），降级为淡色浅描边点（描边 1.2、透明度 .62）。"""
        return self.delta == 0


@dataclass(frozen=True)
class RatingChart:
    points: Tuple[RatingPoint, ...]
    lower: int
    upper: int
    width: float
    height: float
    axis: str                          # "index" | "time"
    span_days: Optional[float]
    first_month: Optional[str]         # "YYYY-MM"，CN_TZ
    last_month: Optional[str]
    pb: Optional[int]                  # 生涯最高，仅当 > 窗口最大值且入域
    pb_y: Optional[float]
    pb_out_of_range: Optional[int]     # 生涯最高但未入域
    flat: bool                          # 整窗所有值相同（生产 0 例，防御）

    @property
    def latest(self) -> int:
        return self.points[-1].value

    @property
    def latest_delta(self) -> Optional[int]:
        return self.points[-1].delta

    @property
    def aria_label(self) -> str:
        low = min(p.value for p in self.points)
        high = max(p.value for p in self.points)
        head = f"最近 {len(self.points)} 场"
        if self.first_month and self.last_month:
            head += f"，{self.first_month} 至 {self.last_month}"
        tail = f"，从 {low} 到 {self.latest}"
        if self.pb is not None:
            tail += f"，生涯最高 {self.pb}"
        elif self.pb_out_of_range is not None:
            tail += f"，生涯最高 {self.pb_out_of_range}（图外）"
        return f"Rating 趋势：{head}{tail}"


def _field(profile: object, key: str, default=None):
    """等价于 account_cards._profile_field：同时读 AccountProfile 与 public_dict()。"""
    if isinstance(profile, dict):
        return profile.get(key, default)
    return getattr(profile, key, default)


def _plausible_ts(timestamp: Optional[float]) -> bool:
    """时间戳存在且不早于 2010-01-01，才用于时间轴与月份标签。"""
    return timestamp is not None and timestamp >= RATING_CHART_MIN_TS


def _month_of(timestamp: Optional[float]) -> Optional[str]:
    if not _plausible_ts(timestamp):
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=CN_TZ).strftime("%Y-%m")
    except (OverflowError, OSError, ValueError):
        return None


def collect_rating_points(profile: object, limit: int = RATING_CHART_DISPLAY_LIMIT):
    """按 (timestamp, 原序) 升序取最后 limit 个可解析项。

    与既有 _rating_history_values 语义逐条对齐：
    - limit 下限 2（沿用 max(2, limit)，不是字面取 0/1 条）；
    - 非 dict / rating 不可 int 的项跳过且不补位；
    - 缺 timestamp 的项用原序 index 兜底参与排序（因此不会产生逆序）。
    """
    raw = _field(profile, "rating_history", []) or []
    if not isinstance(raw, list):
        return []
    rows: List[Tuple[float, int, int, Optional[float], Optional[int]]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        try:
            rating = int(item.get("rating"))
        except (TypeError, ValueError):
            continue
        real_ts = item.get("timestamp")
        try:
            sort_ts = float(real_ts) if real_ts is not None else float(index)
        except (TypeError, ValueError):
            sort_ts = float(index)
            real_ts = None
        else:
            real_ts = sort_ts if real_ts is not None else None
        delta = item.get("delta")
        try:
            delta = int(delta) if delta is not None else None
        except (TypeError, ValueError):
            delta = None
        rows.append((sort_ts, index, rating, real_ts, delta))
    rows.sort(key=lambda row: (row[0], row[1]))
    return rows[-max(2, int(limit)):]


def rating_point_count(profile: object, limit: int = RATING_CHART_DISPLAY_LIMIT) -> int:
    return len(collect_rating_points(profile, limit))


def build_rating_chart(
    profile: object,
    *,
    width: float,
    height: float,
    limit: int = RATING_CHART_DISPLAY_LIMIT,
) -> Optional[RatingChart]:
    """把 rating_history 变成像素坐标。点数 < 2 或宽度非法时返回 None。"""
    rows = collect_rating_points(profile, limit)
    if len(rows) < 2 or width <= 0 or height <= 0:
        return None

    values = [row[2] for row in rows]
    timestamps = [row[3] for row in rows]
    vmin, vmax = min(values), max(values)

    # ---- 纵轴定义域 ----
    flat = vmin == vmax
    if flat:
        lower, upper = vmin - 50, vmin + 50
    else:
        pad = max(25, round((vmax - vmin) * 0.12))
        lower, upper = vmin - pad, vmax + pad

    # ---- 生涯最高 PB ----
    pb_raw = _field(profile, "max_rating", None)
    try:
        pb = int(pb_raw) if pb_raw is not None else None
    except (TypeError, ValueError):
        pb = None
    pb = pb if pb is not None else max(values)
    pb_value: Optional[int] = None
    pb_out_of_range: Optional[int] = None
    if pb > vmax:
        if pb - vmax <= max(150, vmax - vmin):
            pb_value = pb
            upper = max(upper, pb + max(10, round((vmax - vmin) * 0.05)))
        else:
            pb_out_of_range = pb
    if upper <= lower:          # 理论上不可达，兜底防除零
        upper = lower + 1

    # ---- X 轴 ----
    have_all_ts = all(_plausible_ts(ts) for ts in timestamps)
    span_days: Optional[float] = None
    axis = "index"
    if have_all_ts:
        span_seconds = timestamps[-1] - timestamps[0]
        if span_seconds > _TS_EPSILON:
            span_days = span_seconds / 86400.0
            if span_days > RATING_CHART_TIME_AXIS_DAYS:
                axis = "time"

    if axis == "time":
        base = timestamps[0]
        total = timestamps[-1] - base
        xs = [(ts - base) / total * width for ts in timestamps]
    else:
        step = width / (len(values) - 1)
        xs = [step * i for i in range(len(values))]

    # ---- 像素映射 ----
    usable = max(1.0, height - PLOT_INSET_Y * 2)
    scale = upper - lower
    ys = [PLOT_INSET_Y + (upper - v) / scale * usable for v in values]

    # ---- 相邻点过近则不画点 ----
    show = [True] * len(values)
    for i in range(1, len(values)):
        if axis == "time" and xs[i] - xs[i - 1] < RATING_CHART_MIN_DOT_GAP:
            show[i] = False

    points = tuple(
        RatingPoint(
            value=values[i],
            timestamp=timestamps[i],
            delta=rows[i][4],
            x=xs[i],
            y=ys[i],
            show_dot=show[i],
        )
        for i in range(len(values))
    )

    return RatingChart(
        points=points,
        lower=int(lower),
        upper=int(upper),
        width=float(width),
        height=float(height),
        axis=axis,
        span_days=span_days,
        first_month=_month_of(timestamps[0]) if have_all_ts else None,
        last_month=_month_of(timestamps[-1]) if have_all_ts else None,
        pb=pb_value,
        pb_y=(PLOT_INSET_Y + (upper - pb_value) / scale * usable) if pb_value is not None else None,
        pb_out_of_range=pb_out_of_range,
        flat=flat,
    )
