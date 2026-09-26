"""账号绑定「编辑绑定」修复的回归测试。

覆盖：
- 牛客/洛谷编辑不再因为回填展示名而保存失败（⓪）
- 账号未变时不重新校验、不联网抓取（③），并沿用原 verified_at
- replaced 只在真的换绑时出现（⑥）
- 归属群三态：空值不动成员资格（①）
- 批量查归属群（列表展示用）
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.account_registry import AccountRegistry
from src.account_store import AccountStore
from test_main_accounts import _load_main_module

#: _load_main_module() 会缓存模块，本文件替换过 json_response / error_response /
#: request 这些模块级名字，必须还原，否则会污染后续测试文件（test_web_admin.py）。
_PATCHED_GLOBALS = ("json_response", "error_response", "request")


@pytest.fixture(autouse=True)
def _restore_main_module_globals():
    module = _load_main_module()
    saved = {name: (hasattr(module, name), getattr(module, name, None)) for name in _PATCHED_GLOBALS}
    yield
    for name, (present, value) in saved.items():
        if present:
            setattr(module, name, value)
        elif hasattr(module, name):
            delattr(module, name)


class FakeKV:
    def __init__(self):
        self.data = {}

    async def get(self, key, default=None):
        return self.data.get(key, default)

    async def put(self, key, value):
        self.data[key] = value


class FakeRegistry:
    """记录 save_binding 的调用参数，并返回预置的现有绑定。"""

    def __init__(self, existing=None):
        self.existing = dict(existing or {})
        self.saved = []
        self.groups_calls = []
        self.rank_members = []

    async def add_rank_member(self, group_id, user_id, *, added_by="", note=""):
        self.rank_members.append((group_id, user_id, added_by))
        return {"group_id": group_id, "user_id": user_id, "preexisting": False}

    async def get_user_accounts(self, user_id):
        return {platform: dict(record) for platform, record in self.existing.items()}

    async def save_binding(self, user_id, platform, profile, **kwargs):
        self.saved.append(
            {
                "user_id": user_id,
                "platform": platform,
                "handle": profile.handle,
                "platform_user_id": profile.platform_user_id,
                **kwargs,
            }
        )

    async def get_groups_for_users(self, user_ids):
        self.groups_calls.append(list(user_ids))
        return {str(uid): [{"group_id": "g1", "manual": False}] for uid in user_ids}

    async def get_all_accounts(self):
        return {"u1": {"nowcoder": dict(self.existing.get("nowcoder", {}))}}


class FakeFetcher:
    def __init__(self):
        self.calls = []

    @staticmethod
    def invalid_identifier_message(platform):
        return {
            "nowcoder": "请填写牛客数字用户 ID 或个人主页链接",
            "luogu": "请填写洛谷数字 UID 或个人主页链接",
        }.get(platform, "账号格式不正确")

    async def get_profile(self, platform, identifier, **kwargs):
        self.calls.append((platform, identifier))
        return types.SimpleNamespace(
            platform=platform,
            handle=identifier,
            platform_user_id=identifier,
            display_name=identifier,
            profile_url="",
        )


def _body(payload):
    class _Req:
        async def json(self, default=None):
            return payload

    return _Req()


def _bot(m, *, existing=None, registry=None, fetcher=None):
    bot = m.AcmerGroupBot.__new__(m.AcmerGroupBot)
    bot.account_registry = registry or FakeRegistry(existing)
    bot.account_fetcher = fetcher or FakeFetcher()
    bot._invalidate_all_rank_cache = lambda: None
    return bot


def _patch_response(m):
    m.json_response = lambda value: {"json": value}
    m.error_response = lambda message: {"error": message}


def _nowcoder_record(handle="td1336065617", pid="645160704", verified_at=111.0):
    return {
        "platform": "nowcoder",
        "handle": handle,
        "platform_user_id": pid,
        "display_name": handle,
        "profile_url": "",
        "verified_at": verified_at,
    }


def test_get_groups_for_users_merges_sources(tmp_path):
    async def scenario():
        store = AccountStore(tmp_path / "acmer_store.db")
        await store.initialize()
        await store.set_group_member("g1", "u1", True)          # 自然成员
        await store.set_group_member("g1", "u2", False)          # 已退出 → 不该出现
        await store.add_rank_member_override("g2", "u1", added_by="webui")  # 强制加入
        await store.add_rank_member_override("g3", "u3", added_by="webui")

        result = await store.get_groups_for_users(["u1", "u2", "u3", "u404"])
        assert sorted((item["group_id"], item["manual"]) for item in result["u1"]) == [
            ("g1", False), ("g2", True),
        ]
        assert "u2" not in result
        assert result["u3"] == [{"group_id": "g3", "manual": True}]
        assert "u404" not in result
        await store.close()

    asyncio.run(scenario())


def test_registry_get_groups_for_users_passthrough(tmp_path):
    async def scenario():
        registry = AccountRegistry(FakeKV())
        registry.store = AccountStore(tmp_path / "acmer_store.db")
        await registry.store.initialize()
        registry.store_enabled = True
        await registry.set_group_member("g1", "u1", True)
        result = await registry.get_groups_for_users(["u1"])
        assert result == {"u1": [{"group_id": "g1", "manual": False}]}
        assert await registry.get_groups_for_users([]) == {}
        await registry.close()

    asyncio.run(scenario())


def test_save_same_account_skips_fetch():
    """账号没变（只改昵称）：不联网、不重新校验、沿用 verified_at。"""

    async def scenario():
        m = _load_main_module()
        _patch_response(m)
        fetcher = FakeFetcher()
        registry = FakeRegistry({"nowcoder": _nowcoder_record()})
        bot = _bot(m, registry=registry, fetcher=fetcher)
        m.request = _body(
            {
                "action": "save",
                "user_id": "u1",
                "platform": "nowcoder",
                "identifier": "645160704",     # = platform_user_id
                "qq_name": "新昵称",
                "group_id": "",
            }
        )
        result = await m.AcmerGroupBot._web_bindings_write(bot)
        assert fetcher.calls == []
        assert result["json"]["data"]["reused"] is True
        assert result["json"]["data"]["replaced"] is None
        assert registry.saved[0]["qq_name"] == "新昵称"
        assert registry.saved[0]["verified_at"] == 111.0
        assert registry.saved[0]["group_id"] is None      # 空值不动成员资格

    asyncio.run(scenario())


def test_save_display_name_unchanged_is_tolerated():
    """牛客/洛谷：前端若回填展示名（与 platform_user_id 不同但等于当前值）也不该报错。"""

    async def scenario():
        m = _load_main_module()
        _patch_response(m)
        fetcher = FakeFetcher()
        registry = FakeRegistry({"nowcoder": _nowcoder_record()})
        bot = _bot(m, registry=registry, fetcher=fetcher)
        m.request = _body(
            {
                "action": "save",
                "user_id": "u1",
                "platform": "nowcoder",
                "identifier": "td1336065617",   # = handle（展示名）
                "qq_name": "改个昵称",
                "group_id": "",
            }
        )
        result = await m.AcmerGroupBot._web_bindings_write(bot)
        assert "error" not in result, result
        assert fetcher.calls == []
        assert result["json"]["data"]["reused"] is True
        assert registry.saved[0]["handle"] == "td1336065617"
        assert registry.saved[0]["platform_user_id"] == "645160704"

    asyncio.run(scenario())


def test_save_changed_account_fetches_and_reports_replaced():
    async def scenario():
        m = _load_main_module()
        _patch_response(m)
        fetcher = FakeFetcher()
        registry = FakeRegistry({"nowcoder": _nowcoder_record()})
        bot = _bot(m, registry=registry, fetcher=fetcher)
        m.request = _body(
            {
                "action": "save",
                "user_id": "u1",
                "platform": "nowcoder",
                "identifier": "999888777",
                "qq_name": "换绑用户",
                "group_id": "g9",
            }
        )
        result = await m.AcmerGroupBot._web_bindings_write(bot)
        assert fetcher.calls == [("nowcoder", "999888777")]
        assert result["json"]["data"]["reused"] is False
        assert result["json"]["data"]["replaced"] == "td1336065617"
        # 归属群不再传给 save_binding，而是走覆盖表（带留痕）
        assert registry.saved[0]["group_id"] is None
        assert registry.saved[0]["verified_at"] is None
        assert registry.rank_members == [("g9", "u1", "webui:binding")]
        assert result["json"]["data"]["membership_added"] is True

    asyncio.run(scenario())


def test_save_nickname_only_does_not_report_replaced():
    """⑥：编辑已存在绑定但账号没变，不该提示“已换绑”。"""

    async def scenario():
        m = _load_main_module()
        _patch_response(m)
        registry = FakeRegistry({"nowcoder": _nowcoder_record()})
        bot = _bot(m, registry=registry)
        m.request = _body(
            {
                "action": "save",
                "user_id": "u1",
                "platform": "nowcoder",
                "identifier": "645160704",
                "qq_name": "只改昵称",
                "group_id": "",
            }
        )
        result = await m.AcmerGroupBot._web_bindings_write(bot)
        assert result["json"]["data"]["replaced"] is None

    asyncio.run(scenario())


def test_save_invalid_identifier_without_existing_binding():
    async def scenario():
        m = _load_main_module()
        _patch_response(m)
        registry = FakeRegistry({})
        bot = _bot(m, registry=registry)
        m.request = _body(
            {
                "action": "save",
                "user_id": "u9",
                "platform": "nowcoder",
                "identifier": "乱七八糟",
                "qq_name": "",
                "group_id": "",
            }
        )
        result = await m.AcmerGroupBot._web_bindings_write(bot)
        assert "error" in result, result
        assert "牛客" in result["error"]

    asyncio.run(scenario())


def test_bindings_list_attaches_groups():
    async def scenario():
        m = _load_main_module()
        _patch_response(m)
        registry = FakeRegistry({"nowcoder": _nowcoder_record()})
        bot = _bot(m, registry=registry)
        bot._query_param = lambda name, default="": ""
        result = await m.AcmerGroupBot._web_bindings_list(bot)
        item = result["json"]["data"]["items"][0]
        assert item["groups"] == [{"group_id": "g1", "manual": False}]
        assert registry.groups_calls == [["u1"]]

    asyncio.run(scenario())


def test_save_without_group_does_not_touch_membership():
    """新增/编辑不带归属群 → 不写覆盖表，成员资格完全不动。"""

    async def scenario():
        m = _load_main_module()
        _patch_response(m)
        registry = FakeRegistry({})
        bot = _bot(m, registry=registry)
        m.request = _body(
            {
                "action": "save",
                "user_id": "u7",
                "platform": "codeforces",
                "identifier": "newbie",
                "qq_name": "新人",
                "group_id": "",
            }
        )
        result = await m.AcmerGroupBot._web_bindings_write(bot)
        assert "error" not in result, result
        assert registry.rank_members == []
        assert result["json"]["data"]["membership_added"] is False

    asyncio.run(scenario())


def test_save_join_group_failure_is_reported_after_binding_saved():
    """加群排行失败时：绑定已保存，但明确告知去「排行成员」重试。"""

    async def scenario():
        m = _load_main_module()
        _patch_response(m)
        registry = FakeRegistry({})

        async def boom(group_id, user_id, **kwargs):
            raise RuntimeError("db busy")

        registry.add_rank_member = boom
        bot = _bot(m, registry=registry)
        m.request = _body(
            {
                "action": "save",
                "user_id": "u8",
                "platform": "codeforces",
                "identifier": "newbie2",
                "qq_name": "",
                "group_id": "g1",
            }
        )
        result = await m.AcmerGroupBot._web_bindings_write(bot)
        assert "error" in result, result
        assert "排行成员" in result["error"]
        # 绑定本身已经写进去了
        assert registry.saved and registry.saved[0]["user_id"] == "u8"

    asyncio.run(scenario())
