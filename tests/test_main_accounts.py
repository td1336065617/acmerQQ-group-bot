"""主程序账号命令的回归测试。"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import types
from pathlib import Path

from src.account_models import AccountFetchError, AccountProfile

ROOT = Path(__file__).resolve().parent.parent


def _load_main_module():
    package_name = "_acmer_qq_group_bot_package"
    module_name = f"{package_name}.main_test"
    loaded = sys.modules.get(module_name)
    if loaded is not None:
        return loaded

    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    api.logger = getattr(api, "logger", logging.getLogger("astrbot"))

    event_module = types.ModuleType("astrbot.api.event")

    class DummyFilter:
        class PlatformAdapterType:
            QQOFFICIAL = "qq_official"

        class EventMessageType:
            GROUP_MESSAGE = 1
            PRIVATE_MESSAGE = 2

        @staticmethod
        def platform_adapter_type(*args, **kwargs):
            return lambda function: function

        @staticmethod
        def event_message_type(*args, **kwargs):
            return lambda function: function

    event_module.AstrMessageEvent = object
    event_module.MessageChain = list
    event_module.filter = DummyFilter

    message_components = types.ModuleType(
        "astrbot.api.message_components"
    )

    class DummyImage:
        @staticmethod
        def fromFileSystem(path):
            return path

    class DummyPlain:
        def __init__(self, text):
            self.text = text

    message_components.Image = DummyImage
    message_components.Plain = DummyPlain

    platform_module = types.ModuleType("astrbot.api.platform")
    platform_module.MessageType = types.SimpleNamespace(
        GROUP_MESSAGE="group",
        PRIVATE_MESSAGE="private",
    )

    star_module = types.ModuleType("astrbot.api.star")

    class DummyStar:
        def __init__(self, *args, **kwargs):
            pass

    star_module.Context = object
    star_module.Star = DummyStar

    web_module = types.ModuleType("astrbot.api.web")
    web_module.error_response = lambda value: value
    web_module.json_response = lambda value: value
    web_module.request = None

    session_module = types.ModuleType(
        "astrbot.core.platform.message_session"
    )
    session_module.MessageSesion = lambda **kwargs: kwargs

    modules = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event_module,
        "astrbot.api.message_components": message_components,
        "astrbot.api.platform": platform_module,
        "astrbot.api.star": star_module,
        "astrbot.api.web": web_module,
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.platform": types.ModuleType(
            "astrbot.core.platform"
        ),
        "astrbot.core.platform.message_session": session_module,
    }
    sys.modules.update(modules)

    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    sys.modules[package_name] = package
    spec = importlib.util.spec_from_file_location(
        module_name,
        ROOT / "main.py",
        submodule_search_locations=[str(ROOT)],
    )
    if spec is None or spec.loader is None:
        raise AssertionError("无法加载 main.py 测试模块")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class FakeEvent:
    def __init__(self, *, group_id: str = ""):
        self.group_id = group_id
        self.results = []

    def get_sender_id(self):
        return "qq-user"

    def get_group_id(self):
        return self.group_id

    def get_sender_name(self):
        return "测试用户"

    def plain_result(self, text):
        self.results.append(text)
        return text


class FakeRegistry:
    def __init__(self, pending, *, token_valid=True):
        self.pending = pending
        self.token_valid = token_valid
        self.saved = None
        self.clear_calls = 0
        self.matched_values = []

    async def get_pending(self, user_id, platform):
        return self.pending

    async def create_pending(self, *args, **kwargs):
        return "ACM-ABCDEFGH"

    async def clear_pending(self, user_id, platform):
        self.clear_calls += 1

    def token_matches(self, value, expected_hash):
        self.matched_values.append(value)
        return self.token_valid

    async def save_binding(self, *args, **kwargs):
        self.saved = (args, kwargs)


class FakeFetcher:
    def __init__(self, profile):
        self.profile = profile
        self.calls = []

    async def get_profile(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.profile


class LuoguVerificationFetcher(FakeFetcher):
    def __init__(self, profile, verification_value):
        super().__init__(profile)
        self.verification_value = verification_value
        self.verification_calls = []

    async def get_verification_value(self, *args, **kwargs):
        self.verification_calls.append((args, kwargs))
        return self.verification_value


class FailingVerificationFetcher(FakeFetcher):
    async def get_verification_value(self, *args, **kwargs):
        raise AccountFetchError("洛谷个人资料暂时无法读取")


def _build_bot(main_module, registry, fetcher):
    bot = main_module.AcmerGroupBot.__new__(main_module.AcmerGroupBot)
    bot.account_registry = registry
    bot.account_fetcher = fetcher
    bot._invalidate_all_rank_cache = lambda: None

    async def record_metric(*args, **kwargs):
        return None

    bot._record_profile_metric = record_metric
    return bot


def _collect(async_generator):
    async def scenario():
        return [item async for item in async_generator]

    return asyncio.run(scenario())


def test_confirm_binding_rejects_invalid_identifier_without_fetching():
    main_module = _load_main_module()
    profile = AccountProfile(
        platform="codeforces",
        handle="demo",
        platform_user_id="demo",
        verification_value="ACM-ABCDEFGH",
    )
    pending = {
        "platform": "codeforces",
        "handle": "demo",
        "platform_user_id": "demo",
        "token_hash": "hash",
        "group_id": "source-group",
    }
    registry = FakeRegistry(pending)
    fetcher = FakeFetcher(profile)
    bot = _build_bot(main_module, registry, fetcher)

    results = _collect(
        bot._reply_account_confirm(
            FakeEvent(group_id="source-group"),
            "codeforces",
            "invalid!",
        )
    )

    assert len(results) == 1
    assert "账号参数格式不正确" in results[0]
    assert fetcher.calls == []
    assert registry.saved is None


def test_bind_prompt_keeps_verification_instructions():
    main_module = _load_main_module()
    profile = AccountProfile(
        platform="codeforces",
        handle="demo",
        platform_user_id="demo",
    )
    registry = FakeRegistry(None)
    fetcher = FakeFetcher(profile)
    bot = _build_bot(main_module, registry, fetcher)

    results = _collect(
        bot._reply_account_bind(
            FakeEvent(group_id="source-group"),
            "codeforces",
            "demo",
        )
    )

    assert len(results) == 1
    assert "ACM-ABCDEFGH" in results[0]
    assert "姓氏（Last name）" in results[0]
    assert "确认绑定cf" in results[0]


def test_luogu_bind_uses_separate_verification_source():
    main_module = _load_main_module()
    profile = AccountProfile(
        platform="luogu",
        handle="demo",
        platform_user_id="1770958",
        verification_value="",
    )
    registry = FakeRegistry(None)
    fetcher = LuoguVerificationFetcher(profile, "luogu.me ACM-ABCDEFGH")
    bot = _build_bot(main_module, registry, fetcher)

    results = _collect(
        bot._reply_account_bind(
            FakeEvent(group_id="source-group"),
            "luogu",
            "1770958",
        )
    )

    assert results
    assert "已找到 洛谷 账号：demo" in results[0]
    assert fetcher.verification_calls
    assert registry.pending is None


def test_luogu_verification_failure_keeps_pending_binding():
    main_module = _load_main_module()
    profile = AccountProfile(
        platform="luogu",
        handle="demo",
        platform_user_id="1770958",
    )
    pending = {
        "platform": "luogu",
        "handle": "demo",
        "platform_user_id": "1770958",
        "token_hash": "hash",
        "group_id": "source-group",
    }
    registry = FakeRegistry(pending)
    fetcher = FailingVerificationFetcher(profile)
    bot = _build_bot(main_module, registry, fetcher)

    results = _collect(
        bot._reply_account_confirm(
            FakeEvent(group_id="source-group"),
            "luogu",
        )
    )

    assert results
    assert "暂时无法绑定" in results[0]
    assert registry.clear_calls == 0
    assert registry.saved is None


def test_luogu_confirmation_checks_me_introduction():
    main_module = _load_main_module()
    profile = AccountProfile(
        platform="luogu",
        handle="demo",
        platform_user_id="1770958",
    )
    pending = {
        "platform": "luogu",
        "handle": "demo",
        "platform_user_id": "1770958",
        "token_hash": "hash",
        "group_id": "source-group",
    }
    registry = FakeRegistry(pending)
    fetcher = LuoguVerificationFetcher(profile, "luogu.me ACM-ABCDEFGH")
    bot = _build_bot(main_module, registry, fetcher)

    results = _collect(
        bot._reply_account_confirm(
            FakeEvent(group_id="source-group"),
            "luogu",
        )
    )

    assert results
    assert "绑定成功" in results[0]
    assert registry.matched_values == ["luogu.me ACM-ABCDEFGH"]
    assert registry.saved is not None


def test_confirm_binding_keeps_origin_group_for_rank_auto_join():
    main_module = _load_main_module()
    profile = AccountProfile(
        platform="codeforces",
        handle="demo",
        platform_user_id="demo",
        verification_value="ACM-ABCDEFGH",
    )
    pending = {
        "platform": "codeforces",
        "handle": "demo",
        "platform_user_id": "demo",
        "token_hash": "hash",
        "group_id": "origin-group",
    }
    registry = FakeRegistry(pending)
    fetcher = FakeFetcher(profile)
    bot = _build_bot(main_module, registry, fetcher)

    results = _collect(
        bot._reply_account_confirm(
            FakeEvent(group_id="confirm-group"),
            "codeforces",
        )
    )

    assert fetcher.calls
    assert registry.saved is not None
    assert registry.saved[1]["group_id"] == "origin-group"
    assert "发起绑定群" in results[0]


def test_profile_card_render_failure_is_safe():
    main_module = _load_main_module()
    bot = main_module.AcmerGroupBot.__new__(main_module.AcmerGroupBot)

    class BrokenRenderer:
        def render_profile(self, *args, **kwargs):
            raise RuntimeError("renderer down")

    bot.account_card_renderer = BrokenRenderer()
    result = asyncio.run(
        bot._render_profile_card(
            [],
            display_name="测试用户",
            weekly_changes={},
            group_ranks={},
        )
    )

    assert result is None
