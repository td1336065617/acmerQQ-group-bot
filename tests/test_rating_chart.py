from __future__ import annotations

import pytest
from PIL import Image, ImageDraw, ImageFont

from src.account_models import AccountProfile
from src.rating_chart import (
    PLOT_INSET_Y,
    RATING_CHART_HEIGHT,
    RATING_CHART_HTML_WIDTH_MULTI,
    RATING_CHART_HTML_WIDTH_SINGLE,
    RATING_CHART_MIN_TS,
    RATING_CHART_SVG_HEIGHT,
    build_rating_chart,
    collect_rating_points,
)

DAY = 86400.0
T0 = 1709251200.0          # 2024-03-01 08:00 CST


def make_profile(history, **kwargs):
    return AccountProfile(
        platform=kwargs.pop("platform", "codeforces"),
        handle=kwargs.pop("handle", "demo"),
        rating=history[-1]["rating"] if history else 0,
        rating_history=history,
        **kwargs,
    )


def hist(values, *, start=T0, step=DAY, deltas=None):
    rows, prev = [], None
    for i, value in enumerate(values):
        row = {"rating": value, "timestamp": start + i * step}
        if deltas is not None:
            row["delta"] = deltas[i]
        elif prev is not None:
            row["delta"] = value - prev
        else:
            row["delta"] = 0
        rows.append(row)
        prev = value
    return rows
def test_01_short_span_uses_index_axis():
    chart = build_rating_chart(
        make_profile(hist([1500, 1510, 1520, 1530])),
        width=RATING_CHART_HTML_WIDTH_SINGLE, height=RATING_CHART_SVG_HEIGHT,
    )
    assert chart.axis == "index"
    xs = [p.x for p in chart.points]
    assert xs[0] == pytest.approx(0)
    assert xs[-1] == pytest.approx(RATING_CHART_HTML_WIDTH_SINGLE)
    gaps = [round(b - a, 6) for a, b in zip(xs, xs[1:])]
    assert len(set(gaps)) == 1


def test_02_long_span_uses_time_axis():
    rows = [
        {"rating": 1500, "timestamp": T0, "delta": 0},
        {"rating": 1600, "timestamp": T0 + 133 * DAY, "delta": 100},
        {"rating": 1700, "timestamp": T0 + 400 * DAY, "delta": 100},
    ]
    chart = build_rating_chart(make_profile(rows), width=926, height=120)
    assert chart.axis == "time"
    assert chart.span_days == pytest.approx(400, abs=0.01)
    assert chart.points[1].x == pytest.approx(926 * 133 / 400, abs=0.5)


def test_03_flat_series_is_defensive_only():
    profile = make_profile(hist([3013] * 8), max_rating=None)
    chart = build_rating_chart(profile, width=926, height=120)
    assert chart.flat is True
    assert chart.upper - chart.lower == 100
    assert chart.pb is None
    assert len({round(p.y, 6) for p in chart.points}) == 1


def test_04_zero_delta_point_is_flat():
    profile = make_profile(hist([1500, 1500, 1600], deltas=[0, 0, 100]))
    chart = build_rating_chart(profile, width=926, height=120)
    assert [p.is_flat for p in chart.points] == [True, True, False]


def test_05_pb_near_window_max_enters_domain():
    chart = build_rating_chart(
        make_profile(hist([1500, 1520, 1540]), max_rating=1600),
        width=926, height=120,
    )
    assert chart.pb == 1600
    assert chart.upper >= 1600
    assert chart.pb_y is not None


def test_06_pb_far_above_window_is_out_of_range():
    chart = build_rating_chart(
        make_profile(hist([1500, 1520, 1540]), max_rating=2600),
        width=926, height=120,
    )
    assert chart.pb is None
    assert chart.pb_out_of_range == 2600
    assert chart.pb_y is None


def test_07_missing_timestamps_fall_back_to_index():
    rows = [{"rating": v} for v in (1500, 1510, 1520)]
    chart = build_rating_chart(make_profile(rows), width=926, height=120)
    assert chart.axis == "index"
    assert chart.first_month is None and chart.last_month is None
    assert chart.span_days is None


def test_08_width_does_not_affect_domain():
    profile = make_profile(hist([1500, 1520, 1540, 1560]))
    a = build_rating_chart(profile, width=926, height=120)
    b = build_rating_chart(profile, width=387, height=120)
    assert (a.lower, a.upper) == (b.lower, b.upper)
    assert [p.y for p in a.points] == [p.y for p in b.points]


