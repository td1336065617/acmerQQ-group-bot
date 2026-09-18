"""赛后赛果采集测试（A1）。

对应实现文档 `docs/下一阶段功能实现文档.md` §A1.5（12 例）。
全部使用假数据，不访问网络。
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models import Contest
from src.settlement import SettlementService


def _contest(
    platform: str,
    contest_id: str,
    name: str,
    *,
    hours_ago: float = 0.5,
    duration_minutes: int = 120,
) -> Contest:
    start = datetime.now(timezone.utc) - timedelta(
        hours=hours_ago, minutes=duration_minutes
    )
    return Contest(
        platform=platform,
        name=name,
        start_time=start,
        end_time=start + timedelta(minutes=duration_minutes),
        duration_minutes=duration_minutes,
        url=f"https://example.com/{contest_id}",
        contest_id=contest_id,
    )


class FakeFetcher:
    """最小可用的 AccountFetcher 替身：记录调用、返回预设数据。"""

    def __init__(self, **payloads):
        self.cf = payloads.get("cf")
        self.atcoder = payloads.get("atcoder")
        self.nowcoder = payloads.get("nowcoder", {})       # {uid: payload}
        self.profiles = payloads.get("profiles", {})        # {handle: profile}
        self.cf_calls: list[dict] = []
        self.cf_timeouts: list[float] = []
        self.atcoder_urls: list[str] = []
        self.nowcoder_urls: list[str] = []
        self.fail_nowcoder: set[str] = set()

    async def _cf_json(self, method, params, *, timeout=10.0):
        self.cf_calls.append(dict(params))
        self.cf_timeouts.append(timeout)
        if isinstance(self.cf, Exception):
            raise self.cf
        return self.cf

    async def _fetch_json(self, url, **kwargs):
        self.atcoder_urls.append(url)
        if isinstance(self.atcoder, Exception):
            raise self.atcoder
        return self.atcoder

    async def _fetch_text(self, url, *, headers=None, retries=2, timeout=10.0):
        self.nowcoder_urls.append(url)
        uid = ""
        for part in url.split("&"):
            if part.endswith(part) and "uid=" in part:
                uid = part.split("uid=")[-1].split("&")[0]
        if uid in self.fail_nowcoder:
            raise RuntimeError("mock nowcoder failure")
        return json.dumps(self.nowcoder.get(uid, {"data": {"dataList": []}}))

    async def get_profile(self, platform, identifier, **kwargs):
        if identifier in self.profiles:
            return self.profiles[identifier]
        raise RuntimeError("mock profile failure")


def _cf_standings_payload():
    def row(handle, rank, points_flags, prelim=False):
        return {
            "party": {"members": [{"handle": handle}]},
            "rank": rank,
            "points": 100.0,
            "problemResults": [
                {
                    "points": 100.0 if flag else 0.0,
                    "type": "PRELIMINARY" if (prelim and not flag) else "FINAL",
                }
                for flag in points_flags
            ],
        }

    return {
        "status": "OK",
        "result": {
            "contest": {"id": 2264, "name": "Codeforces Round 1121 (Div. 2)"},
            "problems": [
                {"index": "A"},
                {"index": "B"},
                {"index": "C"},
                {"index": "D"},
            ],
            "rows": [
                row("Alice", 120, [True, True, True, False]),
                row("bob", 431, [True, True, False, False], prelim=True),
                row("outsider", 1, [True, True, True, True]),
            ],
        },
    }


def test_codeforces_rows_are_matched_and_ranked():
    async def scenario():
        fetcher = FakeFetcher(cf=_cf_standings_payload())
        service = SettlementService(fetcher)
        members = [
            ("u1", "张三", "Alice"),
            ("u2", "李四", "BOB"),
            ("u3", "王五", "carol"),      # 不在榜单里
        ]
        result = await service.collect("codeforces", _contest("codeforces", "2264", "R1121"), members)
        assert result is not None
        assert [row.user_id for row in result.rows] == ["u1", "u2"]      # 按名次升序
        assert result.rows[0].rank == 120 and result.rows[0].solved == 3
        assert result.rows[0].unsolved == ["D"]
        assert result.rows[0].user_count == 3
        # 大小写不敏感匹配
        assert result.rows[1].handle == "BOB"
        assert "重测中" in result.note

    asyncio.run(scenario())


def test_codeforces_request_carries_only_contest_id():
    async def scenario():
        fetcher = FakeFetcher(cf=_cf_standings_payload())
        service = SettlementService(fetcher)
        await service.collect(
            "codeforces",
            _contest("codeforces", "2264", "R1121"),
            [("u1", "张三", "Alice")],
        )
        assert fetcher.cf_calls == [{"contestId": "2264"}]
        assert fetcher.cf_timeouts and fetcher.cf_timeouts[0] > 10

    asyncio.run(scenario())


def test_codeforces_without_preliminary_has_no_warning():
    async def scenario():
        payload = _cf_standings_payload()
        for row in payload["result"]["rows"]:
            for item in row["problemResults"]:
                item["type"] = "FINAL"
        fetcher = FakeFetcher(cf=payload)
        service = SettlementService(fetcher)
        result = await service.collect(
            "codeforces",
            _contest("codeforces", "2264", "R1121"),
            [("u1", "张三", "Alice")],
        )
        assert "重测中" not in result.note

    asyncio.run(scenario())


def test_codeforces_no_matching_member_returns_none():
    async def scenario():
        fetcher = FakeFetcher(cf=_cf_standings_payload())
        service = SettlementService(fetcher)
        result = await service.collect(
            "codeforces",
            _contest("codeforces", "2264", "R1121"),
            [("u9", "路人", "nobody")],
        )
        assert result is None

    asyncio.run(scenario())


def test_codeforces_api_failure_returns_none():
    async def scenario():
        fetcher = FakeFetcher(cf=RuntimeError("boom"))
        service = SettlementService(fetcher)
        result = await service.collect(
            "codeforces",
            _contest("codeforces", "2264", "R1121"),
            [("u1", "张三", "Alice")],
        )
        assert result is None

    asyncio.run(scenario())


def test_atcoder_place_matching():
    async def scenario():
        payload = [
            {"UserName": "tourist", "Place": 3, "IsRated": True},
            {"UserName": "alice", "Place": 812, "IsRated": True},
        ]
        fetcher = FakeFetcher(atcoder=payload)
        service = SettlementService(fetcher)
        result = await service.collect(
            "atcoder",
            _contest("atcoder", "abc474", "AtCoder Beginner Contest 474"),
            [("u1", "Alice", "alice"), ("u2", "Bob", "bob")],
        )
        assert result is not None
        assert len(result.rows) == 1
        assert result.rows[0].rank == 812
        assert result.rows[0].user_count == 2
        assert result.rows[0].solved is None          # 该平台不提供题目级结果
        assert fetcher.atcoder_urls == [
            "https://atcoder.jp/contests/abc474/results/json"
        ]

    asyncio.run(scenario())


def test_atcoder_no_match_returns_none():
    async def scenario():
        fetcher = FakeFetcher(atcoder=[{"UserName": "someone", "Place": 1}])
        service = SettlementService(fetcher)
        result = await service.collect(
            "atcoder",
            _contest("atcoder", "abc474", "ABC474"),
            [("u1", "Alice", "alice")],
        )
        assert result is None

    asyncio.run(scenario())


def test_nowcoder_history_row_is_used():
    async def scenario():
        payload = {
            "data": {
                "dataList": [
                    {
                        "contestId": 133885,
                        "contestName": "2026牛客暑期多校训练营10",
                        "rank": 1,
                        "userCount": 1273,
                        "acceptedCount": 13,
                        "problemCount": 13,
                        "ratingStatus": "FINISHED",
                        "changeValue": 9.0,
                    }
                ]
            }
        }
        fetcher = FakeFetcher(nowcoder={"886965097": payload})
        service = SettlementService(fetcher)
        result = await service.collect(
            "nowcoder",
            _contest("nowcoder", "133885", "2026牛客暑期多校训练营10"),
            [("u1", "HoMaMaOvO", "886965097")],
        )
        assert result is not None
        row = result.rows[0]
        assert (row.rank, row.user_count, row.solved, row.total_problems) == (
            1,
            1273,
            13,
            13,
        )
        # 走的是 /acm-heavy/ 路径（同名 /acm/ 路径没有 rank/ac 字段）
        assert fetcher.nowcoder_urls
        assert "/acm-heavy/acm/contest/profile/contest-joined-history" in fetcher.nowcoder_urls[0]
        assert "contestEndFilter=true" in fetcher.nowcoder_urls[0]

    asyncio.run(scenario())


def test_nowcoder_missing_contest_returns_none_for_retry():
    async def scenario():
        payload = {"data": {"dataList": [{"contestId": 1, "contestName": "旧比赛"}]}}
        fetcher = FakeFetcher(nowcoder={"886965097": payload})
        service = SettlementService(fetcher)
        result = await service.collect(
            "nowcoder",
            _contest("nowcoder", "133885", "新比赛"),
            [("u1", "HoMaMaOvO", "886965097")],
        )
        assert result is None       # 未就绪 → 调用方重试，不写幂等键

    asyncio.run(scenario())


def test_nowcoder_member_failure_is_isolated():
    async def scenario():
        ok_payload = {
            "data": {
                "dataList": [
                    {
                        "contestId": 133885,
                        "contestName": "多校10",
                        "rank": 5,
                        "userCount": 100,
                        "acceptedCount": 6,
                        "problemCount": 13,
                    }
                ]
            }
        }
        fetcher = FakeFetcher(nowcoder={"111": ok_payload, "222": ok_payload})
        fetcher.fail_nowcoder = {"222"}
        service = SettlementService(fetcher)
        result = await service.collect(
            "nowcoder",
            _contest("nowcoder", "133885", "多校10"),
            [("u1", "A", "111"), ("u2", "B", "222")],
        )
        assert result is not None
        assert [row.user_id for row in result.rows] == ["u1"]

    asyncio.run(scenario())


class _Profile:
    def __init__(self, rating_history):
        self.rating_history = rating_history


def test_luogu_only_reports_joined_members():
    async def scenario():
        contest = _contest("luogu", "273413", "【LGR-304-Div.2】洛谷 10 月月赛 II", hours_ago=1)
        profiles = {
            "100": _Profile(
                [{"name": "【LGR-304-Div.2】洛谷 10 月月赛 II", "timestamp": 0}]
            ),
            "200": _Profile([{"name": "别的比赛", "timestamp": 0}]),
        }
        fetcher = FakeFetcher(profiles=profiles)
        service = SettlementService(fetcher)
        result = await service.collect(
            "luogu",
            contest,
            [("u1", "张三", "100"), ("u2", "李四", "200")],
        )
        assert result is not None
        assert result.rows == []
        assert "张三" in result.extra_note and "李四" not in result.extra_note
        assert result.has_content() is True

    asyncio.run(scenario())


def test_luogu_by_time_window_when_name_differs():
    async def scenario():
        contest = _contest("luogu", "273413", "某月赛", hours_ago=1, duration_minutes=120)
        moment = (contest.end_time - timedelta(minutes=30)).timestamp()
        fetcher = FakeFetcher(profiles={"100": _Profile([{"name": "不同名字", "timestamp": moment}])})
        service = SettlementService(fetcher)
        result = await service.collect(
            "luogu", contest, [("u1", "张三", "100")]
        )
        assert result is not None and "张三" in result.extra_note

    asyncio.run(scenario())


def test_result_cache_avoids_second_fetch():
    async def scenario():
        fetcher = FakeFetcher(cf=_cf_standings_payload())
        service = SettlementService(fetcher)
        contest = _contest("codeforces", "2264", "R1121")
        members = [("u1", "张三", "Alice")]
        first = await service.collect("codeforces", contest, members)
        second = await service.collect("codeforces", contest, members)
        assert first is not None and second is first
        assert len(fetcher.cf_calls) == 1        # 第二次命中缓存

    asyncio.run(scenario())


def test_unknown_platform_or_empty_members_returns_none():
    async def scenario():
        service = SettlementService(FakeFetcher())
        assert await service.collect("offline", _contest("offline", "1", "x"), [("u", "n", "h")]) is None
        assert await service.collect("codeforces", _contest("codeforces", "1", "x"), []) is None

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 最近比赛记录（赛程接口只返回未开始的比赛，结束后会从列表消失）
# ----------------------------------------------------------------------


def test_remember_and_select_candidates():
    from src.settlement import SettlementService

    async def scenario():
        service = SettlementService(FakeFetcher())
        now = datetime.now(timezone.utc)
        # 已结束 12 分钟（>10 分钟延迟，<2 小时窗口）→ 应成为候选
        ended = _contest("codeforces", "1", "刚结束", hours_ago=0.2)
        # 已结束 3 小时 → 超窗
        old = _contest("codeforces", "2", "很久以前", hours_ago=3)
        # 还没开始 → 不是候选
        future = _contest("codeforces", "3", "未开始", hours_ago=-1)
        service.remember_contests("codeforces", [ended, old, future], now=now.timestamp())
        candidates = service.settlement_candidates("codeforces", now, 10)
        assert [c.contest_id for c in candidates] == ["1"]
        # 其他平台不受影响
        assert service.settlement_candidates("atcoder", now, 10) == []

    asyncio.run(scenario())


def test_recent_contests_survive_restart(tmp_path):
    async def scenario():
        service = SettlementService(FakeFetcher())
        now = datetime.now(timezone.utc)
        service.remember_contests(
            "codeforces", [_contest("codeforces", "9", "重启前见过", hours_ago=0.2)],
            now=now.timestamp(),
        )
        path = tmp_path / "settle_recent.json"
        service.save_recent_contests(path)
        assert path.is_file()

        restored = SettlementService(FakeFetcher())
        assert restored.load_recent_contests(path) == 1
        assert [
            c.contest_id
            for c in restored.settlement_candidates("codeforces", now, 10)
        ] == ["9"]

    asyncio.run(scenario())


def test_recent_contests_pruned_when_too_old():
    from src.settlement import RECENT_CONTEST_KEEP_SECONDS

    async def scenario():
        service = SettlementService(FakeFetcher())
        now = datetime.now(timezone.utc)
        stale = _contest(
            "codeforces",
            "5",
            "两天前",
            hours_ago=RECENT_CONTEST_KEEP_SECONDS / 3600 + 5,
        )
        service.remember_contests("codeforces", [stale], now=now.timestamp())
        assert service._recent == {}
        assert service.settlement_candidates("codeforces", now, 10) == []

    asyncio.run(scenario())


def test_end_time_inferred_from_duration_when_missing():
    async def scenario():
        service = SettlementService(FakeFetcher())
        now = datetime.now(timezone.utc)
        contest = _contest("nowcoder", "77", "无 end_time", hours_ago=0.2)
        contest.end_time = None
        service.remember_contests("nowcoder", [contest], now=now.timestamp())
        candidates = service.settlement_candidates("nowcoder", now, 10)
        assert [c.contest_id for c in candidates] == ["77"]

    asyncio.run(scenario())


def test_nowcoder_matches_real_contest_id_from_link():
    """牛客日历的 contestId 是行 ID，真实比赛 ID 在链接里，两者都要能匹配。"""

    async def scenario():
        payload = {
            "data": {
                "dataList": [
                    {
                        "contestId": 139935,          # 真实比赛 ID
                        "contestName": "牛客挑战赛91",
                        "rank": 7,
                        "userCount": 500,
                        "acceptedCount": 4,
                        "problemCount": 6,
                    }
                ]
            }
        }
        fetcher = FakeFetcher(nowcoder={"886965097": payload})
        service = SettlementService(fetcher)
        contest = _contest("nowcoder", "1139935", "牛客挑战赛91", hours_ago=3)
        contest.url = "https://ac.nowcoder.com/acm/contest/139935?from=acm_calendar"
        assert service._nowcoder_contest_ids(contest) == {"1139935", "139935"}
        result = await service.collect(
            "nowcoder", contest, [("u1", "选手", "886965097")]
        )
        assert result is not None
        assert result.rows[0].rank == 7
        assert result.rows[0].solved == 4

    asyncio.run(scenario())


def test_nowcoder_row_pick_accepts_single_id_string():
    from src.settlement import SettlementService

    history = [{"contestId": 42, "rank": 1}]
    assert SettlementService._pick_nowcoder_row(history, "42")["rank"] == 1
    assert SettlementService._pick_nowcoder_row(history, {"9", "42"})["rank"] == 1
    assert SettlementService._pick_nowcoder_row(history, {"9"}) is None
    assert SettlementService._pick_nowcoder_row(history, set()) is None
