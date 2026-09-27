"""结算服务接口契约测试（防"实现改了、调用点没跟"这类只在生产出现的错）。

真实事故（2026-09-27 22:14 灰度期间）：next_poll_delay_minutes 是**模块级函数**，
main.py 却写成 self.settlement.next_poll_delay_minutes(...) → 等待路径（牛客/AtCoder/洛谷）
每个 tick 抛 AttributeError，赛果卡完全推不出去；而测试里的 fake service 恰好定义了
同名方法，于是 451 个用例全绿也没拦住。本文件专门盯这类"接口面不一致"。
"""
from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models import GroupConfig
from src.settlement import Sample, SettlementService, next_poll_delay_minutes

from test_main_accounts import _load_main_module

ROOT = Path(__file__).resolve().parent.parent
CN = timezone(timedelta(hours=8))


class _StubFetcher:
    """SettlementService 只用到 account_fetcher 的少量接口，等待路径用不到。"""


class _Contest:
    def __init__(self, contest_id: str, *, ended_minutes_ago: float = 30.0) -> None:
        self.contest_id = contest_id
        self.name = "牛客周赛 Round 163"
        self.url = "https://ac.nowcoder.com/acm/contest/1140737"
        self.platform = "nowcoder"
        self.end_time = datetime.now(CN) - timedelta(minutes=ended_minutes_ago)


def test_main_only_calls_real_service_methods():
    """main.py 里 self.settlement.X 的每个 X 都必须是 SettlementService 的方法。"""
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    used = set(re.findall(r"self\.settlement\.([A-Za-z_][A-Za-z0-9_]*)", source))
    assert used, "没解析到 self.settlement.* 调用：正则或代码结构变了"
    missing = sorted(name for name in used if not hasattr(SettlementService, name))
    assert not missing, f"main.py 调用了 SettlementService 上不存在的方法：{missing}"


def test_main_imports_settlement_module_helpers():
    """main.py 直接调用的模块级助手必须真的被 import 进来。"""
    m = _load_main_module()
    for name in ("evaluate_readiness", "new_poll_state", "settle_poll_key",
                 "settle_poll_delay_minutes"):
        assert hasattr(m, name), f"main.py 缺少 {name} 的导入"


def test_poll_delay_helper_contract():
    """轮询间隔助手：正常返回、且不会超过稳定窗口（调用方靠它保证采样可信）。"""
    assert next_poll_delay_minutes("atcoder", 0) > 0
    capped = next_poll_delay_minutes("atcoder", 99, max_delay_minutes=30)
    assert capped <= 30
    night = next_poll_delay_minutes("nowcoder", 0, night=True, night_scale=3)
    assert night == next_poll_delay_minutes("nowcoder", 0) * 3


def test_test_fakes_do_not_invent_service_methods():
    """测试用的 fake 不得定义真实服务没有的方法。

    真实事故（2026-09-27）：两个 fake 都定义了 next_poll_delay_minutes，而真实
    SettlementService 没有该方法（它是模块级函数）→ main.py 写错调用点后
    455 个用例仍然全绿，生产每 tick 崩了 9 小时。
    """
    from test_settle_gate import GateSettlement
    from test_settlement_tick import FakeSettlement

    for cls in (FakeSettlement, GateSettlement):
        invented = sorted(
            name
            for name, value in vars(cls).items()
            if not name.startswith("_")
            and callable(value)
            and not hasattr(SettlementService, name)
        )
        assert not invented, f"{cls.__name__} 定义了真实服务没有的方法：{invented}"


def test_gate_wait_path_with_real_service():
    """用真实 SettlementService 走一遍"未就绪 → 等待"分支（生产在此崩过）。"""
    m = _load_main_module()
    bot = m.AcmerGroupBot.__new__(m.AcmerGroupBot)
    bot._kv = {}
    bot._settle_probe_budget = 3

    async def get_kv_data(key, default=None):
        return bot._kv.get(key, default)

    async def put_kv_data(key, value):
        bot._kv[key] = value

    async def members(group_id, platform):
        return []

    bot.get_kv_data = get_kv_data
    bot.put_kv_data = put_kv_data
    bot._settlement_members = members

    service = SettlementService(_StubFetcher())

    async def fake_probe(platform, contest, member_list, group_id=None):
        return Sample(rows=100, fingerprint="fp-1", hint_ready=False)

    service.probe = fake_probe  # 只挡网络，其余走真实实现
    bot.settlement = service

    settings = {
        "settle_stable_samples": 2,
        "settle_night_scale": 2,
        "settle_atcoder_min_age_minutes": 45,
    }
    moment = datetime.now(CN)
    verdict, poll_key = asyncio.run(
        bot._settle_gate(GroupConfig(group_id="G1", platform_id="爱莉希雅", umo="u"),
                         "nowcoder", _Contest("1140737"), settings, moment)
    )
    assert verdict == "wait", verdict
    state = bot._kv[poll_key]
    assert state["state"] == "POLLING"
    assert state["attempts"] == 1
    assert state["next_poll_at"] > moment.timestamp()
    assert state["samples"] and state["samples"][-1]["rows"] == 100
    # 第二次调用：同一个 tick 内预算与状态都要继续可用
    verdict2, _ = asyncio.run(
        bot._settle_gate(GroupConfig(group_id="G1", platform_id="爱莉希雅", umo="u"),
                         "nowcoder", _Contest("1140737"), settings, moment)
    )
    assert verdict2 == "wait"