def test_09_html_viewbox_matches_constants(tmp_path):
    from src.account_cards import AccountCardRenderer

    renderer = AccountCardRenderer(cache_dir=tmp_path)
    profile = make_profile(hist([1500, 1520, 1540]))
    single = renderer._rating_history_html(profile)
    multi = renderer._rating_history_html(profile, compact=True)
    assert f'"0 0 {RATING_CHART_HTML_WIDTH_SINGLE} {RATING_CHART_SVG_HEIGHT}"' in single
    assert f'"0 0 {RATING_CHART_HTML_WIDTH_MULTI} {RATING_CHART_SVG_HEIGHT}"' in multi


def test_10_css_has_single_source_of_truth():
    import inspect
    from src import account_cards

    src = inspect.getsource(account_cards)
    lines = src.splitlines()
    base = [ln for ln in lines if ln.strip().startswith(".rating-chart {{")]
    assert len(base) == 1
    assert ".difficulty-panel" not in base[0]
    assert "padding:12px 14px 10px" in base[0]
    panel = [ln for ln in lines if ln.strip().startswith(".difficulty-panel {{")]
    assert len(panel) == 1
    assert "padding:12px 14px 13px" in panel[0]
    assert ".rating-chart {{ padding-bottom" not in src
    assert ".profile-document .rating-chart-svg" not in src
    svg = [ln for ln in lines if ln.strip().startswith(".rating-chart-svg {{")]
    assert len(svg) == 1
    assert "width:auto" in svg[0]
    assert "width:100%" not in svg[0]
    assert "height:120px" in svg[0]
    assert "height:78px" not in svg[0]
    axis = [ln for ln in lines if ln.strip().startswith(".rating-chart-axis {{")]
    assert len(axis) == 1
    assert "flex:0 0 46px" in axis[0]


def test_11_card_format_version_bumped(tmp_path):
    import hashlib
    import inspect
    import json
    from src import account_cards as mod

    assert mod.CARD_FORMAT_VERSION == 20
    render_src = inspect.getsource(mod.AccountCardRenderer._render)
    assert "CARD_FORMAT_VERSION" in render_src
    assert 'f"{CARD_FORMAT_VERSION}' in render_src

    renderer = mod.AccountCardRenderer(cache_dir=tmp_path)
    source = {"kind": "profile", "profiles": []}
    expected = hashlib.sha256(
        f"{mod.CARD_FORMAT_VERSION}\0"
        f"{json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}".encode("utf-8")
    ).hexdigest()[:24]
    renderer._render("<html></html>", source, None)
    assert (tmp_path / f"account-card-{expected}.html").is_file()


def test_12_pillow_uses_container_width_contract(monkeypatch):
    from src.account_cards import AccountCardRenderer
    import src.rating_chart as rc

    seen = {}
    real = rc.build_rating_chart

    def spy(profile, *, width, height, limit=8):
        seen["width"] = width
        seen["height"] = height
        return real(profile, width=width, height=height, limit=limit)

    monkeypatch.setattr("src.account_cards.build_rating_chart", spy)
    draw = ImageDraw.Draw(Image.new("RGB", (600, 400), "white"))
    font = ImageFont.load_default()
    height = AccountCardRenderer._pillow_rating_chart(
        draw, make_profile(hist([1500, 1520, 1540])),
        10, 10, 1010, title_font=font, label_font=font,
    )
    assert height == RATING_CHART_HEIGHT
    assert seen["width"] == 926          # 1010 - 84
    assert seen["height"] == RATING_CHART_SVG_HEIGHT


def test_13_same_second_span_degrades_to_index():
    rows = [
        {"rating": 1500, "timestamp": T0, "delta": 0},
        {"rating": 1510, "timestamp": T0, "delta": 10},
        {"rating": 1520, "timestamp": T0, "delta": 10},
    ]
    chart = build_rating_chart(make_profile(rows), width=926, height=120)
    assert chart.axis == "index"
    assert chart.span_days is None


def test_14_limit_below_two_is_clamped():
    profile = make_profile(hist([1500, 1510, 1520]))
    assert len(collect_rating_points(profile, 0)) == 2
    assert len(collect_rating_points(profile, 1)) == 2
    assert len(collect_rating_points(profile, 8)) == 3


