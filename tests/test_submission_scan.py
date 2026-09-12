"""CF/AtCoder 提交扫描上限与翻页语义测试。"""
from __future__ import annotations

import asyncio

from src.account_fetcher import (
    ANALYSIS_CACHE_VERSION,
    ATCODER_SUBMISSION_SCAN_LIMIT,
    CF_SUBMISSION_PAGE_SIZE,
    CF_SUBMISSION_SCAN_LIMIT,
    AccountFetcher,
)


def test_analysis_cache_version_invalidates_old_entries():
    """提升扫描上限后，旧的持久化分析缓存必须被忽略，不能继续显示旧口径。"""
    assert ANALYSIS_CACHE_VERSION >= 2
    # 新版本 kind 可以正常还原 key
    key = AccountFetcher._key_from_kind(
        "codeforces", "maspy", f"analysis_v{ANALYSIS_CACHE_VERSION}"
    )
    assert key == ("codeforces", "maspy", True, False, True)
    key_s = AccountFetcher._key_from_kind(
        "codeforces", "maspy", f"analysis_s_v{ANALYSIS_CACHE_VERSION}"
    )
    assert key_s == ("codeforces", "maspy", True, True, True)
    # 旧版 kind（无版本号 / 旧版本号）被丢弃
    assert AccountFetcher._key_from_kind("codeforces", "maspy", "analysis") is None
    assert (
        AccountFetcher._key_from_kind("codeforces", "maspy", "analysis_s") is None
    )
    assert (
        AccountFetcher._key_from_kind(
            "codeforces", "maspy", f"analysis_v{ANALYSIS_CACHE_VERSION - 1}"
        )
        is None
    )
    # 非分析类缓存不受版本影响
    assert AccountFetcher._key_from_kind("codeforces", "x", "basic") == (
        "codeforces",
        "x",
        False,
        False,
        False,
    )


def test_kind_roundtrip_for_analysis():
    for key in (
        ("codeforces", "x", True, False, True),
        ("codeforces", "x", True, True, True),
    ):
        kind = AccountFetcher._kind_for_key(key)
        assert kind.startswith("analysis")
        assert AccountFetcher._key_from_kind(key[0], key[1], kind) == key


def test_scan_limits_raised():
    # LGM/重度选手提交数可超过 2 万，上限需明显高于旧的 10000
    assert CF_SUBMISSION_SCAN_LIMIT == 50000
    assert ATCODER_SUBMISSION_SCAN_LIMIT == 20000
    assert CF_SUBMISSION_PAGE_SIZE == 10000
    assert CF_SUBMISSION_SCAN_LIMIT % CF_SUBMISSION_PAGE_SIZE == 0


class _FakeFetcher(AccountFetcher):
    """按需返回分页结果的假抓取器。"""

    def __init__(self, total: int, page_size: int = CF_SUBMISSION_PAGE_SIZE):
        super().__init__()
        self.total = total
        self.page_size = page_size
        self.calls: list[int] = []

    async def _cf_json(self, method, params):  # type: ignore[override]
        offset = int(params["from"])
        count = int(params["count"])
        self.calls.append(offset)
        remaining = max(0, self.total - (offset - 1))
        rows = [
            {"id": offset + index, "verdict": "OK"}
            for index in range(min(count, remaining))
        ]
        return {"status": "OK", "result": rows}


def test_small_account_uses_single_request():
    async def scenario():
        fetcher = _FakeFetcher(total=3000)
        rows, scanned_all = await fetcher._cf_scan_submissions("someone")
        assert len(rows) == 3000
        assert scanned_all is True
        assert fetcher.calls == [1]  # 一页就够，和旧实现一样只有一次请求

    asyncio.run(scenario())


def test_heavy_account_paginates_until_short_page():
    async def scenario():
        fetcher = _FakeFetcher(total=21459)
        rows, scanned_all = await fetcher._cf_scan_submissions("maspy")
        assert len(rows) == 21459
        assert scanned_all is True
        # 10000 + 10000 + 1459 → 3 页
        assert fetcher.calls == [1, 10001, 20001]

    asyncio.run(scenario())


def test_scan_stops_at_limit_and_marks_incomplete():
    async def scenario():
        fetcher = _FakeFetcher(total=CF_SUBMISSION_SCAN_LIMIT + 5000)
        rows, scanned_all = await fetcher._cf_scan_submissions("huge")
        assert len(rows) == CF_SUBMISSION_SCAN_LIMIT
        assert scanned_all is False  # 达到上限，未读完
        assert fetcher.calls == [1, 10001, 20001, 30001, 40001]

    asyncio.run(scenario())


def test_coverage_text_matches_completeness():
    rows = [{"verdict": "OK"}] * 15000
    # 读完：不应再出现“最多读取”这种自相矛盾的提示
    done = AccountFetcher._build_cf_analysis(
        rows, [], 9000, scanned_all=True
    )
    assert "已读完当前公开记录" in done["coverage"]
    assert "最多读取" not in done["coverage"]
    assert done["submission_count"] == 15000

    # 被上限截断：给出上限提示
    cut = AccountFetcher._build_cf_analysis(
        rows, [], 9000, scanned_all=False
    )
    assert f"最多读取 {CF_SUBMISSION_SCAN_LIMIT} 条" in cut["coverage"]
