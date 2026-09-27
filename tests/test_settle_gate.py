"""严格完整门禁 tick 集成测试（S3 验证）。

复用 test_settlement_tick 的搭建器，覆盖：
- 未就绪：不推送、写轮询状态、按间隔排队
- 就绪：推送一次、轮询状态置终态
- 超时：不发卡、群内一句异常提示、push_log 记失败、只提示一次
- 开关关闭：回到旧行为（有内容即推）
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_main_accounts import _load_main_module
from test_settlement_tick import (
    ACCOUNTS,
    GROUP,
    MEMBERS,
    FakeSettlement,
    _build_bot,
    _contest,
    _result,
)

from src.settlement import Sample


class GateSettlement(FakeSettlement):
    """按预设样本序列返回 probe 结果（不访问网络）。"""

    def __init__(self, samples, result=None):
        super().__init__(result=result if result is not None else _result())
        self._samples = list(samples)
        self.probe_calls = 0

    async def probe(self, platform, contest, members, **kwargs):
        index = min(self.probe_calls, len(self._samples) - 1)
        self.probe_calls += 1
        return self._samples[index]




def _bot(main_module, settlement, settings=None, hours_ago: float = 0.5):
    return _build_bot(
        main_module,
        groups=[GROUP],
        contests={"codeforces": [_contest(hours_ago=hours_ago)]},
        members=MEMBERS,
        accounts=ACCOUNTS,
        settlement=settlement,
        settings=settings,
    )


def test_gate_waits_when_not_ready():
    async def scenario():
        m = _load_main_module()
        settlement = GateSettlement(
            [Sample(rows=7474, fingerprint="rows:7474:SYSTEM_TEST", hint_ready=False)]
        )
        bot = _bot(m, settlement, settings={"settle_strict_enabled": True})
        assert await bot.tick_settlements() == 0
        assert bot._sent == [] and bot._images == []
        state = bot._kv["settle_poll_codeforces_2264"]
        assert state["state"] == "POLLING"
        assert state["attempts"] == 1
        assert state["next_poll_at"] > 0
        assert state["samples"][-1]["rows"] == 7474

    asyncio.run(scenario())


def test_gate_pushes_once_when_ready():
    async def scenario():
        m = _load_main_module()
        settlement = GateSettlement(
            [Sample(rows=7474, fingerprint="rows:7474:FINISHED", hint_ready=True)]
        )
        bot = _bot(m, settlement, settings={"settle_strict_enabled": True})
        assert await bot.tick_settlements() == 1
        assert bot._kv["settle_poll_codeforces_2264"]["state"] == "PUSHED"
        marker = bot._kv["settle_g1_codeforces_2264"]
        assert isinstance(marker, dict) and marker["state"] == "pushed"
        # 第二次 tick：命中幂等键，不再推送
        assert await bot.tick_settlements() == 0

    asyncio.run(scenario())


def test_gate_abandons_and_notifies_once():
    async def scenario():
        m = _load_main_module()
        settlement = GateSettlement(
            [Sample(rows=7474, fingerprint="rows:7474:SYSTEM_TEST", hint_ready=False)]
        )
        # CF 上限取边界（60 分钟），比赛结束 2 小时前 → 直接超时
        bot = _bot(
            m,
            settlement,
            settings={"settle_strict_enabled": True, "settle_cf_max_wait_minutes": 60},
            hours_ago=2.0,
        )
        assert await bot.tick_settlements() == 0
        state = bot._kv["settle_poll_codeforces_2264"]
        assert state["state"] == "ABANDONED"
        assert state["abandoned_reason"]
        notices = [text for _, text in bot._sent if "结算异常" in text]
        assert len(notices) == 1
        assert bot._kv["push_log"][-1]["ok"] is False
        # 再次 tick：终态，不再重复提示
        assert await bot.tick_settlements() == 0
        assert len([t for _, t in bot._sent if "结算异常" in t]) == 1
        assert "settle_g1_codeforces_2264" not in bot._kv  # 绝不留"已推送"标记

    asyncio.run(scenario())


def test_strict_disabled_keeps_legacy_behaviour():
    async def scenario():
        m = _load_main_module()
        settlement = GateSettlement(
            [Sample(rows=7474, fingerprint="not-ready", hint_ready=False)]
        )
        bot = _bot(m, settlement, settings={"settle_strict_enabled": False})
        assert await bot.tick_settlements() == 1  # 旧行为：有内容就推
        assert "settle_g1_codeforces_2264" in bot._kv

    asyncio.run(scenario())


def test_replay_real_abc477_through_tick():
    """端到端回放：用真实 abc477 样本驱动 tick，半截数据绝不推卡。

    时间线（比赛结束 T+0）：
    T+37/T+40 半截榜（3594 行）→ 不推
    T+100 数据发布完成（11547 行）→ 计数与稳定窗口重置 → 不推
    T+140 稳定窗口（30 分钟）与 MinAge（45 分钟）均满足 → 推一次
    """
    import gzip
    import json
    from datetime import datetime, timedelta, timezone
    from pathlib import Path

    from src.models import Contest, GroupConfig
    from src.settlement import Sample

    from test_main_accounts import _load_main_module
    from test_settlement_tick import _build_bot

    fixtures = Path(__file__).resolve().parent / "fixtures"

    def _load(name):
        with gzip.open(fixtures / name, "rt", encoding="utf-8") as fh:
            return json.load(fh)

    partial = _load("atcoder_abc477_partial.json.gz")
    full = _load("atcoder_abc477_full.json.gz")
    half = f"rows:{len(partial)}"
    done = f"rows:{len(full)}"
    samples = [
        Sample(rows=len(partial), fingerprint=half),
        Sample(rows=len(partial), fingerprint=half),
        Sample(rows=len(full), fingerprint=done),
        Sample(rows=len(full), fingerprint=done),
        Sample(rows=len(full), fingerprint=done),
    ]

    async def scenario():
        m = _load_main_module()
        end = datetime.now(timezone.utc) - timedelta(minutes=37)
        contest = Contest(
            platform="atcoder",
            name="ABC477",
            start_time=end - timedelta(minutes=100),
            end_time=end,
            duration_minutes=100,
            url="https://atcoder.jp/contests/abc477",
            contest_id="abc477",
        )
        group = GroupConfig(group_id="g1", push_platforms=["atcoder"])
        bot = _build_bot(
            m,
            groups=[group],
            contests={"atcoder": [contest]},
            members={"g1": ["u1"]},
            accounts={"u1": {"atcoder": {"handle": "starsilk", "display_name": "星"}}},
            settlement=GateSettlement(samples),
            settings={
                "settle_strict_enabled": True,
                "settle_atcoder_min_age_minutes": 45,
                "push_platforms": ["atcoder"],
            },
        )
        push_points = []
        # offset 必须大于 GateSettlement 的 5 分钟间隔，否则该次 tick 会被 next_poll_at 跳过
        for offset in (0, 6, 12, 40, 75):
            await bot.tick_settlements(now=end + timedelta(minutes=37 + offset))
            push_points.append(1 if bot._kv.get("settle_g1_atcoder_abc477") else 0)
        # 只有最后一次（T+140）允许推
        assert push_points == [0, 0, 0, 0, 1]
        state = bot._kv["settle_poll_atcoder_abc477"]
        assert state["state"] == "PUSHED"
        assert len(state["samples"]) >= 5
        # 推送发生在最后一次 tick（T+37+75 = T+112），且必须已越过 MinAge
        assert state["elapsed_minutes"] >= 100

    asyncio.run(scenario())



def test_abandon_notice_is_per_group():
    """poll 状态跨群共享：同一场超时后，每个群各收到一次提示（且仅一次）。"""
    from src.models import GroupConfig

    async def scenario():
        m = _load_main_module()
        settlement = GateSettlement(
            [Sample(rows=7474, fingerprint="rows:7474:SYSTEM_TEST", hint_ready=False)]
        )
        groups = [
            GroupConfig(group_id="g1", push_platforms=["codeforces"]),
            GroupConfig(group_id="g2", push_platforms=["codeforces"]),
        ]
        bot = _build_bot(
            m,
            groups=groups,
            contests={"codeforces": [_contest(hours_ago=2.0)]},
            members={"g1": ["u1"], "g2": ["u2"]},
            accounts={
                "u1": {"codeforces": {"handle": "zhangsan", "display_name": "张三"}},
                "u2": {"codeforces": {"handle": "lisi", "display_name": "李四"}},
            },
            settlement=settlement,
            settings={
                "settle_strict_enabled": True,
                "settle_cf_max_wait_minutes": 60,
            },
        )
        assert await bot.tick_settlements() == 0
        noticed = sorted(gid for gid, text in bot._sent if "结算异常" in text)
        assert noticed == ["g1", "g2"], noticed
        assert bot._kv["settle_poll_codeforces_2264"]["state"] == "ABANDONED"
        # 终态 + per-group 去重：再来一轮不重复提示
        assert await bot.tick_settlements() == 0
        assert len([t for _, t in bot._sent if "结算异常" in t]) == 2

    asyncio.run(scenario())



# ---------------------------------------------------------------------------
# P2：READY 复评节流（1.20.6）
# ---------------------------------------------------------------------------

GATE_SETTINGS = {
    "settle_strict_enabled": True,
    "settle_stable_samples": 2,
    "settle_atcoder_min_age_minutes": 45,
    "settle_night_scale": 2,
    "settle_cf_max_wait_minutes": 1440,
}


def _gate_once(bot, *, hours_ago=0.5, marker_key="settle_g1_codeforces_2264", settings=None):
    from datetime import datetime, timezone

    st = dict(GATE_SETTINGS)
    st.update(settings or {})
    moment = datetime.now(timezone.utc)
    return asyncio.run(
        bot._settle_gate(
            GROUP, "codeforces", _contest(hours_ago=hours_ago), st, moment,
            marker_key=marker_key,
        )
    )


def test_ready_state_defers_recheck():
    """就绪后短时间内再评估：直接 wait，不再探测，并排好 30 分钟复评。"""
    m = _load_main_module()
    settlement = GateSettlement(
        [Sample(rows=7474, fingerprint="rows:7474:FINISHED", hint_ready=True)]
    )
    bot = _bot(m, settlement, settings={"settle_strict_enabled": True})
    verdict1, poll_key = _gate_once(bot)
    verdict2, _ = _gate_once(bot)
    assert verdict1 == "ready"
    assert verdict2 == "wait"           # 复评间隔内不再探测
    assert settlement.probe_calls == 1  # 关键：没有第二次抓榜单
    state = bot._kv[poll_key]
    assert state["ready_logged"] is True
    assert state["next_poll_at"] - state["ready_at"] == 30 * 60


def test_ready_log_emitted_once(caplog):
    """「已结算完成」只在状态迁移时打一次，复评不再刷屏。"""
    import logging

    m = _load_main_module()
    settlement = GateSettlement(
        [Sample(rows=7474, fingerprint="rows:7474:FINISHED", hint_ready=True)]
    )
    bot = _bot(m, settlement, settings={"settle_strict_enabled": True})
    with caplog.at_level(logging.INFO):
        _gate_once(bot)
        _gate_once(bot)
    hits = [r for r in caplog.records if "已结算完成" in r.getMessage()]
    assert len(hits) == 1


# ---------------------------------------------------------------------------
# P3：无人参赛静默跳过（1.20.6）
# ---------------------------------------------------------------------------


def test_timeout_without_participants_is_silent():
    """全场 0 行（本群无人参赛）+ 超时：不发提示、不记失败，只落 skipped 标记。"""
    m = _load_main_module()
    settlement = GateSettlement([Sample(rows=0, fingerprint="rows:0:none", hint_ready=False)])
    bot = _bot(m, settlement, settings={"settle_strict_enabled": True})
    verdict, poll_key = _gate_once(bot, hours_ago=30.0)
    assert verdict == "abandoned"
    state = bot._kv[poll_key]
    assert state["state"] == "ABANDONED"
    assert state["abandoned_reason"] == "no-participants"
    assert bot._sent == []                                   # 群内没有任何提示
    assert not [i for i in bot._kv.get("push_log", []) if i.get("kind") == "settle"]
    marker = bot._kv["settle_g1_codeforces_2264"]
    assert marker["skipped"] is True and marker["members"]   # 带成员指纹，便于日后补评估


def test_timeout_with_current_rows_still_notifies():
    """回归：首轮评估就超时时，本次样本的 rows 必须参与判定（否则漏发真数据）。"""
    m = _load_main_module()
    settlement = GateSettlement([Sample(rows=5, fingerprint="rows:5:real", hint_ready=False)])
    bot = _bot(m, settlement, settings={"settle_strict_enabled": True})
    verdict, poll_key = _gate_once(bot, hours_ago=30.0)
    assert verdict == "abandoned"
    assert [t for _, t in bot._sent if "结算异常" in t]
    assert bot._kv[poll_key]["abandoned_reason"] != "no-participants"
    assert bot._kv["push_log"][-1]["ok"] is False


def test_timeout_history_zero_but_current_rows_notifies():
    """历史样本全 0、本次有行：仍按"有数据"处理（发提示）。"""
    m = _load_main_module()
    settlement = GateSettlement([Sample(rows=7, fingerprint="rows:7:real", hint_ready=False)])
    bot = _bot(m, settlement, settings={"settle_strict_enabled": True})
    bot._kv["settle_poll_codeforces_2264"] = {
        "state": "POLLING",
        "attempts": 2,
        "samples": [{"ts": 1, "rows": 0, "fp": "rows:0"}],
        "next_poll_at": 0,
    }
    verdict, poll_key = _gate_once(bot, hours_ago=30.0)
    assert verdict == "abandoned"
    assert [t for _, t in bot._sent if "结算异常" in t]
    assert bot._kv[poll_key]["abandoned_reason"] != "no-participants"

