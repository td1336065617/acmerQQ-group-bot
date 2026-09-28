"""赛果「无人参赛」归因修正与终局标记（N1-N3，实现文档：赛果无人参赛归因与终局标记）。

覆盖：AT/CF 采集层「就绪但无人」返回空结果（不再折 None）、调用方终局标记
（一次后静默、指纹变化可重开）、None 语义改为「数据未就绪」且文案不再误导。
"""
from __future__ import annotations

import asyncio
import logging
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.settlement import SettlementService, SettleResult

from test_main_accounts import _load_main_module
from test_settlement_tick import GROUP, FakeSettlement, _build_bot, _contest


# ---------------------------------------------------------------------------
# 采集层单元：N1 AtCoder / N2 Codeforces
# ---------------------------------------------------------------------------


def test_atcoder_collect_returns_empty_result_when_no_member_in_ready_index():
    """N1：index 就绪但成员全不在榜 → 空结果（终局），不得返回 None。"""
    svc = SettlementService(None)

    async def fake_index(slug, *, fresh=False):
        return ({"somebodyelse": {"Place": 1}}, 1)

    svc._atcoder_results_index = fake_index
    contest = types.SimpleNamespace(contest_id="agc078", name="AGC 078")
    members = [("u1", "张三", "nosuchhandle")]
    result = asyncio.run(svc._collect_atcoder(contest, members))
    assert isinstance(result, SettleResult)
    assert result.rows == []
    assert result.has_content() is False


def test_atcoder_collect_none_when_index_not_ready():
    """N1 边界：index 空（结果未发布）仍是 None（瞬态、继续重试）。"""
    svc = SettlementService(None)

    async def fake_index(slug, *, fresh=False):
        return ({}, 0)

    svc._atcoder_results_index = fake_index
    contest = types.SimpleNamespace(contest_id="agc078", name="AGC 078")
    result = asyncio.run(
        svc._collect_atcoder(contest, [("u1", "张三", "handle1")])
    )
    assert result is None


def test_cf_collect_returns_empty_result_when_no_member_in_standings():
    """N2：raw 榜就绪但成员全不在 → 空结果（终局），不得返回 None。"""
    svc = SettlementService(None)

    async def fake_meta(cid, *, fresh=False):
        return {"rows": [{"handle": "somebodyelse"}], "problems": []}

    def fake_official(*args, **kwargs):
        return []  # 单测边界：匹配器给空（折叠逻辑本身是被测对象；注意真身是同步方法）

    svc._cf_standings_meta = fake_meta
    svc._cf_official_rows = fake_official
    contest = types.SimpleNamespace(contest_id="2264", name="CF Round")
    result = asyncio.run(
        svc._collect_codeforces(contest, [("u1", "张三", "zhangsan")], include_unofficial=False)
    )
    assert isinstance(result, SettleResult)
    assert result.rows == []
    assert result.has_content() is False


def test_cf_collect_none_when_standings_not_ready():
    """N2 边界：raw 榜为空（910 行）仍是 None（瞬态、继续重试）。"""
    svc = SettlementService(None)

    async def fake_meta(cid, *, fresh=False):
        return {"rows": [], "problems": []}

    svc._cf_standings_meta = fake_meta
    contest = types.SimpleNamespace(contest_id="2264", name="CF Round")
    result = asyncio.run(
        svc._collect_codeforces(contest, [("u1", "张三", "zhangsan")])
    )
    assert result is None


# ---------------------------------------------------------------------------
# 调用层集成：N3 终局标记 / 瞬态 / 重开 / 文案
# ---------------------------------------------------------------------------

_EMPTY = SettleResult(
    platform="codeforces",
    contest_id="2264",
    contest_name="Codeforces Round 1121 (Div. 2)",
    rows=[],
    note="CF standings 已就绪，本群绑定成员均未参加该场比赛",
)


def test_terminal_no_participants_writes_marker_once_then_silent(caplog):
    """终局：空结果 → skipped 标记 + 准确日志 + push_log；下一 tick 不再采集。"""
    m = _load_main_module()
    settlement = FakeSettlement(result=_EMPTY)
    bot = _build_bot(
        m,
        groups=[GROUP],
        contests={"codeforces": [_contest()]},
        members={"g1": ["u1"]},
        accounts={"u1": {"codeforces": {"handle": "zhangsan", "display_name": "张三"}}},
        settlement=settlement,
    )
    key = f"settle_{GROUP.group_id}_codeforces_2264"
    with caplog.at_level(logging.INFO, logger="astrbot"):
        first = asyncio.run(bot.tick_settlements())
        second = asyncio.run(bot.tick_settlements())
    assert first == 0 and second == 0
    assert settlement.calls == 1  # 第二 tick 被终局标记拦下，未再采集
    marker = bot._kv.get(key)
    assert isinstance(marker, dict) and marker.get("skipped") is True
    assert marker.get("reason") == "no-participants"
    assert str(marker.get("members") or "")  # 带成员指纹（变化可重开）
    msgs = [r.getMessage() for r in caplog.records]
    assert any("均未参加该场比赛" in msg for msg in msgs), msgs
    assert any("赛果采集失败（接口异常或平台未公开）" in msg for msg in msgs) is False
    assert bot._kv.get("push_log") and any(
        "均未参加" in str(entry.get("detail") or entry.get("reason") or "")
        for entry in bot._kv["push_log"]
        if isinstance(entry, dict)
    ), bot._kv.get("push_log")


