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




def _tick_bot():
    settlement = FakeSettlement(_result())
    bot = _build_bot(
        m,
        groups=[TICK_GROUP],
        contests={"codeforces": [_contest()]},
        members=MEMBERS,
        accounts=ACCOUNTS,
        settlement=settlement,
    )
    return bot, settlement


def test_blocked_marker_same_fingerprint_skips():
    """被拒场次：成员指纹没变 → 不评估不推送、标记保留（台账 M4.2）。"""
    bot, settlement = _tick_bot()
    contest = _contest()
    key = f"settle_{TICK_GROUP.group_id}_codeforces_{contest.contest_id}"
    # 指纹要用运行时口径（_settlement_members 返回的是 (id, 名, handle) 元组表）
    fp = bot._members_fingerprint(
        asyncio.run(bot._settlement_members(TICK_GROUP.group_id, "codeforces"))
    )
    bot._kv[key] = {"blocked": True, "reason": push_health.K_PERMISSION, "members": fp}
    assert asyncio.run(bot.tick_settlements()) == 0
    assert settlement.calls == 0  # 门禁与采集都没跑
    assert bot._kv[key].get("blocked") is True  # 标记仍在


def test_blocked_marker_changed_fingerprint_repushes():
    """成员指纹变了 → blocked 失效，重新评估并推送（标记被 pushed 覆盖）。"""
    bot, settlement = _tick_bot()
    contest = _contest()
    key = f"settle_{TICK_GROUP.group_id}_codeforces_{contest.contest_id}"
    bot._kv[key] = {
        "blocked": True,
        "reason": push_health.K_PERMISSION,
        "members": "rows:stale-fingerprint",
    }
    assert asyncio.run(bot.tick_settlements()) == 1
    marker = bot._kv[key]
    assert isinstance(marker, dict) and marker.get("state") == "pushed"
    assert bot._sent  # 真的发出去了


def test_push_failure_after_denial_writes_blocked():
    """真实尝试失败且群已进入暂停 → 写 blocked（reason 取自健康状态，台账 M4.1）。"""
    bot, settlement = _tick_bot()

    async def denying_send(group, text, **kwargs):
        bot._cache_health(
            KEY,
            {
                "suspended_until": time.time() + 600,
                "state": "denied",
                "count": 1,
                "last_error_class": push_health.K_PERMISSION,
            },
        )
        return False

    bot.send_notification = denying_send
    contest = _contest()
    key = f"settle_{TICK_GROUP.group_id}_codeforces_{contest.contest_id}"
    assert asyncio.run(bot.tick_settlements()) == 0
    marker = bot._kv.get(key)
    assert isinstance(marker, dict) and marker.get("blocked") is True
    assert marker.get("reason") == push_health.K_PERMISSION
    assert str(marker.get("members") or "")  # 带成员指纹（指纹变化才重评的依据）



def test_settings_denied_keys_are_clamped():
    """两个新设置项走有界整数解析：越界自动收敛到合法范围。"""
    bot = m.AcmerGroupBot.__new__(m.AcmerGroupBot)
    bot._settings_cache = None  # get_settings 直接读该属性（非惰性）
    kv = {
        "settings": {
            "push_denied_threshold": 0,        # 越界 → 有界整数助手按语义回落默认值
            "push_denied_probe_minutes": 999,  # 越界 → 回落默认值
        }
    }

    async def get_kv_data(key, default=None):
        return kv.get(key, default)

    async def put_kv_data(key, value):
        kv[key] = value

    bot.get_kv_data = get_kv_data
    bot.put_kv_data = put_kv_data
    settings = asyncio.run(bot.get_settings())
    assert settings["push_denied_threshold"] == push_health.DEFAULT_DENIED_THRESHOLD
    assert settings["push_denied_probe_minutes"] == push_health.DEFAULT_PROBE_MINUTES
    # 合法值原样通过
    kv["settings"] = {"push_denied_threshold": 7, "push_denied_probe_minutes": 120}
    bot._settings_cache = None
    settings_in = asyncio.run(bot.get_settings())
    assert settings_in["push_denied_threshold"] == 7
    assert settings_in["push_denied_probe_minutes"] == 120
    # 未配置 → 默认值
    kv["settings"] = {}
    bot._settings_cache = None
    settings2 = asyncio.run(bot.get_settings())
    assert settings2["push_denied_threshold"] == push_health.DEFAULT_DENIED_THRESHOLD
    assert settings2["push_denied_probe_minutes"] == push_health.DEFAULT_PROBE_MINUTES


def test_group_readiness_carries_push_health():
    """就绪列数据源必须带 push_health（前端六态的依据）。"""
    bot, kv, sent = _send_bot(suspended=False)
    bot.context = types.SimpleNamespace()
    import platform_compat as _pc
    from unittest import mock
    with mock.patch.object(m, "platform_channel_of", return_value="official"):
        plain = asyncio.run(bot._group_readiness(TICK_GROUP))
    assert plain["push_health"] == {}  # 无状态时给空对象（避免 **None 解包炸）
    bot._cache_health(
        KEY,
        {"state": "denied", "count": 6, "suspended_until": time.time() + 300},
    )
    with mock.patch.object(m, "platform_channel_of", return_value="official"):
        health_view = asyncio.run(bot._group_readiness(TICK_GROUP))
    assert health_view["push_health"]["state"] == "denied"
    assert health_view["push_health"]["count"] == 6
    assert health_view["channel"] == "official"