def test_15_invalid_items_are_skipped_not_padded():
    rows = [
        None,
        {"rating": 1500, "timestamp": T0},
        "junk",
        {"rating": "abc", "timestamp": T0 + DAY},
        {"rating": 1520, "timestamp": T0 + 2 * DAY},
    ]
    points = collect_rating_points(make_profile(rows), 8)
    assert [row[2] for row in points] == [1500, 1520]


def test_16_pb_below_window_max_is_ignored():
    chart = build_rating_chart(
        make_profile(hist([1600, 1610, 1620]), max_rating=1500),
        width=926, height=120,
    )
    assert chart.pb is None
    assert chart.pb_out_of_range is None


def test_17_one_sided_timestamp_still_monotonic():
    rows = [
        {"rating": 1500},
        {"rating": 1510, "timestamp": T0 + DAY},
        {"rating": 1520, "timestamp": T0 + 2 * DAY},
    ]
    chart = build_rating_chart(make_profile(rows), width=926, height=120)
    xs = [p.x for p in chart.points]
    assert xs == sorted(xs)
    assert [p.value for p in chart.points] == [1500, 1510, 1520]


def test_18_months_use_cn_tz():
    ts = 1709233200.0          # 2024-03-01 03:00 CST == 2024-02-29 19:00 UTC
    rows = [
        {"rating": 1500, "timestamp": ts, "delta": 0},
        {"rating": 1600, "timestamp": ts + 400 * DAY, "delta": 100},
    ]
    chart = build_rating_chart(make_profile(rows), width=926, height=120)
    assert chart.first_month == "2024-03"


def test_19_pillow_chart_returns_full_height():
    from src.account_cards import AccountCardRenderer

    draw = ImageDraw.Draw(Image.new("RGB", (1200, 400), "white"))
    font = ImageFont.load_default()
    assert AccountCardRenderer._pillow_rating_chart(
        draw, make_profile(hist([1500, 1520, 1540, 1560])),
        25, 20, 1010, title_font=font, label_font=font,
    ) == RATING_CHART_HEIGHT
    assert AccountCardRenderer._pillow_rating_chart(
        draw, make_profile(hist([1500])),
        25, 20, 1010, title_font=font, label_font=font,
    ) == 0


def test_20_dense_points_hide_later_dots():
    rows = [
        {"rating": 1500, "timestamp": T0, "delta": 0},
        {"rating": 1501, "timestamp": T0 + DAY, "delta": 1},
        {"rating": 1600, "timestamp": T0 + 4000 * DAY, "delta": 99},
    ]
    chart = build_rating_chart(make_profile(rows), width=RATING_CHART_HTML_WIDTH_MULTI, height=120)
    assert chart.axis == "time"
    assert chart.points[0].show_dot is True
    assert chart.points[1].show_dot is False
    assert chart.points[2].show_dot is True


def test_21_implausible_timestamps_hide_months():
    rows = [{"rating": r, "timestamp": i + 1} for i, r in enumerate([1500, 1610, 1580, 1820])]
    chart = build_rating_chart(make_profile(rows), width=RATING_CHART_HTML_WIDTH_SINGLE, height=120)
    assert chart.axis == "index"
    assert chart.first_month is None and chart.last_month is None
    assert chart.span_days is None
    mixed = [{"rating": 1500, "timestamp": 1}, {"rating": 1600, "timestamp": T0 + DAY}]
    chart2 = build_rating_chart(make_profile(mixed), width=926, height=120)
    assert chart2.first_month is None and chart2.last_month is None
    ok = [{"rating": 1500, "timestamp": RATING_CHART_MIN_TS}, {"rating": 1600, "timestamp": RATING_CHART_MIN_TS + 400 * DAY}]
    chart3 = build_rating_chart(make_profile(ok), width=926, height=120)
    assert chart3.axis == "time"
    assert chart3.first_month == "2010-01"


def test_22_flat_axis_shows_single_value(tmp_path):
    from src.account_cards import AccountCardRenderer

    renderer = AccountCardRenderer(cache_dir=tmp_path)
    html = renderer._rating_history_html(make_profile(hist([3013] * 8), max_rating=None))
    start = html.index('class="rating-chart-axis"')
    axis = html[start:html.index("</div>", start)]
    assert axis.count("3013") == 1
    assert "<span></span><span>3013</span><span></span>" in axis