def test_transient_none_retries_without_marker(caplog):
    """瞬态：None → 「数据未就绪」文案、不写标记、下 tick 重新采集。"""
    m = _load_main_module()
    settlement = FakeSettlement(result=None)
    bot = _build_bot(
        m,
        groups=[GROUP],
        contests={"codeforces": [_contest()]},
        members={"g1": ["u1"]},
        accounts={"u1": {"codeforces": {"handle": "zhangsan", "display_name": "张三"}}},
        settlement=settlement,
    )
    key = f"settle_{GROUP.group_id}_codeforces_2264"
    with caplog.at_level(logging.INFO, logger="astrbot"):
        asyncio.run(bot.tick_settlements())
        asyncio.run(bot.tick_settlements())
    assert settlement.calls == 2  # 瞬态允许重试
    assert key not in bot._kv  # 不写终局标记
    msgs = [r.getMessage() for r in caplog.records]
    assert any("赛果数据未就绪" in msg for msg in msgs), msgs
    assert not any("接口异常或平台未公开" in msg for msg in msgs)




def test_collect_passes_empty_result_through():
    """接缝用例（生产断言抓出的漏洞）：collect() 的缓存尾巴必须原样透传空结果。

    此前两层测试都绕过了这里——单测直调 _collect_atcoder、集成用 FakeSettlement，
    于是「采集层返回空结果」在真实 collect() 里被归一成 None，
    终局标记永远触发不了（1.21.2 实施时的实际缺陷）。
    """
    svc = SettlementService(None)

    async def fake_atcoder(contest, members):
        return _EMPTY

    svc._collect_atcoder = fake_atcoder
    contest = types.SimpleNamespace(contest_id="agc078", name="AGC 078")
    members = [("u1", "张三", "h1")]
    got = asyncio.run(svc.collect("atcoder", contest, members))
    assert isinstance(got, SettleResult) and got.has_content() is False  # 不得被吞成 None

    async def fake_atcoder_none(contest, members):
        return None

    svc._collect_atcoder = fake_atcoder_none
    assert asyncio.run(svc.collect("atcoder", contest, members)) is None  # 瞬态仍透传 None

    async def fake_atcoder_content(contest, members):
        return SettleResult(
            platform="atcoder",
            contest_id="agc078",
            contest_name="AGC 078",
            rows=[],
            extra_note="洛谷参赛：张三",  # 有 extra_note = 有内容，应被缓存并返回
        )

    svc._collect_atcoder = fake_atcoder_content
    got2 = asyncio.run(svc.collect("atcoder", contest, members))
    assert got2 is not None and got2.has_content() is True

def test_trial_no_participants_writes_no_marker_or_empty_key(caplog):
    """1.21.3 精读补锁：试跑（write_key=False、key 为空）遇无人参赛时，
    不得写标记、不得写空键垃圾行、不记跳过 push_log（与阈值跳过同款守卫）；
    准确文案仍要打（试跑者要看到原因）。
    """
    m = _load_main_module()
    settlement = FakeSettlement(result=_EMPTY)
    bot = _build_bot(
        m,
        groups=[GROUP],
        contests={"codeforces": [_contest()]},
        members={"g1": ["u1"]},
        accounts={"u1": {"codeforces": {"handle": "zhangsan", "display_name": "张三"}}},
        settlement=settlement,
    )
    with caplog.at_level(logging.INFO, logger="astrbot"):
        rc = asyncio.run(
            bot._push_settlement(
                GROUP,
                "codeforces",
                _contest(),
                "",  # 试跑不带幂等键（与 _run_now_settle 一致）
                min_participants=1,
                show_unsolved=True,
                platform_order=["codeforces"],
                write_key=False,
                bypass_suspend=True,
            )
        )
    assert rc == 0
    assert "" not in bot._kv  # 不写空键
    assert not any(str(k).startswith("settle_") for k in bot._kv)  # 不写标记
    assert "push_log" not in bot._kv  # 不记跳过（阈值跳过对试跑同样不记）
    msgs = [r.getMessage() for r in caplog.records]
    assert any("均未参加该场比赛" in msg for msg in msgs), msgs


def test_marker_reevaluates_once_when_fingerprint_changes():
    """重开：成员指纹变化（新绑定）→ 终局标记放行一次重评。"""
    m = _load_main_module()
    settlement = FakeSettlement(result=_EMPTY)
    bot = _build_bot(
        m,
        groups=[GROUP],
        contests={"codeforces": [_contest()]},
        members={"g1": ["u1"]},
        accounts={"u1": {"codeforces": {"handle": "zhangsan", "display_name": "张三"}}},
        settlement=settlement,
    )
    asyncio.run(bot.tick_settlements())
    assert settlement.calls == 1
    # 第二 tick：指纹没变 → 拦下
    asyncio.run(bot.tick_settlements())
    assert settlement.calls == 1
    # 成员变化（新绑定）→ 放行重评一次
    bot.account_registry = _build_bot(
        m,
        groups=[GROUP],
        contests={"codeforces": [_contest()]},
        members={"g1": ["u1", "u2"]},
        accounts={
            "u1": {"codeforces": {"handle": "zhangsan", "display_name": "张三"}},
            "u2": {"codeforces": {"handle": "lisi", "display_name": "李四"}},
        },
        settlement=settlement,
    ).account_registry
    asyncio.run(bot.tick_settlements())
    assert settlement.calls == 2  # 指纹变化允许恰好一次
