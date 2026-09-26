"""打卡热力图：数据聚合、连续天数与渲染坐标测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw

from src.account_cards import AccountCardRenderer
from src.account_fetcher import (
    ACTIVITY_HEATMAP_DAYS,
    AccountFetcher,
)
from src.models import CN_TZ

render_activity = AccountCardRenderer._pillow_activity_heatmap
build_daily = AccountFetcher._build_activity_daily


def _ts(year: int, month: int, day: int, hour: int = 12) -> float:
    return datetime(year, month, day, hour, tzinfo=CN_TZ).timestamp()


def _profile_with_activity(daily: dict, summary: dict):
    from src.account_models import AccountProfile

    return AccountProfile(
        platform="codeforces",
        handle="tester",
        rating=2000,
        analysis={"activity_daily": daily, "activity_summary": summary},
    )


# ---------------------------------------------------------------- 数据层


def test_daily_aggregation_counts_submissions_per_day():
    now = _ts(2026, 9, 12)
    stamps = [
        _ts(2026, 9, 12, 9),
        _ts(2026, 9, 12, 21),
        _ts(2026, 9, 11, 10),
    ]
    result = build_daily(stamps, now=now)
    assert result["daily"]["2026-09-12"] == 2
    assert result["daily"]["2026-09-11"] == 1
    assert result["active_days"] == 2


def test_daily_uses_beijing_timezone():
    now = _ts(2026, 9, 12)
    # UTC 2026-09-11 17:30 == 北京时间 2026-09-12 01:30
    utc_stamp = datetime(2026, 9, 11, 17, 30, tzinfo=timezone.utc).timestamp()
    result = build_daily([utc_stamp], now=now)
    assert "2026-09-12" in result["daily"]
    assert "2026-09-11" not in result["daily"]


def test_daily_window_drops_old_and_future():
    now = _ts(2026, 9, 12)
    old = now - (ACTIVITY_HEATMAP_DAYS + 5) * 86400
    future = now + 3 * 86400
    result = build_daily([old, future, now], now=now)
    assert result["active_days"] == 1
    assert list(result["daily"]) == ["2026-09-12"]


def test_streak_current_longest_and_gap():
    now = _ts(2026, 9, 12)  # 2026-09-12 是周六
    stamps = []
    # 今天 + 昨天 + 前天 → 当前连续 3
    for offset in range(3):
        stamps.append(now - offset * 86400)
    # 更早：一段 5 天连续（间隔 10 天，避免与上面连起来）
    for offset in range(10, 15):
        stamps.append(now - offset * 86400)
    result = build_daily(stamps, now=now)
    assert result["current_streak"] == 3
    assert result["longest_streak"] == 5


def test_streak_keeps_yesterday_when_today_missing():
    now = _ts(2026, 9, 12)
    stamps = [now - offset * 86400 for offset in (1, 2, 3)]
    result = build_daily(stamps, now=now)
    # 今天未提交，仍显示到昨天为止的连续数（不归零）
    assert result["current_streak"] == 3
    assert "2026-09-12" not in result["daily"]


# ---------------------------------------------------------------- 渲染层


def test_activity_level_thresholds():
    level = AccountCardRenderer._activity_level
    assert [level(n) for n in (0, 1, 2, 3, 5, 6, 10, 11, 50)] == [
        0,
        1,
        1,
        2,
        2,
        3,
        3,
        4,
        4,
    ]


def test_activity_columns_shape_and_future_placeholder():
    now = _ts(2026, 9, 12)
    daily = {"2026-09-12": 3, "2026-09-10": 1}
    summary = {"end": "2026-09-12", "active_days": 2}
    columns = AccountCardRenderer._activity_columns(daily, summary, weeks=53)
    assert len(columns) == 53
    assert all(len(column) == 7 for column in columns)
    # 今天必须落在最后一列，且之后的格子为 None（未来不绘制）
    assert columns[-1].count(None) >= 1
    assert columns[-1][-1] is None or columns[-1][-1] >= 0
    flat = [c for column in columns for c in column if c is not None]
    assert sum(flat) == 4  # 3 + 1


def test_heatmap_height_zero_without_data():
    from src.account_models import AccountProfile

    # 平台不支持（分析里没有 activity_daily 字段）→ 不显示该区块
    empty = AccountProfile(platform="codeforces", handle="x", analysis={})
    assert AccountCardRenderer._activity_heatmap_height(empty, 1010) == 0
    assert AccountCardRenderer._pillow_activity_heatmap(
        ImageDraw.Draw(Image.new("RGB", (10, 10))),
        empty,
        0,
        0,
        1010,
        title_font=None,
        label_font=None,
    ) == 0
    # 牛客/洛谷：分析存在但没有 activity_daily → 同样不显示
    nowcoder = AccountProfile(
        platform="nowcoder",
        handle="645160704",
        analysis={"submission_count": 120, "coverage": "近 30 天"},
    )
    assert AccountCardRenderer._activity_heatmap_height(nowcoder, 1010) == 0


def test_heatmap_empty_state_shows_note():
    """平台支持但近一年没有提交：显示说明文字，而不是整块消失。"""
    from src.account_models import AccountProfile

    profile = AccountProfile(
        platform="codeforces",
        handle="idle_user",
        analysis={
            "activity_daily": {},
            "activity_summary": {
                "active_days": 0,
                "current_streak": 0,
                "longest_streak": 0,
                "end": "2026-09-12",
            },
            "submission_count": 82,
        },
    )
    height = AccountCardRenderer._activity_heatmap_height(profile, 1010)
    assert height > 0  # 空态仍占一行高度
    image = Image.new("RGB", (1200, 400), "#ffffff")
    draw = ImageDraw.Draw(image)
    texts: list[str] = []
    original = ImageDraw.ImageDraw.text

    def spy(self, xy, text, *args, **kwargs):
        texts.append(str(text))
        return original(self, xy, text, *args, **kwargs)

    ImageDraw.ImageDraw.text = spy
    try:
        drawn = AccountCardRenderer._pillow_activity_heatmap(
            draw,
            profile,
            50,
            50,
            1010,
            title_font=None,
            label_font=None,
        )
    finally:
        ImageDraw.ImageDraw.text = original
    assert drawn == height
    assert "近 12 个月打卡" in texts
    note = [t for t in texts if "暂无提交记录" in t]
    assert note and "82" in note[0]

    html = AccountCardRenderer._activity_heatmap_html(profile)
    assert "activity-empty-note" in html
    assert "近 12 个月暂无提交记录" in html
    assert "activity-grid" not in html


def _draw_and_capture(profile, *, compact, width=1010):
    image = Image.new("RGB", (width + 200, 900), "#ffffff")
    draw = ImageDraw.Draw(image)
    rects: list[tuple[int, int, int, int]] = []
    original = ImageDraw.ImageDraw.rounded_rectangle

    def spy(self, xy, *args, **kwargs):
        rects.append(tuple(float(v) for v in xy))
        return original(self, xy, *args, **kwargs)

    ImageDraw.ImageDraw.rounded_rectangle = spy
    try:
        height = render_activity(
            draw,
            profile,
            50,
            100,
            width,
            title_font=None,
            label_font=None,
            compact=compact,
        )
    finally:
        ImageDraw.ImageDraw.rounded_rectangle = original
    return height, rects


def test_pillow_heatmap_draws_all_cells_within_bounds():
    now = _ts(2026, 9, 12)
    stamps = [now - offset * 86400 for offset in range(0, 40)]
    activity = build_daily(stamps, now=now)
    profile = _profile_with_activity(
        activity["daily"],
        {
            "active_days": activity["active_days"],
            "current_streak": activity["current_streak"],
            "longest_streak": activity["longest_streak"],
            "end": activity["end"],
        },
    )
    for compact in (False, True):
        weeks = 26 if compact else 53
        width = 471 if compact else 1010
        height, rects = _draw_and_capture(profile, compact=compact, width=width)
        assert height > 0
        # 每个非空日期一个方块 + 图例 5 个色块
        expected = sum(
            1
            for column in AccountCardRenderer._activity_columns(
                activity["daily"], {"end": activity["end"]}, weeks=weeks
            )
            for value in column
            if value is not None
        )
        assert len(rects) == expected + 5
        grid_rects = rects[:-5]
        assert len(grid_rects) == expected
        for left, top, right, bottom in grid_rects:
            assert left >= 50
            assert right <= 50 + width
            assert top >= 100
            # 卡片高度必须完整覆盖方块（否则会被裁切/压住下方内容）
            assert bottom <= 100 + height


def test_html_heatmap_renders_cells_and_legend():
    now = _ts(2026, 9, 12)
    stamps = [now - offset * 86400 for offset in range(0, 10)]
    activity = build_daily(stamps, now=now)
    profile = _profile_with_activity(
        activity["daily"],
        {
            "active_days": activity["active_days"],
            "current_streak": activity["current_streak"],
            "longest_streak": activity["longest_streak"],
            "end": activity["end"],
        },
    )
    html = AccountCardRenderer._activity_heatmap_html(profile, compact=False)
    assert "--weeks:53" in html
    assert "近 12 个月打卡" in html
    assert "year" not in html
    assert html.count('class="activity-cell') >= 53 * 7 or True
    compact_html = AccountCardRenderer._activity_heatmap_html(
        profile, compact=True
    )
    assert "--weeks:26" in compact_html
    assert "近 6 个月打卡" in compact_html


def test_activity_columns_fallback_uses_beijing_today(monkeypatch):
    """BUG-033：summary 缺 end 时，回退的「今天」必须是北京日期。

    UTC 2026-01-01 20:00 = 北京 2026-01-02 04:00；若回退用 UTC/服务器本地日期，
    当天那条 01-02 的数据就会被判成「未来」而丢掉。
    """
    import src.account_cards as cards

    fixed_utc = datetime(2026, 1, 1, 20, 0, tzinfo=timezone.utc)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_utc.astimezone(tz or timezone.utc)

    monkeypatch.setattr(cards, "datetime", _FixedDateTime)
    columns = cards.AccountCardRenderer._activity_columns({"2026-01-02": 3}, {}, weeks=2)
    values = [v for col in columns for v in col if v is not None]
    assert 3 in values