def _seed(bot, kv, key, value):
    """缓存与 KV 同步播种（_cache_health 只写缓存，断言读的是 KV）。"""
    kv[key] = value
    bot._cache_health(key, value)


def test_probe_on_message_lifts_then_throttles():
    """群消息探测：首次放行窗口并写节流；节流内不再放行；已到期不写（台账 M6.1）。"""
    bot, kv, sent = _send_bot(suspended=True)
    key = KEY
    pid = TICK_GROUP.platform_id or ""
    # 1) 首次：放行窗口 + 默认 60 分钟节流
    asyncio.run(bot._probe_on_group_message(TICK_GROUP.group_id, pid))
    state = kv[key]
    assert state["suspended_until"] <= time.time()  # 窗口已放行
    assert state["probe_at"] >= time.time() + 59 * 60
    assert asyncio.run(bot.push_suspended(TICK_GROUP)) is False
    # 2) 节流内：重新立起暂停后再来消息 → 不再放行
    _seed(bot, kv, key, dict(state, suspended_until=time.time() + 500))
    asyncio.run(bot._probe_on_group_message(TICK_GROUP.group_id, pid))
    assert kv[key]["suspended_until"] > time.time()
    # 3) 暂停已到期：什么都不写
    _seed(bot, kv, key, dict(state, suspended_until=time.time() - 1, probe_at=0))
    asyncio.run(bot._probe_on_group_message(TICK_GROUP.group_id, pid))
    assert kv[key]["probe_at"] == 0


def test_success_self_heals_and_logs_recovery(caplog):
    """bypass 放行后发送成功 → 删除状态（自愈）并打恢复 INFO（M6.2）。"""
    bot, kv, sent = _send_bot(suspended=True)
    original = m.platform_compat.channel_of_platform_id
    m.platform_compat.channel_of_platform_id = lambda context, pid: "onebot"
    try:
        with caplog.at_level(logging.INFO, logger="astrbot"):
            assert (
                asyncio.run(bot.send_notification(TICK_GROUP, "hello", bypass_suspend=True))
                is True
            )
    finally:
        m.platform_compat.channel_of_platform_id = original
    assert KEY not in kv  # 键已删
    assert any("推送恢复正常" in r.getMessage() for r in caplog.records)
    assert sent == ["hello"]


def test_enable_transition_clears_state_only_on_false_to_true():
    """后台停用→启用 = 人工恢复：清暂停；原本就启用的保存不清（台账 M6.3）。"""
    bot, kv, sent = _send_bot(suspended=False)
    bot.settlement = types.SimpleNamespace(_recent={})
    k = push_health.health_key("爱莉希雅", "G1")
    old_key = "爱莉希雅:G1"
    _seed(bot, kv, k, {"state": "denied", "count": 3, "suspended_until": time.time() + 999})

    async def raw_old(fresh=False):
        return {old_key: {"group_id": "G1", "platform_id": "爱莉希雅", "enabled": False}}

    bot._raw_groups = raw_old
    asyncio.run(
        bot._handle_group_enable_transitions(
            {old_key: {"group_id": "G1", "platform_id": "爱莉希雅", "enabled": True}}
        )
    )
    assert k not in kv  # 停用→启用：清掉了
    # 反例：原本就启用 → 不动状态
    _seed(bot, kv, k, {"state": "denied", "count": 2, "suspended_until": time.time() + 999})

    async def raw_same(fresh=False):
        return {old_key: {"group_id": "G1", "platform_id": "爱莉希雅", "enabled": True}}

    bot._raw_groups = raw_same
    asyncio.run(
        bot._handle_group_enable_transitions(
            {old_key: {"group_id": "G1", "platform_id": "爱莉希雅", "enabled": True}}
        )
    )
    assert k in kv  # 非启用转换：保留


def test_maybe_prune_blocked_runs_once_per_day():
    """每日清理按日期去重：同日只跑一次，次日再跑（台账 M6.4）。"""
    from datetime import datetime

    from src.models import CN_TZ
    from src.scheduler import PushScheduler

    runs = []

    class FakePlugin:
        async def _prune_blocked_markers(self):
            runs.append(1)

    sch = PushScheduler(FakePlugin())
    asyncio.run(sch._maybe_prune_blocked(datetime(2026, 9, 28, 1, 0, tzinfo=CN_TZ)))
    asyncio.run(sch._maybe_prune_blocked(datetime(2026, 9, 28, 23, 59, tzinfo=CN_TZ)))
    assert runs == [1]
    asyncio.run(sch._maybe_prune_blocked(datetime(2026, 9, 29, 0, 1, tzinfo=CN_TZ)))
    assert runs == [1, 1]

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
