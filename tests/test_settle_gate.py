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
