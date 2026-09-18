"""赛后赛果卡渲染测试（A1 卡片路径）。

覆盖：HTML 内容、分区排序与截断、高度估算路由、Pillow 回退路径。
"""
from __future__ import annotations

from src.account_cards import (
    CARD_FORMAT_VERSION,
    SETTLE_CARD_MAX_ROWS,
    AccountCardRenderer,
)


def _row(name: str, rank: int | None, solved: int | None, total: int | None, count: int | None = 100):
    return {
        "rank": rank,
        "user_count": count,
        "display_name": name,
        "handle": name.lower(),
        "solved": solved,
        "total_problems": total,
        "ak": bool(solved is not None and total and solved >= total),
        "source": "cf-standings",
    }


SECTIONS = {
    "codeforces": [
        _row("Zhao", 1520, 2, 6, 6243),
        _row("Zhang", 120, 6, 6, 6243),
    ],
    "nowcoder": [_row("Li", 1, 13, 13, 1273)],
}


def test_settlement_html_contains_rank_and_solved_columns():
    html = AccountCardRenderer._settlement_html(
        AccountCardRenderer._ordered_settlement_sections(SECTIONS),
        title="Codeforces Round 1121 (Div. 2) 赛果",
        subtitle="本群 3 人参赛",
        note="数据源：Codeforces 官方 standings",
    )
    assert "名次" in html and "成员" in html and "通过" in html and "参赛人数" in html
    assert "#120" in html and "#1520" in html and "#1" in html
    assert "6/6 题 · AK" in html
    assert "6243 人" in html and "1273 人" in html
    assert "settlement" in html                      # page_class，用于名次列加宽
    assert "评分变化" not in html                     # 卡面不含 Rating 变化
    assert "数据源：Codeforces 官方 standings" in html


def test_sections_are_sorted_by_rank_and_truncated():
    rows = [_row(f"u{index}", 1000 - index, 1, 6) for index in range(30)]
    ordered = AccountCardRenderer._ordered_settlement_sections({"codeforces": rows})
    assert len(ordered["codeforces"]) == SETTLE_CARD_MAX_ROWS
    # 先按名次升序，再截断前 N：留下的是名次最小的 10 人
    assert [row["rank"] for row in ordered["codeforces"]] == list(range(971, 981))

    # 无名次的行排在最后
    mixed = [_row("no-rank", None, 1, 6), _row("ranked", 5, 2, 6)]
    ordered = AccountCardRenderer._ordered_settlement_sections({"codeforces": mixed})
    assert [row["display_name"] for row in ordered["codeforces"]] == ["ranked", "no-rank"]


def test_platform_order_and_empty_sections_dropped():
    ordered = AccountCardRenderer._ordered_settlement_sections(
        {"nowcoder": [_row("Li", 1, 2, 6)], "luogu": []},
        platform_order=["luogu", "nowcoder", "codeforces"],
    )
    assert list(ordered.keys()) == ["nowcoder"]      # 空分区被丢弃


def test_settlement_height_routes_through_estimate_height():
    sections = AccountCardRenderer._ordered_settlement_sections(SECTIONS)
    height = AccountCardRenderer._estimate_height(
        {"kind": "settlement", "sections": sections, "note": "数据源：X"}
    )
    assert height >= 520
    # 行数越多越高
    more = AccountCardRenderer._ordered_settlement_sections(
        {"codeforces": [_row(f"u{i}", i + 1, 1, 6) for i in range(10)]}
    )
    assert AccountCardRenderer._estimate_height(
        {"kind": "settlement", "sections": more}
    ) > height


def test_card_format_version_bumped():
    # 新增卡片类型必须让旧缓存图失效
    assert CARD_FORMAT_VERSION >= 17


def test_render_settlement_pillow_path(tmp_path, monkeypatch):
    """Pillow 回退路径必须能真的产出图片（用 Pillow 自带字体代替 CJK 字体）。"""
    from PIL import ImageFont

    default_font = ImageFont.load_default()
    monkeypatch.setattr(
        AccountCardRenderer,
        "_find_font",
        staticmethod(lambda size, bold=False: default_font),
    )

    sections = AccountCardRenderer._ordered_settlement_sections(SECTIONS)
    image_path = tmp_path / "settlement.png"
    ok = AccountCardRenderer._pillow_settlement(
        sections,
        "Round 1121 Settlement",
        "3 participants",
        "source: codeforces standings",
        image_path,
    )
    assert ok is True
    assert image_path.is_file() and image_path.stat().st_size > 0


def test_render_settlement_returns_none_without_fonts(tmp_path, monkeypatch):
    """找不到字体时 Pillow 路径应当优雅失败（返回 False，不抛异常）。"""
    monkeypatch.setattr(
        AccountCardRenderer, "_find_font", staticmethod(lambda size, bold=False: None)
    )
    ok = AccountCardRenderer._pillow_settlement(
        AccountCardRenderer._ordered_settlement_sections(SECTIONS),
        "t",
        "s",
        "n",
        tmp_path / "none.png",
    )
    assert ok is False
    assert not (tmp_path / "none.png").exists()


def test_settlement_pillow_with_no_rows(tmp_path, monkeypatch):
    """没有行时也应产出可读的空态图，而不是崩溃。"""
    from PIL import ImageFont

    monkeypatch.setattr(
        AccountCardRenderer,
        "_find_font",
        staticmethod(lambda size, bold=False: ImageFont.load_default()),
    )
    ok = AccountCardRenderer._pillow_settlement(
        {},
        "Empty",
        "0 participants",
        "",
        tmp_path / "empty.png",
    )
    assert ok is True
