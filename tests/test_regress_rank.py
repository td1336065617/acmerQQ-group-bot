"""本周退步榜：指令解析与行筛选/排序测试。"""
from __future__ import annotations

from test_main_accounts import _load_main_module

bot_main = _load_main_module()

GROUP_RANK_COMMANDS = bot_main.GROUP_RANK_COMMANDS
parse_group_rank_command = bot_main.parse_group_rank_command
regress_rows = bot_main.AcmerGroupBot._regress_rows


def test_regress_command_aliases():
    for command in ("本周退步榜", "本周掉分榜", "退步榜"):
        assert GROUP_RANK_COMMANDS[command] == "regress"
    # 进步榜与平台排行不受影响
    assert GROUP_RANK_COMMANDS["本周进步榜"] == "progress"
    assert GROUP_RANK_COMMANDS["群cf排行"] == "codeforces"
    assert GROUP_RANK_COMMANDS["群排行"] is None


def test_parse_regress_command_with_and_without_page():
    assert parse_group_rank_command("本周退步榜") == ("regress", 1)
    assert parse_group_rank_command("退步榜") == ("regress", 1)
    assert parse_group_rank_command("本周掉分榜") == ("regress", 1)
    # 总览类指令带页码也解析为 regress（页码在总览分支被忽略）
    assert parse_group_rank_command("本周退步榜 2") == ("regress", 2)
    assert parse_group_rank_command("本周进步榜") == ("progress", 1)


def _row(name: str, delta, rating=1500):
    return {
        "user_id": f"u_{name}",
        "display_name": name,
        "handle": name.lower(),
        "value": delta,
        "display_value": f"{delta:+d}" if isinstance(delta, int) else "—",
        "delta": delta,
        "rating": rating,
        "current_display_value": str(rating),
    }


def test_regress_keeps_only_drops_sorted_by_biggest_loss():
    rows = [
        _row("进步王", 120),
        _row("小跌", -30),
        _row("持平", 0),
        _row("暴跌", -210),
        _row("无基线", None),
        _row("中跌", -75),
    ]
    result = regress_rows(rows)
    assert [r["display_name"] for r in result] == ["暴跌", "中跌", "小跌"]
    assert [r["delta"] for r in result] == [-210, -75, -30]
    # sort_value 被改写为按“下滑幅度”排序用的值
    assert [r["sort_value"] for r in result] == [-210, -75, -30]


def test_regress_ignores_malformed_rows():
    rows = [
        _row("正常", -10),
        {"display_name": "缺字段"},
        {"display_name": "脏数据", "delta": "abc"},
        "not-a-dict",
        None,
        _row("持平", 0),
        _row("上涨", 5),
    ]
    result = regress_rows(rows)
    assert [r["display_name"] for r in result] == ["正常"]


def test_regress_empty_and_none_input():
    assert regress_rows([]) == []
    assert regress_rows(None) == []
    # 全是上涨 → 退步榜为空（对应“本周没有成员 Rating 下降”）
    assert regress_rows([_row("涨", 10), _row("涨2", 3)]) == []


def test_regress_does_not_mutate_input_rows():
    original = _row("小跌", -30)
    rows = [original]
    result = regress_rows(rows)
    assert result[0] is not original
    assert result[0]["sort_value"] == -30
    # 原始行不应被改写（避免污染进步榜/其他调用方的数据）
    assert "sort_value" not in original


def test_regress_ties_sorted_by_name():
    rows = [_row("b选手", -50), _row("a选手", -50)]
    result = regress_rows(rows)
    assert [r["display_name"] for r in result] == ["a选手", "b选手"]
