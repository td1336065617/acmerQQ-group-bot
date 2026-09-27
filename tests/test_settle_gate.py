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

    def next_poll_delay_minutes(self, platform, attempts, **kwargs):
        return 5.0




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