class _CaptureDraw:
    def __init__(self, image):
        self._draw = ImageDraw.Draw(image)
        self.texts = []
        self.text_positions = []
        self.lines = []

    def text(self, xy, text, *args, **kwargs):
        self.texts.append(str(text))
        self.text_positions.append((xy, str(text)))
        return self._draw.text(xy, text, *args, **kwargs)

    def line(self, xy, *args, **kwargs):
        self.lines.append((xy, kwargs.get("fill")))
        return self._draw.line(xy, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._draw, name)


def test_23_html_pb_in_domain_renders_reference_line(tmp_path):
    from src.account_cards import AccountCardRenderer

    renderer = AccountCardRenderer(cache_dir=tmp_path)
    html = renderer._rating_history_html(
        make_profile(hist([1500, 1520, 1540]), max_rating=1600)
    )
    assert html.count('class="rating-chart-pb"') == 1
    assert 'rating-chart-pb' in html
    assert "rating-chart-pb-label" in html
    assert "PB 1600" in html
    # 标签基线必须在虚线下方：压在线上会被虚线穿过（y 向下为正）。
    import re

    line_y = float(
        re.search(r'class="rating-chart-pb" x1="[\d.]+" y1="([\d.]+)"', html).group(1)
    )
    label_y = float(
        re.search(r'class="rating-chart-pb-label"[^>]*?y="([\d.]+)"', html).group(1)
    )
    assert label_y >= line_y + 10


def test_24_html_pb_out_of_range_keeps_last_month(tmp_path):
    from src.account_cards import AccountCardRenderer

    renderer = AccountCardRenderer(cache_dir=tmp_path)
    html = renderer._rating_history_html(
        make_profile(hist([1500, 1520, 1540], step=200 * DAY), max_rating=2600)
    )
    start = html.index('class="rating-chart-scale"')
    scale = html[start:html.index("</div>", start)]
    assert "2025-04" in scale
    assert "PB" not in scale
    assert "rating-chart-pb-label" in html
    assert "PB 2600（图外）" in html


def test_25_pillow_pb_text_is_drawn():
    from src.account_cards import AccountCardRenderer

    capture = _CaptureDraw(Image.new("RGB", (1200, 400), "white"))
    font = ImageFont.load_default()
    AccountCardRenderer._pillow_rating_chart(
        capture,
        make_profile(hist([1500, 1520, 1540]), max_rating=2600),
        25,
        20,
        1010,
        title_font=font,
        label_font=font,
    )
    assert "PB 2600（图外）" in capture.texts

    # 入域时虚线存在，文字必须画在虚线下方，否则虚线穿字。
    dom = _CaptureDraw(Image.new("RGB", (1200, 400), "white"))
    AccountCardRenderer._pillow_rating_chart(
        dom,
        make_profile(hist([1500, 1520, 1540]), max_rating=1600),
        25,
        20,
        1010,
        title_font=font,
        label_font=font,
    )
    dash_ys = [xy[1] for xy, fill in dom.lines if fill == "#b03a7a" and xy[1] == xy[3]]
    pb_pos = [pos[1] for pos, text in dom.text_positions if text == "PB 1600"]
    assert dash_ys, "入域 PB 必须画虚线"
    assert pb_pos, "入域 PB 必须画数值标签"
    assert pb_pos[0] >= min(dash_ys) + 1


def test_26_pillow_flat_text_shows_tie_count():
    from src.account_cards import AccountCardRenderer

    capture = _CaptureDraw(Image.new("RGB", (1200, 400), "white"))
    font = ImageFont.load_default()
    AccountCardRenderer._pillow_rating_chart(
        capture,
        make_profile(hist([3013] * 8), max_rating=None),
        25,
        20,
        1010,
        title_font=font,
        label_font=font,
    )
    assert any("8 场持平" in text for text in capture.texts)


def test_27_css_rating_scale_has_four_pixel_top_margin():
    import inspect
    from src import account_cards

    src = inspect.getsource(account_cards)
    scale = [ln for ln in src.splitlines() if ln.strip().startswith(".rating-chart-scale {{")]
    assert len(scale) == 1
    assert "margin-top:4px" in scale[0]
