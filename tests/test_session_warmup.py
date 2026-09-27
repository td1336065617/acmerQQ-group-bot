"""会话场景预热测试（1.20.1）：整进程重启后自动恢复官方群主动推送。

背景：QQ 官方通道的会话场景只存在进程内存里，重启后必须群内发消息才能恢复；
生产实测一次重启导致 13/17 个群收不到赛前提醒。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import platform_compat
from platform_compat import warm_scene
from src.models import GroupConfig

from test_main_accounts import _load_main_module


class FakeMeta:
    def __init__(self, platform_id: str, name: str) -> None:
        self.id = platform_id
        self.name = name


class FakeInst:
    def __init__(self, platform_id: str, name: str) -> None:
        self._meta = FakeMeta(platform_id, name)
        self._session_scene: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []

    def meta(self):
        return self._meta

    def remember_session_scene(self, session_id: str, scene: str) -> None:
        self.calls.append((session_id, scene))
        self._session_scene[session_id] = scene


class FakeContext:
    def __init__(self, insts) -> None:
        self.platform_manager = type("PM", (), {"platform_insts": list(insts)})()


def test_warm_scene_official_only():
    official = FakeInst("爱莉希雅", "qq_official")
    onebot = FakeInst("爱莉希雅2", "aiocqhttp")
    ctx = FakeContext([official, onebot])
    assert warm_scene(ctx, "G1", "爱莉希雅") is True
    assert official.calls == [("G1", "group")]
    # OneBot 通道无会话限制，不做预热
    assert warm_scene(ctx, "G1", "爱莉希雅2") is False
    assert onebot.calls == []


def test_warm_scene_then_scene_ready():
    official = FakeInst("爱莉希雅", "qq_official")
    ctx = FakeContext([official])
    assert platform_compat.scene_ready(ctx, "G1", "爱莉希雅") is False
    assert warm_scene(ctx, "G1", "爱莉希雅") is True
    assert platform_compat.scene_ready(ctx, "G1", "爱莉希雅") is True


def test_warm_scene_ignores_unknown_platform():
    official = FakeInst("爱莉希雅", "qq_official")
    ctx = FakeContext([official])
    assert warm_scene(ctx, "G1", "不存在的平台") is False
    assert official.calls == []


def _bot_with(insts, groups):
    m = _load_main_module()
    bot = m.AcmerGroupBot.__new__(m.AcmerGroupBot)
    bot.context = FakeContext(insts)

    async def get_groups():
        return list(groups)

    async def get_settings():
        return {"session_warmup_enabled": True}

    bot.get_groups = get_groups
    bot.get_settings = get_settings
    return bot


def test_ensure_session_scenes_warms_interacted_groups_only():
    official = FakeInst("爱莉希雅", "qq_official")
    groups = [
        GroupConfig(group_id="G1", platform_id="爱莉希雅", umo="爱莉希雅:GroupMessage:G1"),
        GroupConfig(group_id="G2", platform_id="爱莉希雅", umo=""),  # 从未交互
        GroupConfig(group_id="G3", platform_id="爱莉希雅", umo="umo3", enabled=False),
    ]
    bot = _bot_with([official], groups)
    assert asyncio.run(bot.ensure_session_scenes()) == 1
    assert official._session_scene == {"G1": "group"}
    # 幂等：同一进程内只做一次
    assert asyncio.run(bot.ensure_session_scenes()) == 0


def test_ensure_session_scenes_retries_until_platform_loaded():
    groups = [
        GroupConfig(group_id="G1", platform_id="爱莉希雅", umo="爱莉希雅:GroupMessage:G1"),
    ]
    bot = _bot_with([], groups)  # 平台实例还没加载
    assert asyncio.run(bot.ensure_session_scenes()) == 0
    # 未完成 → 不写入"已预热时间"，下个 tick 继续
    assert float(getattr(bot, "_session_warm_at", 0.0) or 0.0) == 0.0
    official = FakeInst("爱莉希雅", "qq_official")
    bot.context = FakeContext([official])
    assert asyncio.run(bot.ensure_session_scenes()) == 1
    assert official._session_scene == {"G1": "group"}
    assert float(getattr(bot, "_session_warm_at", 0.0) or 0.0) > 0.0


def test_ensure_session_scenes_skips_onebot_only_and_stops_retrying():
    """纯 OneBot 部署：不做预热、直接收工，不空转重试。"""
    calls = {"n": 0}

    class CountingContext(FakeContext):
        pass

    bot = _bot_with([FakeInst("爱莉希雅2", "aiocqhttp")],
                    [GroupConfig(group_id="100", platform_id="爱莉希雅2", umo="爱莉希雅2:GroupMessage:100")])
    original_get_groups = bot.get_groups

    async def counting_get_groups():
        calls["n"] += 1
        return await original_get_groups()

    bot.get_groups = counting_get_groups
    assert asyncio.run(bot.ensure_session_scenes()) == 0
    assert float(getattr(bot, "_session_warm_at", 0.0) or 0.0) > 0.0  # 已完成本轮
    first_calls = calls["n"]
    assert asyncio.run(bot.ensure_session_scenes()) == 0
    assert calls["n"] == first_calls  # 刷新间隔内不再扫描群


def test_ensure_session_scenes_warns_when_platform_mismatch(caplog):
    """官方实例在、但群配置的平台 ID 对不上：给出诊断日志而不是静默放弃。"""
    import logging

    bot = _bot_with(
        [FakeInst("其它平台实例", "qq_official")],
        [GroupConfig(group_id="G1", platform_id="爱莉希雅", umo="爱莉希雅:GroupMessage:G1")],
    )
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(bot.ensure_session_scenes()) == 0
    assert any("未能恢复" in rec.getMessage() for rec in caplog.records)


def test_ensure_session_scenes_repeats_after_refresh_window():
    official = FakeInst("爱莉希雅", "qq_official")
    groups = [GroupConfig(group_id="G1", platform_id="爱莉希雅", umo="爱莉希雅:GroupMessage:G1")]
    bot = _bot_with([official], groups)
    assert asyncio.run(bot.ensure_session_scenes()) == 1
    official._session_scene.clear()  # 模拟平台适配器被重建
    assert asyncio.run(bot.ensure_session_scenes()) == 0        # 刷新间隔内不动
    assert official._session_scene == {}
    bot._session_warm_at = time.time() - 10 ** 4                 # 超过刷新间隔
    assert asyncio.run(bot.ensure_session_scenes()) == 1
    assert official._session_scene == {"G1": "group"}


def test_ensure_session_scenes_gives_up_with_warning(caplog):
    import logging

    bot = _bot_with(
        [FakeInst("其它实例", "qq_official")],
        [GroupConfig(group_id="G1", platform_id="爱莉希雅", umo="u")],
    )
    bot._session_warm_tries = 10 ** 6  # 已超过尝试上限
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(bot.ensure_session_scenes()) == 0
    assert any("未能完成" in rec.getMessage() for rec in caplog.records)


def test_group_readiness_reports_real_capability():
    """WebUI「推送就绪」列取的是会话能力，与历史 activated 标记无关。"""
    official = FakeInst("爱莉希雅", "qq_official")
    onebot = FakeInst("爱莉希雅2", "aiocqhttp")
    bot = _bot_with([official, onebot], [])

    g_off = GroupConfig(group_id="G1", platform_id="爱莉希雅", umo="u")
    assert bot._group_readiness(g_off) == {"channel": "official", "scene_ready": False}
    official._session_scene["G1"] = "group"
    assert bot._group_readiness(g_off) == {"channel": "official", "scene_ready": True}

    g_ob = GroupConfig(group_id="100", platform_id="爱莉希雅2", umo="u")
    assert bot._group_readiness(g_ob) == {"channel": "onebot", "scene_ready": True}

    g_unknown = GroupConfig(group_id="X", platform_id="不存在的平台", umo="u")
    assert bot._group_readiness(g_unknown) == {"channel": "", "scene_ready": False}


def test_config_api_merges_readiness_fields():
    """/config 返回的每个群要带 channel / scene_ready，供前端渲染。"""
    official = FakeInst("爱莉希雅", "qq_official")
    groups = [GroupConfig(group_id="G1", platform_id="爱莉希雅", umo="umo1", activated=True)]
    bot = _bot_with([official], groups)
    m = _load_main_module()
    captured = {}

    def fake_json_response(payload):
        captured.update(payload)
        return payload

    m.json_response = fake_json_response  # 打桩模块级 json_response（实例属性无效）

    async def get_admins():
        return []

    bot._get_admins = get_admins
    bot._default_platform_id = lambda: "爱莉希雅"
    asyncio.run(bot._web_config_get())
    rows = captured["data"]["groups"]
    assert rows and rows[0]["group_id"] == "G1"
    assert rows[0]["channel"] == "official"
    assert rows[0]["scene_ready"] is False
    # 历史激活标记仍在，但不参与就绪判断
    assert rows[0]["activated"] is True

