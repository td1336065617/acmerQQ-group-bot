"""会话场景预热测试（1.20.1）：整进程重启后自动恢复官方群主动推送。

背景：QQ 官方通道的会话场景只存在进程内存里，重启后必须群内发消息才能恢复；
生产实测一次重启导致 13/17 个群收不到赛前提醒。
"""
from __future__ import annotations

import asyncio
import sys
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
    assert getattr(bot, "_session_warm_done", False) is False  # 未放弃，下个 tick 继续
    official = FakeInst("爱莉希雅", "qq_official")
    bot.context = FakeContext([official])
    assert asyncio.run(bot.ensure_session_scenes()) == 1
    assert official._session_scene == {"G1": "group"}
