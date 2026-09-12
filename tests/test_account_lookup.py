"""未绑定用户战绩查询：指令解析、平台能力边界与防滥用。"""
from __future__ import annotations

from src.account_fetcher import normalize_account_identifier
from test_main_accounts import _load_main_module

bot_main = _load_main_module()

LOOKUP_RE = bot_main.ACCOUNT_LOOKUP_RE
USAGE_RE = bot_main.ACCOUNT_LOOKUP_USAGE_RE
DETAIL_RE = bot_main.ACCOUNT_LOOKUP_DETAIL_RE


def _parse(text: str):
    match = LOOKUP_RE.match(text)
    if not match:
        return None
    return match.group(1), match.group(2)


def test_lookup_command_accepts_aliases_and_optional_space():
    assert _parse("查询cf td1336065617") == ("cf", "td1336065617")
    assert _parse("查询 cf td1336065617") == ("cf", "td1336065617")
    assert _parse("查codeforces jiangly") == ("codeforces", "jiangly")
    assert _parse("查询atcoder tourist") == ("atcoder", "tourist")
    assert _parse("查询牛客 12345678") == ("牛客", "12345678")
    assert _parse("查询lg 123456") == ("lg", "123456")


def test_lookup_command_does_not_shadow_existing_commands():
    # 精确指令「查询战绩」不能被新前缀指令吃掉
    assert _parse("查询战绩") is None
    assert _parse("我的战绩") is None
    assert _parse("cf比赛") is None
    # 绑定指令走自己的前缀
    assert _parse("绑定cf td1336065617") is None
    # 只给前缀（没有标识）不由主正则匹配，而是走用法提示
    assert _parse("查询cf") is None
    assert USAGE_RE.match("查询cf") is not None
    assert USAGE_RE.match("查询 洛谷") is not None


def test_lookup_detail_suffix():
    match = DETAIL_RE.match("td1336065617 详细")
    assert match is not None and match.group("target") == "td1336065617"
    assert DETAIL_RE.match("td1336065617") is None


def test_lookup_identifier_normalization_per_platform():
    # CF / AtCoder：用户名与主页链接都可用
    assert (
        normalize_account_identifier("codeforces", "td1336065617")
        == "td1336065617"
    )
    assert (
        normalize_account_identifier(
            "codeforces", "https://codeforces.com/profile/jiangly"
        )
        == "jiangly"
    )
    assert normalize_account_identifier("atcoder", "tourist") == "tourist"
    assert (
        normalize_account_identifier(
            "atcoder", "https://atcoder.jp/users/tourist"
        )
        == "tourist"
    )
    # 牛客 / 洛谷：只接受数字 UID 或主页链接
    assert normalize_account_identifier("nowcoder", "12345678") == "12345678"
    assert (
        normalize_account_identifier(
            "nowcoder",
            "https://ac.nowcoder.com/acm/contest/profile/12345678",
        )
        == "12345678"
    )
    assert normalize_account_identifier("luogu", "123456") == "123456"
    assert (
        normalize_account_identifier(
            "luogu", "https://www.luogu.com.cn/user/123456"
        )
        == "123456"
    )
    # 用户名在这两个平台无法解析（公开接口不支持用户名查询）
    assert normalize_account_identifier("nowcoder", "someuser") == ""
    assert normalize_account_identifier("luogu", "someuser") == ""


def test_lookup_uid_only_platforms_hint():
    for platform in ("nowcoder", "luogu"):
        assert platform in bot_main.ACCOUNT_LOOKUP_UID_ONLY
        usage = bot_main.AcmerGroupBot._account_lookup_usage(platform)
        assert "不支持用户名" in usage
        hint = bot_main.AcmerGroupBot._account_lookup_invalid_hint(platform)
        assert "不支持用户名查询" in hint
    cf_hint = bot_main.AcmerGroupBot._account_lookup_invalid_hint("codeforces")
    assert "不支持用户名" not in cf_hint


def test_lookup_rate_verdict_cooldown_and_group_window():
    verdict = bot_main.AcmerGroupBot._lookup_rate_verdict
    cooldown = bot_main.LOOKUP_USER_COOLDOWN_SECONDS
    window = bot_main.LOOKUP_GROUP_WINDOW_SECONDS
    limit = bot_main.LOOKUP_GROUP_MAX_PER_WINDOW

    # 首次：放行
    assert verdict(1000.0, 0.0, []) == (True, 0)
    # 冷却内：拦截，且给出等待秒数
    allowed, wait = verdict(1000.0, 1000.0 - cooldown / 2, [])
    assert allowed is False and wait >= 1
    # 冷却结束后：放行
    assert verdict(1000.0, 1000.0 - cooldown - 1, [])[0] is True
    # 群窗口内达到上限：拦截
    times = [1000.0 - i for i in range(limit)]
    allowed, wait = verdict(1000.0, 0.0, times)
    assert allowed is False and wait >= 1
    # 窗口外的旧记录被忽略
    stale = [1000.0 - window - 5 for _ in range(limit)]
    assert verdict(1000.0, 0.0, stale)[0] is True
