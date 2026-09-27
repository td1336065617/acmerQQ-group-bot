"""真实数据集成测试（S5 补充）：CF 打星并集 / AtCoder 含 unrated。

用生产采集到的 fixture（CF 2269、ABC477）打通"取数 → 匹配 → 卡片行"，不访问网络。
"""
from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path
from types import SimpleNamespace

from src.settlement import SettlementService

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str):
    with gzip.open(FIXTURES / name, "rt", encoding="utf-8") as fh:
        return json.load(fh)


class CfFixtureFetcher:
    """把裁剪后的 fixture 还原成 CF API 形状（数据真实，协议层由测试合成）。"""

    def __init__(self, standings, changes, status) -> None:
        self.standings = standings
        self.changes = changes
        self.status = status
        self.calls: list[str] = []

    async def _cf_json(self, method, params, timeout=None):
        self.calls.append(method)
        if method == "contest.standings":
            rows = [
                {
                    "rank": row["rank"],
                    "party": {"members": [{"handle": h} for h in row["handles"]]},
                    "problemResults": [
                        {"points": p, "type": t} for p, t in row["pts"]
                    ],
                }
                for row in self.standings["rows"]
            ]
            return {
                "status": "OK",
                "result": {
                    "contest": {
                        "phase": self.standings["phase"],
                        "name": "Codeforces Round 1124 (Div. 2)",
                        "durationSeconds": self.standings["duration"],
                    },
                    "problems": [{"index": i} for i in self.standings["problems"]],
                    "rows": rows,
                },
            }
        if method == "contest.ratingChanges":
            return {
                "status": "OK",
                "result": [
                    {"handle": i["handle"], "oldRating": i["old"], "newRating": i["new"]}
                    for i in self.changes
                ],
            }
        if method == "contest.status":
            out = []
            for handle, info in self.status.items():
                for index in info["ok"]:
                    out.append(
                        {
                            "relativeTimeSeconds": 60,
                            "verdict": "OK",
                            "problem": {"index": index},
                            "author": {
                                "participantType": "CONTESTANT",
                                "members": [{"handle": handle}],
                            },
                        }
                    )
                if info["maxrel"] > self.standings["duration"] + 60:
                    out.append(
                        {
                            "relativeTimeSeconds": info["maxrel"],
                            "verdict": "OK",
                            "problem": {"index": "A"},
                            "author": {
                                "participantType": "PRACTICE",
                                "members": [{"handle": handle}],
                            },
                        }
                    )
            return {"status": "OK", "result": out}
        raise AssertionError("unexpected method " + method)


def test_real_fixture_cf_unofficial_union():
    standings = _load("cf_2269_standings.json.gz")
    changes = _load("cf_2269_ratingchanges.json.gz")
    status = _load("cf_2269_status.json.gz")
    official_set = {h.lower() for row in standings["rows"] for h in row["handles"]}
    official_handle = standings["rows"][0]["handles"][0]
    changed_handles = {i["handle"] for i in changes}
    star_handle = next(
        i["handle"] for i in changes if i["handle"].lower() not in official_set
    )
    practice_handle = next(
        h
        for h, info in status.items()
        if h.lower() not in official_set
        and h not in changed_handles
        and info["maxrel"] > standings["duration"] + 60
    )

    fetcher = CfFixtureFetcher(standings, changes, status)
    service = SettlementService(fetcher)
    contest = SimpleNamespace(contest_id="2269", name="Round 1124 (Div. 2)")
    members = [
        ("u1", official_handle, official_handle),
        ("u2", star_handle, star_handle),
        ("u3", practice_handle, practice_handle),
    ]
    result = asyncio.run(service.collect("codeforces", contest, members))
    assert result is not None
    by_handle = {row.handle: row for row in result.rows}

    official = by_handle[official_handle]
    assert official.unofficial is False
    assert official.rank is not None
    assert official.user_count == len(standings["rows"])

    star = by_handle[star_handle]
    assert star.unofficial is True
    assert star.rank is None
    assert "打星" in result.note
    # 打星行统一置尾
    assert result.rows[-1].handle == star_handle
    # 只有练习提交、且不在计分名单 → 不该出现
    assert practice_handle not in by_handle


class AtCoderFixtureFetcher:
    def __init__(self, rows) -> None:
        self.rows = rows
        self.calls = 0

    async def _fetch_json(self, url, timeout=None):
        self.calls += 1
        return self.rows


def test_real_fixture_atcoder_includes_unrated():
    rows = _load("atcoder_abc477_full.json.gz")
    service = SettlementService(AtCoderFixtureFetcher(rows))
    contest = SimpleNamespace(contest_id="abc477", name="UNICORN 2026 (ABC477)")
    members = [
        ("u1", "StarSilk", "StarSilk"),   # 3008 红名 → 不计分，但榜单里有他
        ("u2", "moran36", "moran36"),
        ("u3", "ghost", "ghost"),
    ]
    result = asyncio.run(service.collect("atcoder", contest, members))
    assert result is not None
    by_handle = {row.handle: row for row in result.rows}
    assert by_handle["StarSilk"].rank == 4102   # unrated 也被收录
    assert by_handle["moran36"].rank == 782
    assert by_handle["StarSilk"].user_count == len(rows)
    assert "ghost" not in by_handle


def test_probe_payload_is_reused_by_collect():
    """probe → adopt_probe → collect 不应重复抓取（且卡片与判定同源）。"""
    import asyncio
    from types import SimpleNamespace

    from src.settlement import SettlementService

    class Fetcher:
        def __init__(self, rows) -> None:
            self.rows = rows
            self.calls = 0

        async def _fetch_json(self, url, timeout=None):
            self.calls += 1
            return self.rows

    rows = _load("atcoder_abc477_full.json.gz")
    fetcher = Fetcher(rows)
    service = SettlementService(fetcher)
    contest = SimpleNamespace(contest_id="abc477", name="ABC477")
    sample = asyncio.run(service.probe("atcoder", contest, []))
    assert sample.rows == len(rows)
    assert fetcher.calls == 1
    service.adopt_probe("atcoder", "abc477", sample)
    result = asyncio.run(
        service.collect("atcoder", contest, [("u1", "StarSilk", "StarSilk")])
    )
    assert result is not None
    assert result.rows[0].rank == 4102
    assert fetcher.calls == 1, "就绪后 collect 复用了探测数据，不应再抓一次"

