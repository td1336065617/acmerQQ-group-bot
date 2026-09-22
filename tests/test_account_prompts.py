"""绑定/查询提示文案：必须带具体例子。"""
from __future__ import annotations

from test_main_accounts import _load_main_module

bot_main = _load_main_module()


def test_bind_help_has_example():
    text = bot_main.AcmerGroupBot._account_platform_help()
    assert "例如" in text
    assert "绑定cf jiangly" in text


def test_lookup_usage_has_example():
    text = bot_main.AcmerGroupBot._account_lookup_usage("codeforces")
    assert "例如" in text
    assert "查询cf jiangly" in text


def test_example_tables_cover_all_platforms():
    for platform in ("codeforces", "nowcoder", "luogu", "atcoder"):
        assert bot_main.ACCOUNT_BIND_EXAMPLES[platform].startswith("绑定")
        assert bot_main.ACCOUNT_LOOKUP_EXAMPLES[platform].startswith("查询")


def test_menu_mentions_bind_example():
    assert "绑定cf jiangly" in bot_main.MENU_TEXT
