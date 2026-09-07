"""AccountRegistry + AccountStore 集成测试（迁移 / 双写 / 回退）。"""
from __future__ import annotations

import asyncio
import time

import pytest

from src.account_fetcher import AccountFetcher
from src.account_models import AccountProfile
from src.account_registry import (
    ACCOUNTS_KEY,
    AccountRegistry,
    create_binding_token,
)
from src.account_store import AccountStore


class FakePlugin:
    def __init__(self):
        self.store = {}

    async def get_kv_data(self, key, default=None):
        return self.store.get(key, default)

    async def put_kv_data(self, key, value):
        self.store[key] = value


def _profile(handle: str, platform: str = "codeforces") -> AccountProfile:
    return AccountProfile(
        platform=platform,
        handle=handle,
        platform_user_id=handle,
        display_name=handle,
        profile_url=f"https://codeforces.com/profile/{handle}",
        verification_value="",
    )


def test_store_mode_migrates_from_kv_and_dual_writes(tmp_path):
    async def scenario():
        plugin = FakePlugin()
        now = time.time()
        plugin.store[ACCOUNTS_KEY] = {
            "u1": {
                "codeforces": {
                    "handle": "tourist",
                    "platform_user_id": "Tourist",
                    "display_name": "Tourist",
                    "verified_at": now,
                    "qq_name": "道",
                }
            }
        }
        plugin.store["group_rank_members"] = {
            "g1": {"u1": {"enabled": True, "updated_at": now}}
        }
        plugin.store["pending_account_bindings"] = {}
        plugin.store["account_rating_snapshots"] = {}
        registry = AccountRegistry(plugin)
        await registry.initialize(db_path=tmp_path / "registry.db")
        assert registry.store_enabled is True

        # 迁移后 Store 可读
        accounts = await registry.get_user_accounts("u1")
        assert accounts["codeforces"]["handle"] == "tourist"
        assert await registry.get_group_member_ids("g1") == ["u1"]

        # 双写：新增绑定后 KV 同步包含
        await registry.save_binding(
            "u2",
            "codeforces",
            _profile("alice"),
            group_id="g1",
            qq_name="爱丽丝",
        )
        kv_accounts = plugin.store[ACCOUNTS_KEY]
        assert kv_accounts["u2"]["codeforces"]["handle"] == "alice"
        kv_members = plugin.store["group_rank_members"]["g1"]
        assert kv_members["u2"]["enabled"] is True

        # Store 可读新增绑定
        assert "codeforces" in await registry.get_user_accounts("u2")

        # 双写：Rating 快照
        await registry.record_ratings([("u1", "codeforces", 1800)])
        assert (
            plugin.store["account_rating_snapshots"]["u1"]["codeforces"][-1][
                "rating"
            ]
            == 1800
        )

    asyncio.run(scenario())


def test_store_mode_pending_and_remove(tmp_path):
    async def scenario():
        plugin = FakePlugin()
        registry = AccountRegistry(plugin)
        await registry.initialize(db_path=tmp_path / "registry2.db")
        token = await registry.create_pending(
            "u1", "codeforces", _profile("bob"), group_id="g1"
        )
        assert token == create_binding_token() or len(token) >= 8
        pending = await registry.get_pending("u1", "codeforces")
        assert pending is not None and pending["handle"] == "bob"
        # KV 双写
        assert "u1:codeforces" in plugin.store["pending_account_bindings"]

        await registry.clear_pending("u1", "codeforces")
        assert await registry.get_pending("u1", "codeforces") is None

        await registry.save_binding("u1", "codeforces", _profile("bob"))
        assert await registry.remove_binding("u1", "codeforces") is True
        assert "codeforces" not in await registry.get_user_accounts("u1")

    asyncio.run(scenario())


def test_registry_kv_fallback_when_disabled(tmp_path):
    async def scenario():
        plugin = FakePlugin()
        registry = AccountRegistry(plugin)
        # enable=False：保持纯 KV 模式
        await registry.initialize(
            enable=False, db_path=tmp_path / "ignored.db"
        )
        assert registry.store_enabled is False
        await registry.save_binding(
            "u1", "codeforces", _profile("carol"), group_id="g1"
        )
        assert ACCOUNTS_KEY in plugin.store
        assert await registry.get_user_accounts("u1")
        assert await registry.get_group_member_ids("g1") == ["u1"]

    asyncio.run(scenario())


def test_store_close_and_stats(tmp_path):
    async def scenario():
        store = AccountStore(tmp_path / "stats.db")
        await store.initialize()
        await store.save_binding("u1", "codeforces", {
            "handle": "x",
            "platform_user_id": "X",
            "display_name": "x",
            "verified_at": time.time(),
        })
        stats = await store.stats()
        assert stats["accounts"] == 1
        await store.close()

    asyncio.run(scenario())


def test_fetcher_persistent_profile_and_failure(tmp_path):
    async def scenario():
        store = AccountStore(tmp_path / "fetcher.db")
        await store.initialize()
        fetcher = AccountFetcher()
        await fetcher.initialize(cache_store=store)
        key = ("codeforces", "tourist", False, False, False)
        profile = _profile("tourist")
        profile.fetched_at = time.time()
        fetcher._cache[key] = (profile.fetched_at, profile)
        fetcher._profile_cache_dirty.add(key)
        fkey = ("codeforces", "ghost")
        fetcher._failure_cache[fkey] = (
            time.time() + 300,
            "未找到该 Codeforces 用户",
            False,
        )
        fetcher._failure_cache_dirty.add(fkey)

        await fetcher.flush_persistent_cache()
        assert not fetcher._profile_cache_dirty
        assert not fetcher._failure_cache_dirty

        # 新实例加载同一 SQLite：内存缓存恢复到另一进程/实例。
        fetcher2 = AccountFetcher()
        await fetcher2.initialize(cache_store=store)
        assert key in fetcher2._cache
        failure = fetcher2._failure_cache.get(fkey)
        assert failure is not None
        assert failure[2] is False  # permanent 失败重启后仍为 temporary=False

    asyncio.run(scenario())
