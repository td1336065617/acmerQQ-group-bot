"""两层闸门与被拒行为的集成用例（S3，台账 M3.1-M3.8）。

覆盖：暂停期早报不烧补发预算、层 B 闸门与人工绕行、真实发送失败的
分类接入、结算入口闸门（不采集不标记不刷屏）、放弃提示闸门、
报名提醒闸门、阈值告警每群每日一次。
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import push_health
from test_main_accounts import _load_main_module
from test_morning_boards import _FakePlugin, _tick as _morning_tick
from test_settlement_tick import GROUP as TICK_GROUP, ACCOUNTS, MEMBERS
from test_settlement_tick import FakeSettlement, _build_bot, _contest, _result

m = _load_main_module()
KEY = push_health.health_key("", TICK_GROUP.group_id)
DENIED = {"kind": push_health.K_PERMISSION, "raw": "接口返回 主动消息失败, 无权限"}


def test_morning_suspension_skips_before_budget():
    """暂停期早报在护栏与计数之前就跳过：不烧每日 3 次补发预算（实现文档 P1）。"""
    plugin = _FakePlugin("🌅 今日比赛早报")

    async def suspended(group):
        return True

    plugin.push_suspended = suspended
    _morning_tick(plugin)
    assert plugin.sent == []
    assert plugin.attempt_checks == 0  # 护栏压根没走到 → 预算未消耗
    assert plugin.kv.get("morning_g1_20260913") is not True  # 不写幂等键


def _send_bot(suspended: bool):
    bot = m.AcmerGroupBot.__new__(m.AcmerGroupBot)
    kv: dict = {}
    sent: list = []

    async def get_kv_data(key, default=None):
        return kv.get(key, default)

    async def put_kv_data(key, value):
        kv[key] = value

    async def delete_kv_data(key):
        kv.pop(key, None)

    async def get_settings():
        return {"at_all_enabled": False}

    async def post_to_group(group_id, text, **kwargs):
        sent.append(text)
        return True

    bot.get_kv_data = get_kv_data
    bot.put_kv_data = put_kv_data
    bot.delete_kv_data = delete_kv_data
    bot.get_settings = get_settings
    bot._post_to_group = post_to_group
    bot._group_scene_ready = lambda gid, pid: True
    bot.context = types.SimpleNamespace()  # 放行路径会读 context 做通道判定（用例里另行打桩）
    bot._kv = kv
    bot._sent = sent
    if suspended:
        bot._cache_health(
            KEY,
            {"suspended_until": time.time() + 600, "state": "denied", "count": 1},
        )
    return bot, kv, sent


def test_layer_b_gate_blocks_text_bypass_passes(caplog):
    bot, kv, sent = _send_bot(suspended=True)
    # 插件包装日志器默认 INFO 级，会先一步滤掉 DEBUG——测试内临时放行并还原
    original_level = m.logger.level
    try:
        m.logger.level = logging.DEBUG
        with caplog.at_level(logging.DEBUG, logger="astrbot"):
            assert asyncio.run(bot.send_notification(TICK_GROUP, "hello")) is False
    finally:
        m.logger.level = original_level
    assert sent == []  # 真正没发
    assert any("跳过文字通知" in r.getMessage() for r in caplog.records)
    # 人工通道（测试推送）绕过闸门
    original = m.platform_compat.channel_of_platform_id
    m.platform_compat.channel_of_platform_id = lambda context, pid: "onebot"
    try:
        assert (
            asyncio.run(bot.send_notification(TICK_GROUP, "hello", bypass_suspend=True))
            is True
        )
    finally:
        m.platform_compat.channel_of_platform_id = original
    assert sent == ["hello"]


def test_text_send_failure_classifies_denied_and_writes_state():
    """真实异常路径：_post_to_group 内异常 → 分类 → 写健康状态，计数仍走既有路径。"""
    bot, kv, sent = _send_bot(suspended=False)
    # 换回真实的 _post_to_group（_send_bot 里装的是替身，分类逻辑在真身内部）
    del bot._post_to_group

    async def boom(*args, **kwargs):
        raise Exception("主动消息失败, 无权限")

    bot.context = types.SimpleNamespace(send_message=boom)
    bot._session_for = lambda *a, **k: "s"
    bot.output_renderer = types.SimpleNamespace(needs_image=lambda v: False)
    original = m.platform_compat.channel_of_platform_id
    m.platform_compat.channel_of_platform_id = lambda context, pid: "official"
    try:
        assert asyncio.run(bot.send_notification(TICK_GROUP, "hi")) is False
    finally:
        m.platform_compat.channel_of_platform_id = original
    state = kv.get(KEY)
    assert state, "权限类失败必须写健康状态"
    assert state["count"] == 1
    assert state["last_error_class"] == push_health.K_PERMISSION
    assert state["suspended_until"] > time.time() + 29 * 60  # 首次 30 分钟
    # 计数不重复：健康状态不写 pushfail，由发送路径的既有 _note_push_failure 记一次
    assert kv["pushfail_group_" + TICK_GROUP.group_id]["n"] == 1


def test_settlement_entry_gate_skips_without_render(caplog):
    settlement = FakeSettlement(_result())
    calls = []
    original_collect = settlement.collect

    async def spy(*args, **kwargs):
        calls.append(args[0] if args else None)
        return await original_collect(*args, **kwargs)

    settlement.collect = spy
    bot = _build_bot(
        m,
        groups=[TICK_GROUP],
        contests={"codeforces": [_contest()]},
        members=MEMBERS,
        accounts=ACCOUNTS,
        settlement=settlement,
    )
    contest = _contest()
    key = f"settle_{TICK_GROUP.group_id}_codeforces_{contest.contest_id}"
    bot._cache_health(
        KEY,
        {"suspended_until": time.time() + 600, "state": "denied", "count": 1},
    )
    with caplog.at_level(logging.DEBUG):
        result = asyncio.run(
            bot._push_settlement(
                TICK_GROUP,
                "codeforces",
                contest,
                key,
                min_participants=1,
                show_unsolved=True,
                platform_order=["codeforces"],
            )
        )
    assert result == 0
    assert calls == []  # 连采集都没发生 → 渲染必然没跑
    assert key not in bot._kv  # 不写任何标记（blocked 只在真实尝试失败后写）
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)  # 不刷屏
    # 人工试跑：同状态放行（bypass）
    result2 = asyncio.run(
        bot._push_settlement(
            TICK_GROUP,
            "codeforces",
            contest,
            key,
            min_participants=1,
            show_unsolved=True,
            platform_order=["codeforces"],
            bypass_suspend=True,
        )
    )
    assert len(calls) == 1  # 采集真的发生了
    assert result2 in (0, 1)


def test_abandon_notice_gate_skips_silently():
    bot, kv, sent = _send_bot(suspended=True)
    notified = []

    async def notify(group, contest):
        notified.append(contest)

    bot._notify_settle_abandoned = notify
    contest = types.SimpleNamespace(contest_id="999")
    asyncio.run(
        bot._settle_abandon_notice(
            TICK_GROUP, "codeforces", contest, {"settle_abandon_notice": True}
        )
    )
    assert notified == []
    assert f"settle_notice_{TICK_GROUP.group_id}_codeforces_999" not in kv  # 不写去重键


def test_signup_gate_blocks_scheduled_bypass_passes():
    bot, kv, sent = _send_bot(suspended=True)

    async def rec(group, text, **kwargs):
        sent.append(text)
        return True

    bot.send_notification = rec
    assert asyncio.run(bot._send_signup_text(TICK_GROUP, "报名提醒")) is False
    assert sent == []
    assert asyncio.run(
        bot._send_signup_text(TICK_GROUP, "报名提醒", bypass_suspend=True)
    ) is True
    assert sent == ["报名提醒"]


def test_threshold_warn_once_per_day(caplog):
    """达阈值后每群每日只告警一次；进程内记忆清空（次日）才再告警。"""
    bot, kv, sent = _send_bot(suspended=False)
    with caplog.at_level(logging.WARNING):
        for _ in range(6):
            asyncio.run(bot._note_push_denied(TICK_GROUP.group_id, "", DENIED))
    warns = [r for r in caplog.records if "推送被平台拒绝" in r.getMessage()]
    assert len(warns) == 1, f"应恰有 1 条阈值告警，实际 {len(warns)}"
    # 模拟次日：清空进程内记忆后允许再告警
    bot._denied_warned.clear()
    asyncio.run(bot._note_push_denied(TICK_GROUP.group_id, "", DENIED))
    warns2 = [r for r in caplog.records if "推送被平台拒绝" in r.getMessage()]
    assert len(warns2) == 2
