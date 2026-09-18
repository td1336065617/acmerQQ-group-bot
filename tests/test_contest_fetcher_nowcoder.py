"""牛客赛程抓取范围测试：OJ 归属判定、月份窗口、分级容错、洛谷翻页保险。

对应方案 `docs/牛客数据获取范围优化方案.md` §1.9。
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contest_fetcher import (
    NOWCODER_SCOPE_ALL,
    NOWCODER_SCOPE_SERIES_ONLY,
    ContestFetcher,
)
from src.models import Contest


def _item(
    contest_id: int,
    name: str,
    oj_name: str,
    start_ms: int,
    end_ms: int | None = None,
    link: str = "https://ac.nowcoder.com/acm/contest/1",
) -> dict:
    """构造一条牛客日历条目。"""
    return {
        "contestId": contest_id,
        "contestName": name,
        "ojName": oj_name,
        "link": link,
        "startTime": start_ms,
        "endTime": end_ms if end_ms is not None else start_ms + 3600_000,
    }


def _ms(year: int, month: int, day: int, hour: int = 12) -> int:
    return int(
        datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp() * 1000
    )


# ----------------------------------------------------------------------
# 月份窗口
# ----------------------------------------------------------------------


def test_nc_months_spans_year_boundary():
    months = ContestFetcher._nc_months(
        datetime(2026, 12, 5, tzinfo=timezone.utc), lookahead=3
    )
    assert months == ["2026-12", "2027-1", "2027-2", "2027-3"]


def test_nc_months_zero_lookahead_is_current_month_only():
    months = ContestFetcher._nc_months(
        datetime(2026, 9, 18, tzinfo=timezone.utc), lookahead=0
    )
    assert months == ["2026-9"]


# ----------------------------------------------------------------------
# OJ 归属判定
# ----------------------------------------------------------------------


def test_platform_of_item_matches_nowcoder_by_oj_name():
    # 高校校赛名称里没有“牛客”，旧口径会把它丢掉
    assert (
        ContestFetcher._nc_platform_of_item(
            _item(1, "河南萌新联赛2026第（三）场：郑州轻工业大学", "NowCoder", 0)
        )
        == "nowcoder"
    )


def test_platform_of_item_classifies_other_ojs():
    assert (
        ContestFetcher._nc_platform_of_item(
            _item(2, "AtCoder Beginner Contest 476", "AtCoder", 0)
        )
        == "atcoder"
    )
    # CodeForces 由官方 API 提供（更全），不参与牛客列表
    assert (
        ContestFetcher._nc_platform_of_item(
            _item(3, "Codeforces Round 1122 (Div. 3)", "CodeForces", 0)
        )
        is None
    )


def test_platform_of_item_falls_back_to_name_when_oj_missing():
    assert (
        ContestFetcher._nc_platform_of_item(_item(4, "牛客周赛 Round 162", "", 0))
        == "nowcoder"
    )
    assert (
        ContestFetcher._nc_platform_of_item(
            _item(5, "AtCoder Regular Contest 230", "", 0)
        )
        == "atcoder"
    )
    assert ContestFetcher._nc_platform_of_item(_item(6, "未知比赛", "", 0)) is None


# ----------------------------------------------------------------------
# 多月份合并 / 分级容错
# ----------------------------------------------------------------------


def test_nowcoder_calendar_merges_months_and_dedupes(monkeypatch):
    async def scenario():
        fetcher = ContestFetcher(cache_path="/tmp/nonexistent-nc-test.json")
        fetcher.session = object()  # 仅用于绕过 session 检查
        calls = []

        async def fake_month(month: str):
            calls.append(month)
            return {
                "2026-9": [
                    # 名称不含“牛客”的校赛必须保留
                    _item(101, "河南萌新联赛：郑州轻工业大学", "NowCoder", _ms(2026, 9, 20)),
                    _item(102, "牛客周赛 Round 162", "NowCoder", _ms(2026, 9, 21)),
                    _item(201, "AtCoder Beginner Contest 476", "AtCoder", _ms(2026, 9, 19)),
                    _item(301, "Codeforces Round 1122", "CodeForces", _ms(2026, 9, 22)),
                ],
                # 跨月重复的同一条比赛只应保留一次
                "2026-10": [
                    _item(102, "牛客周赛 Round 162", "NowCoder", _ms(2026, 9, 21)),
                    _item(103, "2026牛客国庆集训派对day1", "NowCoder", _ms(2026, 10, 1)),
                ],
            }.get(month, [])

        monkeypatch.setattr(fetcher, "_fetch_nc_month", fake_month)
        monkeypatch.setattr(
            fetcher, "_nc_months", lambda *a, **k: ["2026-9", "2026-10"]
        )
        contests = await fetcher._fetch_nowcoder_calendar("nowcoder")

        assert calls == ["2026-9", "2026-10"]
        assert [c.contest_id for c in contests] == ["101", "102", "103"]
        # 按开始时间升序
        assert [c.start_time for c in contests] == sorted(
            c.start_time for c in contests
        )
        assert contests[0].platform == "nowcoder"
        assert contests[0].name.startswith("河南萌新联赛")

    asyncio.run(scenario())


def test_required_month_failure_raises(monkeypatch):
    async def scenario():
        fetcher = ContestFetcher(cache_path="/tmp/nonexistent-nc-test.json")
        fetcher.session = object()

        async def fake_month(month: str):
            if month == "2026-9":
                raise RuntimeError("network down")
            return []

        monkeypatch.setattr(fetcher, "_fetch_nc_month", fake_month)
        monkeypatch.setattr(
            fetcher, "_nc_months", lambda *a, **k: ["2026-9", "2026-10", "2026-11"]
        )
        try:
            await fetcher._fetch_nowcoder_calendar("nowcoder")
        except RuntimeError as exc:
            assert "network down" in str(exc)
        else:  # pragma: no cover - 必需月失败必须抛出
            raise AssertionError("必需月失败时应抛出异常")

    asyncio.run(scenario())


def test_optional_month_failure_is_tolerated(monkeypatch):
    async def scenario():
        fetcher = ContestFetcher(cache_path="/tmp/nonexistent-nc-test.json")
        fetcher.session = object()

        async def fake_month(month: str):
            if month == "2026-11":
                raise RuntimeError("future month down")
            return [
                _item(101, "牛客周赛 Round 162", "NowCoder", _ms(2026, 9, 21))
            ]

        monkeypatch.setattr(fetcher, "_fetch_nc_month", fake_month)
        monkeypatch.setattr(
            fetcher, "_nc_months", lambda *a, **k: ["2026-9", "2026-10", "2026-11"]
        )
        contests = await fetcher._fetch_nowcoder_calendar("nowcoder")
        assert [c.contest_id for c in contests] == ["101"]

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 牛客赛事口径（nowcoder_scope）
# ----------------------------------------------------------------------


def test_scope_all_keeps_school_contests_and_series_only_drops_them():
    fetcher = ContestFetcher(cache_path="/tmp/nonexistent-nc-test.json")
    contests = [
        Contest(
            platform="nowcoder",
            name="河南萌新联赛2026第（三）场：郑州轻工业大学",
            start_time=datetime(2026, 9, 20, tzinfo=timezone.utc),
        ),
        Contest(
            platform="nowcoder",
            name="牛客周赛 Round 162",
            start_time=datetime(2026, 9, 21, tzinfo=timezone.utc),
        ),
    ]
    fetcher.nowcoder_scope = NOWCODER_SCOPE_ALL
    assert fetcher._apply_scope("nowcoder", contests) == contests

    fetcher.nowcoder_scope = NOWCODER_SCOPE_SERIES_ONLY
    kept = fetcher._apply_scope("nowcoder", contests)
    assert [c.name for c in kept] == ["牛客周赛 Round 162"]
    # 其他平台不受口径影响
    assert fetcher._apply_scope("codeforces", contests) == contests


def test_scope_is_applied_on_cache_hit(monkeypatch):
    async def scenario():
        fetcher = ContestFetcher(cache_path="/tmp/nonexistent-nc-test.json")
        import time

        fetcher._cache["nowcoder"] = (
            time.time(),
            [
                Contest(
                    platform="nowcoder",
                    name="某高校新生赛",
                    start_time=datetime(2026, 9, 20, tzinfo=timezone.utc),
                ),
                Contest(
                    platform="nowcoder",
                    name="牛客小白月赛137",
                    start_time=datetime(2026, 9, 21, tzinfo=timezone.utc),
                ),
            ],
        )

        async def unexpected():
            raise AssertionError("命中缓存时不应抓取")

        monkeypatch.setattr(fetcher, "_fetch_nowcoder_calendar", unexpected)
        contests, error = await fetcher.fetch_platform("nowcoder")
        assert error is None and len(contests) == 2

        fetcher.nowcoder_scope = NOWCODER_SCOPE_SERIES_ONLY
        contests, error = await fetcher.fetch_platform("nowcoder")
        assert error is None
        assert [c.name for c in contests] == ["牛客小白月赛137"]
        # 缓存里仍是全量
        assert len(fetcher._cache["nowcoder"][1]) == 2

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 洛谷翻页保险
# ----------------------------------------------------------------------


def _luogu_page(rows: list, per_page: int = 20) -> str:
    payload = {"data": {"contests": {"perPage": per_page, "result": rows}}}
    return (
        '<html><script id="lentille-context" type="application/json">'
        + json.dumps(payload, ensure_ascii=False)
        + "</script></html>"
    )


def test_luogu_fetches_second_page_only_when_needed(monkeypatch):
    async def scenario():
        import src.contest_fetcher as module

        # 洛谷页内 JSON 的 startTime/endTime 是「秒」（牛客日历是毫秒）
        now_s = int(datetime.now(timezone.utc).timestamp())
        future = [
            {
                "id": 900 + index,
                "name": f"洛谷比赛 {index}",
                "startTime": now_s + (index + 1) * 86_400,
                "endTime": now_s + (index + 1) * 86_400 + 3600,
            }
            for index in range(20)
        ]
        second_page = [
            {
                "id": 999,
                "name": "第二页比赛",
                "startTime": now_s + 30 * 86_400,
                "endTime": now_s + 30 * 86_400 + 3600,
            }
        ]
        requested: list = []

        async def fake_fetch(session, url, **kwargs):
            requested.append(url)
            if "page=2" in url:
                return _luogu_page(second_page)
            return _luogu_page(future)

        monkeypatch.setattr(module, "fetch_text_with_retry", fake_fetch)
        fetcher = ContestFetcher(cache_path="/tmp/nonexistent-nc-test.json")
        fetcher.session = object()
        contests = await fetcher._fetch_luogu()
        assert len(requested) == 2
        assert [c.contest_id for c in contests][-1] == "999"
        assert len(contests) == 21

        # 第 1 页最后一条已是过去 → 不再翻页
        requested.clear()
        past_first_page = future[:5] + [
            {
                "id": 800,
                "name": "已结束",
                "startTime": now_s - 86_400,
                "endTime": now_s - 86_400 + 3600,
            }
        ]

        async def fake_fetch_short(session, url, **kwargs):
            requested.append(url)
            return _luogu_page(past_first_page)

        monkeypatch.setattr(module, "fetch_text_with_retry", fake_fetch_short)
        contests = await fetcher._fetch_luogu()
        assert len(requested) == 1
        assert len(contests) == 5

    asyncio.run(scenario())
