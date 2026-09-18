"""资料卡排版回归：段位缩写、自适应列数与长文本不越界。

用「假字体」提供确定性度量，避免依赖服务器上装了什么字体。
"""
from __future__ import annotations

from src.account_cards import (
    CF_RANK_ABBREVIATIONS,
    AccountCardRenderer,
    _abbreviate_rank_text,
    _profile_stats,
    _rank_display_text,
)
from src.account_models import AccountProfile


class FakeFont:
    """按字符宽度估算：CJK 17px、ASCII 8.5px。"""

    @staticmethod
    def getlength(text: str) -> float:
        return sum(17.0 if ord(char) > 0x2E80 else 8.5 for char in str(text))


FONT = FakeFont()
# 单平台卡：CARD_WIDTH(1200) - 140 = 1060，内容宽 = card_w - 50
SINGLE_CONTENT_WIDTH = 1060 - 50
# 双平台卡：(1200 - 140 - gap 24) // 2 = 518
MULTI_CARD_WIDTH = (1200 - 140 - 24) // 2
MULTI_CONTENT_WIDTH = MULTI_CARD_WIDTH - 50


def test_cf_rank_abbreviations():
    assert _abbreviate_rank_text("codeforces", "international grandmaster") == "IGM"
    # 社区标准是 LGM（不是 LG）
    assert _abbreviate_rank_text("codeforces", "legendary grandmaster") == "LGM"
    assert _abbreviate_rank_text("codeforces", "Legendary Grandmaster") == "LGM"
    assert _abbreviate_rank_text("codeforces", "candidate master") == "CM"
    assert _abbreviate_rank_text("codeforces", "grandmaster") == "GM"
    # 自定义头衔（CF 允许把任意文本作为称号，实测有用户直接填用户名）
    assert _abbreviate_rank_text("codeforces", "jiangly") == "jiangly"
    # AtCoder 的颜色类映射成中文段位（避免把 CSS 类名展示给用户）
    assert _abbreviate_rank_text("atcoder", "red") == "红"
    assert _abbreviate_rank_text("atcoder", "unknown") == "unknown"
    assert _abbreviate_rank_text("luogu", "") == ""
    assert set(CF_RANK_ABBREVIATIONS) >= {"grandmaster", "international master"}


def test_rank_display_text_marks_current_rank():
    """头部段位必须带“当前段位”前缀，否则会被误读成 Rating 的值。"""
    profile = AccountProfile(
        platform="codeforces",
        handle="Petr",
        rating=2947,
        rank_text="legendary grandmaster",
        max_rank_text="legendary grandmaster",
    )
    assert _rank_display_text(profile) == "当前段位：LGM"
    stats = dict(_profile_stats(profile))
    assert stats["最高段位"] == "LGM"

    atcoder = AccountProfile(platform="atcoder", handle="tourist", color="red")
    assert _rank_display_text(atcoder) == "当前段位：红"

    blank = AccountProfile(platform="luogu", handle="123456")
    assert _rank_display_text(blank) == "当前段位：未评级"


def test_profile_stats_uses_abbreviation_for_max_rank():
    profile = AccountProfile(
        platform="codeforces",
        handle="Petr",
        rating=3000,
        max_rank_text="international grandmaster",
        rating_rank=100,
        max_rating=3200,
    )
    stats = dict(_profile_stats(profile))
    assert stats["最高段位"] == "IGM"

    custom = AccountProfile(
        platform="codeforces",
        handle="whoever",
        rating=1500,
        max_rank_text="some very long custom title here",
    )
    assert dict(_profile_stats(custom))["最高段位"] == (
        "some very long custom title here"
    )


def test_stat_columns_shrink_for_wide_cells():
    short = ["最高段位：IGM", "最高 Rating：3200", "参赛次数：120", "贡献：+5"]
    assert (
        AccountCardRenderer._pillow_stat_columns(
            short, FONT, SINGLE_CONTENT_WIDTH, preferred=4
        )
        == 4
    )
    # 自定义长头衔（40 字符左右）放不进 4 列 → 自动降列
    wide = ["最高段位：" + "x" * 40] + short[1:]
    columns = AccountCardRenderer._pillow_stat_columns(
        wide, FONT, SINGLE_CONTENT_WIDTH, preferred=4
    )
    assert columns < 4
    # 双平台卡最少 2 列，不会更少
    assert (
        AccountCardRenderer._pillow_stat_columns(
            wide, FONT, MULTI_CONTENT_WIDTH, preferred=2
        )
        == 2
    )
    # 空数据时保持 preferred
    assert (
        AccountCardRenderer._pillow_stat_columns(
            [], FONT, SINGLE_CONTENT_WIDTH, preferred=4
        )
        == 4
    )


def _layout_cells(values, font, content_width, preferred):
    columns = AccountCardRenderer._pillow_stat_columns(
        values, font, content_width, preferred=preferred
    )
    column_width = content_width / columns
    fitted = [
        AccountCardRenderer._fit_rank_pillow_text(
            value, font, column_width - 14
        )
        for value in values
    ]
    return columns, column_width, fitted


def test_long_cells_stay_inside_their_column():
    """修复前：长段位文本会越界压到相邻列。这里断言每格都在自己的列内。"""
    values = [
        "最高段位：international grandmaster",
        "最高段位：some extremely long custom title that never ends",
        "平台排名：12345",
        "参赛次数：120",
    ]
    for content_width, preferred in (
        (SINGLE_CONTENT_WIDTH, 4),
        (MULTI_CONTENT_WIDTH, 2),
    ):
        columns, column_width, fitted = _layout_cells(
            values, FONT, content_width, preferred
        )
        assert columns >= 2
        for text in fitted:
            assert AccountCardRenderer._pillow_text_width(
                text, FONT
            ) <= column_width - 14 + 0.01, (content_width, preferred, text)
        # 相邻列不重叠：第 i 格的右边界 <= 第 i+1 格的左边界
        for index in range(columns - 1):
            left = index * column_width + AccountCardRenderer._pillow_text_width(
                fitted[index], FONT
            )
            right = (index + 1) * column_width
            assert left <= right + 0.01


def test_rank_header_fits_remaining_width():
    """头部“当前段位”同样要按剩余宽度截断，不能越出卡片。"""
    for card_w, offset in ((1060, 420), (MULTI_CARD_WIDTH, 370)):
        remaining = max(0, (card_w - 18) - offset)
        fitted = AccountCardRenderer._fit_rank_pillow_text(
            "international grandmaster", FONT, remaining
        )
        assert AccountCardRenderer._pillow_text_width(
            fitted, FONT
        ) <= remaining + 0.01
        if remaining < AccountCardRenderer._pillow_text_width(
            "international grandmaster", FONT
        ):
            assert fitted.endswith("…")
