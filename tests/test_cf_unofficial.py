"""CF 打星（榜外）成员补齐测试（S2）。

覆盖：官方榜单 ∪ 计分名单的并集、比赛窗口过滤（练习提交不算）、
include_unofficial 开关、卡片行结构带出 unofficial 标记。
"""
from __future__ import annotations

import asyncio

from src.settlement import SettlementService, SettleRow


class FakeContest:
    contest_id = "2269"
    name = "Codeforces Round X"


class FakeFetcher:
    """按方法返回合成数据：standings / ratingChanges / status。"""

    def __init__(self, phase: str = "FINISHED", preliminary: bool = False, duration: int = 7200) -> None:
        self.phase = phase
        self.preliminary = preliminary
        self.duration = duration
        self.calls: list[str] = []

    async def _cf_json(self, method, params, timeout=None):
        self.calls.append(method)
        if method == "contest.standings":
            return {
                "status": "OK",
                "result": {
                    "contest": {
                        "phase": self.phase,
                        "name": "Round X",
                        "durationSeconds": self.duration,
                    },
                    "problems": [{"index": "A"}, {"index": "B"}],
                    "rows": [
                        {
                            "rank": 5,
                            "party": {"members": [{"handle": "official1"}]},
                            "problemResults": [
                                {"points": 500, "type": "FINAL"},
                                {"points": 0, "type": "FINAL"},
                            ],
                        }
                    ],
                },
            }
        if method == "contest.ratingChanges":
            return {
                "status": "OK",
                "result": [{"handle": "star1", "oldRating": 2100, "newRating": 2150}],
            }
        if method == "contest.status":
            return {
                "status": "OK",
                "result": [
                    # 比赛窗口内（relativeTime 100 ≤ 7200）
                    {"relativeTimeSeconds": 100, "verdict": "OK", "problem": {"index": "A"},
                     "author": {"participantType": "CONTESTANT", "members": [{"handle": "star1"}]}},
                    # 窗口外（8000 > 7200 + 60）→ 必须排除
                    {"relativeTimeSeconds": 8000, "verdict": "OK", "problem": {"index": "B"},
                     "author": {"participantType": "CONTESTANT", "members": [{"handle": "star1"}]}},
                    # 纯练习（时间戳极大）→ 该成员既不在榜单也不在计分名单，必须整体排除
                    {"relativeTimeSeconds": 10 ** 9, "verdict": "OK", "problem": {"index": "A"},
                     "author": {"participantType": "PRACTICE", "members": [{"handle": "prac1"}]}},
                ],
            }
        raise AssertionError("unexpected method " + method)


def test_official_and_unofficial_union():
    svc = SettlementService(FakeFetcher())
    members = [
        ("u1", "official1", "official1"),
        ("u2", "star1", "star1"),
        ("u3", "stranger", "stranger"),
    ]
    result = asyncio.run(svc.collect("codeforces", FakeContest(), members))
    assert result is not None
    by_handle = {row.handle: row for row in result.rows}
    assert set(by_handle) == {"official1", "star1"}  # 无关成员不出现
    official = by_handle["official1"]
    assert official.unofficial is False
    assert official.rank == 5
    assert official.solved == 1  # A 通过、B 未通过
    star = by_handle["star1"]
    assert star.unofficial is True
    assert star.rank is None
    assert star.solved == 1  # 只统计窗口内的 A
    assert by_handle["official1"] == result.rows[0]  # 正式行在前、打星置尾
    assert "打星" in result.note


def test_include_unofficial_can_be_disabled():
    svc = SettlementService(FakeFetcher())
    members = [("u1", "official1", "official1"), ("u2", "star1", "star1")]
    result = asyncio.run(
        svc.collect("codeforces", FakeContest(), members, include_unofficial=False)
    )
    assert result is not None
    assert [row.handle for row in result.rows] == ["official1"]
    assert all(row.unofficial is False for row in result.rows)


def test_practice_only_member_is_excluded():
    svc = SettlementService(FakeFetcher())
    members = [("u1", "official1", "official1"), ("u9", "prac1", "prac1")]
    result = asyncio.run(svc.collect("codeforces", FakeContest(), members))
    assert result is not None
    assert [row.handle for row in result.rows] == ["official1"]


def test_rating_changes_and_status_are_cached():
    fetcher = FakeFetcher()
    svc = SettlementService(fetcher)
    members = [("u1", "official1", "official1"), ("u2", "star1", "star1")]
    asyncio.run(svc.collect("codeforces", FakeContest(), members))
    first_calls = list(fetcher.calls)
    asyncio.run(svc.collect("codeforces", FakeContest(), members, include_unofficial=True))
    # 结果缓存命中 → 不再产生新的网络调用
    assert fetcher.calls == first_calls


def test_card_row_carries_unofficial_flag():
    row = SettleRow(
        platform="codeforces", user_id="u", display_name="打星甲", handle="star1",
        rank=None, solved=2, total_problems=6, unofficial=True,
    )
    card_row = row.to_card_row()
    assert card_row["unofficial"] is True
    assert card_row["rank"] is None
    official = SettleRow(platform="codeforces", user_id="v", display_name="正式", handle="ok")
    assert official.to_card_row()["unofficial"] is False
