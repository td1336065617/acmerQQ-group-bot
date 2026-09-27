"""严格完整门禁判定测试（S1）。

覆盖：CF 的 phase 判据、AtCoder/牛客/洛谷的"稳定窗口 + 最小等待"判据、
轮询间隔推进与夜间放大、超时放弃判定，以及真实样本 fixture 回放。
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

from src.settlement import (
    MAX_WAIT_MINUTES,
    MIN_AGE_MINUTES,
    PROBE_INTERVALS,
    STABLE_SPAN_MINUTES,
    Sample,
    evaluate_readiness,
    new_poll_state,
    next_poll_delay_minutes,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str):
    with gzip.open(FIXTURES / name, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def test_cf_requires_finished_phase():
    state = new_poll_state(0.0)
    ready, reason = evaluate_readiness(
        "codeforces", Sample(rows=7474, hint_ready=None), state, 11.0
    )
    assert ready is False
    assert reason == "cf:phase-not-finished"

    ready, reason = evaluate_readiness(
        "codeforces", Sample(rows=7474, hint_ready=True), state, 45.0
    )
    assert ready is True
    assert reason == "cf:finished"


def test_platform_thresholds():
    assert MIN_AGE_MINUTES["codeforces"] == 0
    assert MIN_AGE_MINUTES["atcoder"] == 45
    assert MIN_AGE_MINUTES["luogu"] == 15
    assert STABLE_SPAN_MINUTES["atcoder"] == 30
    assert STABLE_SPAN_MINUTES["nowcoder"] == 15


def test_single_sample_is_never_ready():
    state = new_poll_state(0.0)
    ready, _ = evaluate_readiness(
        "atcoder",
        Sample(rows=3594, fingerprint="rows:3594"),
        state,
        37.0,
        now_ts=1000.0,
    )
    assert ready is False


def test_needs_min_age_even_when_stable():
    state = new_poll_state(0.0)
    sample = Sample(rows=11547, fingerprint="rows:11547")
    evaluate_readiness("atcoder", sample, state, 20.0, now_ts=1000.0)
    ready, _ = evaluate_readiness("atcoder", sample, state, 30.0, now_ts=3000.0)
    assert ready is False  # MinAge=45 未到


def test_needs_stable_span_even_when_counts_match():
    """关键回归：半截数据在稳定窗口未满时不得就绪。"""
    state = new_poll_state(0.0)
    sample = Sample(rows=3594, fingerprint="rows:3594")
    evaluate_readiness("atcoder", sample, state, 46.0, now_ts=1000.0)
    ready, reason = evaluate_readiness(
        "atcoder", sample, state, 50.0, now_ts=1600.0
    )  # 只过了 10 分钟 < 30 分钟窗口
    assert ready is False and "span_ok=False" in reason
    ready, _ = evaluate_readiness(
        "atcoder", sample, state, 90.0, now_ts=3000.0
    )  # 距稳定起点 33 分钟 ≥ 30 分钟
    assert ready is True


def test_growing_sample_resets_stability_and_span():
    state = new_poll_state(0.0)
    evaluate_readiness(
        "atcoder", Sample(rows=3594, fingerprint="rows:3594"), state, 46.0, now_ts=1000.0
    )
    ready, reason = evaluate_readiness(
        "atcoder", Sample(rows=8000, fingerprint="rows:8000"), state, 60.0, now_ts=4000.0
    )
    assert ready is False and "unstable" in reason
    evaluate_readiness(
        "atcoder", Sample(rows=8000, fingerprint="rows:8000"), state, 100.0, now_ts=6500.0
    )
    ready, _ = evaluate_readiness(
        "atcoder", Sample(rows=8000, fingerprint="rows:8000"), state, 120.0, now_ts=7000.0
    )
    assert ready is True


def test_fingerprint_stability_for_nowcoder():
    state = new_poll_state(0.0)
    first = Sample(rows=3, fingerprint="set:a|b|c")
    evaluate_readiness("nowcoder", first, state, 20.0, now_ts=0.0)
    assert evaluate_readiness("nowcoder", first, state, 25.0, now_ts=600.0)[0] is False
    assert evaluate_readiness("nowcoder", first, state, 40.0, now_ts=1200.0)[0] is True
    changed = Sample(rows=4, fingerprint="set:a|b|c|d")
    assert evaluate_readiness("nowcoder", changed, state, 45.0, now_ts=1500.0)[0] is False


def test_stable_samples_can_be_raised():
    state = new_poll_state(0.0)
    sample = Sample(rows=100, fingerprint="rows:100")
    # 本用例只验证"次数"门槛，故把稳定窗口显式关掉
    for _ in range(2):
        assert (
            evaluate_readiness(
                "atcoder", sample, state, 60.0, stable_samples=3,
                stable_span_minutes=0, now_ts=5000.0,
            )[0]
            is False
        )
    assert (
        evaluate_readiness(
            "atcoder", sample, state, 60.0, stable_samples=3,
            stable_span_minutes=0, now_ts=5000.0,
        )[0]
        is True
    )


def test_next_poll_delay_progression():
    seq = PROBE_INTERVALS["codeforces"]
    assert next_poll_delay_minutes("codeforces", 0) == seq[0]
    assert next_poll_delay_minutes("codeforces", len(seq) - 1) == seq[-1]
    assert next_poll_delay_minutes("codeforces", 99) == seq[-1]  # 长尾固定最后一档
    assert next_poll_delay_minutes("atcoder", 0) == PROBE_INTERVALS["atcoder"][0]


def test_night_scale_doubles_interval():
    base = next_poll_delay_minutes("atcoder", 0)
    assert next_poll_delay_minutes("atcoder", 0, night=True, night_scale=2.0) == base * 2
    assert next_poll_delay_minutes("atcoder", 0, night=True, night_scale=0.5) == base


def test_max_wait_constants():
    """最长等待常量表（线上由设置项覆盖，常量仅作兜底）。"""
    assert MAX_WAIT_MINUTES["codeforces"] == 1440
    assert MAX_WAIT_MINUTES["atcoder"] == 720
    assert MAX_WAIT_MINUTES["nowcoder"] == 720
    assert MAX_WAIT_MINUTES["luogu"] == 360


def test_real_fixture_row_counts():
    partial = _load("atcoder_abc477_partial.json.gz")
    full = _load("atcoder_abc477_full.json.gz")
    assert len(partial) == 3594
    assert len(full) == 11547
    assert max(r["Place"] for r in partial) <= 3594


def test_replay_abc477_partial_never_pushes():
    """回放昨晚真实序列：半截榜单在稳定窗口内无论多少 tick 都不能判为就绪。"""
    partial = _load("atcoder_abc477_partial.json.gz")
    full = _load("atcoder_abc477_full.json.gz")
    state = new_poll_state(0.0)
    half = f"rows:{len(partial)}"
    # 真实节奏：T+11 ~ T+37 多次抓取都是 3594 行（自洽但未发布完）
    for elapsed, ts in ((11.0, 100.0), (14.0, 280.0), (20.0, 640.0), (30.0, 1240.0), (37.0, 1660.0)):
        ready, _ = evaluate_readiness(
            "atcoder", Sample(rows=len(partial), fingerprint=half), state, elapsed, now_ts=ts
        )
        assert ready is False, f"elapsed={elapsed} 时误判为就绪"
    # 数据发布完成（11547 行）后：先重置基线，再满足稳定窗口 + MinAge
    ready, _ = evaluate_readiness(
        "atcoder", Sample(rows=len(full), fingerprint=f"rows:{len(full)}"), state, 60.0, now_ts=3000.0
    )
    assert ready is False
    ready, _ = evaluate_readiness(
        "atcoder", Sample(rows=len(full), fingerprint=f"rows:{len(full)}"), state, 70.0, now_ts=3400.0
    )
    assert ready is False  # 稳定窗口未满
    ready, _ = evaluate_readiness(
        "atcoder", Sample(rows=len(full), fingerprint=f"rows:{len(full)}"), state, 90.0, now_ts=4900.0
    )
    assert ready is True


def test_cf_fixture_exposes_unofficial_gap():
    """CF fixture 固化"S2 要补的打星人群"：ratingChanges 比官方榜单多 2979 人。"""
    standings = _load("cf_2269_standings.json.gz")
    changes = _load("cf_2269_ratingchanges.json.gz")
    official = {h for row in standings["rows"] for h in row["handles"]}
    assert standings["phase"] == "FINISHED"
    assert len(standings["rows"]) == 7474
    assert len(changes) == 10453
    unofficial = {item["handle"] for item in changes} - official
    assert len(unofficial) == 2979

def test_poll_key_scoping():
    from src.settlement import poll_key

    assert poll_key("codeforces", "2269") == "settle_poll_codeforces_2269"
    assert poll_key("atcoder", "abc477") == "settle_poll_atcoder_abc477"
    assert (
        poll_key("nowcoder", "1140237", "661977139")
        == "settle_poll_661977139_nowcoder_1140237"
    )
    assert poll_key("luogu", "289873", "C8D6") == "settle_poll_C8D6_luogu_289873"
    # 牛客/洛谷没有群号时退化为共享键（调用方必须传群号，测试仅锁行为）
    assert poll_key("nowcoder", "1140237") == "settle_poll_nowcoder_1140237"


def test_candidate_window_is_per_platform():
    import time as _t
    from datetime import datetime, timezone

    from src.settlement import SETTLE_WINDOW_HOURS, SettlementService

    svc = SettlementService(account_fetcher=None)
    now = _t.time()

    def remember(platform: str, cid: str, hours_ago: float) -> None:
        svc._recent[(platform, cid)] = {
            "contest_id": cid,
            "name": cid,
            "end_time": now - hours_ago * 3600,
            "start_time": now - (hours_ago + 2) * 3600,
            "duration_minutes": 120,
            "url": "",
        }

    remember("codeforces", "cf20h", 20.0)
    remember("atcoder", "at10h", 10.0)
    remember("atcoder", "at13h", 13.0)
    moment = datetime.fromtimestamp(now, tz=timezone.utc)
    assert [c.contest_id for c in svc.settlement_candidates("codeforces", moment, 10)] == ["cf20h"]
    assert [c.contest_id for c in svc.settlement_candidates("atcoder", moment, 10)] == ["at10h"]
    assert SETTLE_WINDOW_HOURS["codeforces"] == 24.0
    assert SETTLE_WINDOW_HOURS["atcoder"] == 12.0


def test_probe_bypasses_final_cache():
    """探测必须绕过 6 小时 final_cache：否则半截数据会被当成最终样本。"""
    import asyncio

    from src.settlement import SettlementService

    class FakeFetcher:
        def __init__(self) -> None:
            self.calls = 0

        async def _fetch_json(self, url, timeout=None):
            self.calls += 1
            size = 3594 if self.calls == 1 else 11547
            return [{"UserName": f"u{i}", "Place": i + 1} for i in range(size)]

    class FakeContest:
        contest_id = "abc477"

    svc = SettlementService(FakeFetcher())
    first = asyncio.run(svc.probe("atcoder", FakeContest(), []))
    assert first.rows == 3594
    # 第二次命中 30 秒 probe_cache（不重复抓取）
    again = asyncio.run(svc.probe("atcoder", FakeContest(), []))
    assert again.rows == 3594
    # force=True 绕过 probe_cache，且不读 final_cache → 拿到最新行数
    fresh = asyncio.run(svc.probe("atcoder", FakeContest(), [], force=True))
    assert fresh.rows == 11547


def test_probe_marks_cf_ready_only_when_finished():
    import asyncio

    from src.settlement import SettlementService

    class FakeFetcher:
        def __init__(self, phase: str, prelim: bool) -> None:
            self.phase = phase
            self.prelim = prelim

        async def _cf_json(self, method, params, timeout=None):
            return {
                "status": "OK",
                "result": {
                    "contest": {"phase": self.phase, "name": "Round X"},
                    "problems": [{"index": "A"}],
                    "rows": [
                        {
                            "rank": 1,
                            "party": {"members": [{"handle": "a"}]},
                            "problemResults": [
                                {"points": 1, "type": "PRELIMINARY" if self.prelim else "FINAL"}
                            ],
                        }
                    ],
                },
            }

    class FakeContest:
        contest_id = "2269"

    svc = SettlementService(FakeFetcher("SYSTEM_TEST", True))
    sample = asyncio.run(svc.probe("codeforces", FakeContest(), []))
    assert sample.hint_ready is False
    assert sample.rows == 1

    svc2 = SettlementService(FakeFetcher("FINISHED", False))
    sample2 = asyncio.run(svc2.probe("codeforces", FakeContest(), []))
    assert sample2.hint_ready is True

