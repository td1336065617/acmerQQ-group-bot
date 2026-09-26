"""后台「强制加入群排行」测试：覆盖表语义、成员读取与 KV 回退。"""
from __future__ import annotations

import asyncio

from src.account_registry import AccountRegistry
from src.account_store import AccountStore


class FakePlugin:
    def __init__(self):
        self.store = {}

    async def get_kv_data(self, key, default=None):
        return self.store.get(key, default)

    async def put_kv_data(self, key, value):
        self.store[key] = value


async def _store(tmp_path) -> AccountStore:
    store = AccountStore(tmp_path / "acmer_store.db")
    await store.initialize()
    return store


def _seed_account(store: AccountStore, user_id: str, platform="codeforces", handle="h"):
    conn = store._connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO accounts(user_id, platform, handle, platform_user_id,"
                " display_name, profile_url, qq_name, verified_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (user_id, platform, handle, handle, handle, "", "", 1.0),
            )
    finally:
        conn.close()


def test_override_keeps_member_after_user_opt_out(tmp_path):
    """强制加入后，用户自己发「退出排行」也不该掉出成员列表。"""

    async def scenario():
        store = await _store(tmp_path)
        await store.set_group_member("g1", "u1", True)
        await store.add_rank_member_override("g1", "u1", added_by="webui")
        await store.set_group_member("g1", "u1", False)  # 用户自己退出
        assert await store.get_group_member_ids("g1") == ["u1"]
        await store.close()

    asyncio.run(scenario())


def test_accounts_query_includes_forced_member(tmp_path):
    """排行抓取的账号来源（get_group_platform_accounts）必须带上强制加入的人。"""

    async def scenario():
        store = await _store(tmp_path)
        _seed_account(store, "u2", handle="forced")
        rows_before = await store.get_group_platform_accounts("g1", "codeforces")
        assert rows_before == []

        await store.add_rank_member_override("g1", "u2", added_by="webui")
        rows = await store.get_group_platform_accounts("g1", "codeforces")
        assert [row["user_id"] for row in rows] == ["u2"]
        assert rows[0]["handle"] == "forced"
        await store.close()

    asyncio.run(scenario())


def test_remove_override_restores_previous_state(tmp_path):
    async def scenario():
        store = await _store(tmp_path)
        # A) 本来就是启用成员：移除强制标记后仍是成员
        await store.set_group_member("g1", "u1", True)
        await store.add_rank_member_override("g1", "u1", added_by="webui")
        assert (await store.get_rank_member_override("g1", "u1"))["preexisting"] == 1
        await store.remove_rank_member_override("g1", "u1")
        assert await store.get_group_member_ids("g1") == ["u1"]

        # B) 原本不在排行里：移除后应消失
        await store.add_rank_member_override("g1", "u9", added_by="webui")
        assert (await store.get_rank_member_override("g1", "u9"))["preexisting"] == 0
        assert sorted(await store.get_group_member_ids("g1")) == ["u1", "u9"]
        await store.remove_rank_member_override("g1", "u9")
        assert await store.get_group_member_ids("g1") == ["u1"]
        await store.close()

    asyncio.run(scenario())


def test_list_rank_members_marks_source(tmp_path):
    async def scenario():
        store = await _store(tmp_path)
        await store.set_group_member("g1", "u1", True)  # 自然成员
        await store.add_rank_member_override("g1", "u2", added_by="webui", note="手动")
        rows = await store.list_rank_member_overrides("g1")
        assert [row["user_id"] for row in rows] == ["u2"]
        assert rows[0]["added_by"] == "webui"

        members = await store.get_group_member_ids("g1")
        assert sorted(members) == ["u1", "u2"]
        await store.close()

    asyncio.run(scenario())


def test_registry_add_and_remove_rank_member(tmp_path):
    async def scenario():
        plugin = FakePlugin()
        registry = AccountRegistry(plugin)
        registry.store = await _store(tmp_path)
        registry.store_enabled = True

        result = await registry.add_rank_member("g1", "u1", added_by="webui")
        assert result["preexisting"] is False
        members = await registry.list_rank_members("g1")
        assert [item["user_id"] for item in members] == ["u1"]
        assert members[0]["manual"] is True
        assert members[0]["added_by"] == "webui"

        removed = await registry.remove_rank_member("g1", "u1")
        assert removed["restored"] is False
        assert await registry.list_rank_members("g1") == []
        await registry.close()

    asyncio.run(scenario())


def test_registry_remove_keeps_preexisting_member(tmp_path):
    async def scenario():
        plugin = FakePlugin()
        registry = AccountRegistry(plugin)
        registry.store = await _store(tmp_path)
        registry.store_enabled = True
        await registry.set_group_member("g1", "u1", True)

        result = await registry.add_rank_member("g1", "u1", added_by="webui")
        assert result["preexisting"] is True
        removed = await registry.remove_rank_member("g1", "u1")
        assert removed["restored"] is True
        # 去掉强制标记后，本人仍是启用成员
        assert await registry.get_group_member_ids("g1") == ["u1"]
        await registry.close()

    asyncio.run(scenario())


def test_registry_kv_fallback(tmp_path):
    """未启用 SQLite 时走 KV：加入/列出/移除仍然可用。"""

    async def scenario():
        registry = AccountRegistry(FakePlugin())
        await registry.add_rank_member("g1", "u1", added_by="webui")
        members = await registry.list_rank_members("g1")
        assert [item["user_id"] for item in members] == ["u1"]
        assert members[0]["manual"] is True

        await registry.remove_rank_member("g1", "u1")
        assert await registry.list_rank_members("g1") == []

    asyncio.run(scenario())
