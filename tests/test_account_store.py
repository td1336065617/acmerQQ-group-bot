"""AccountStore（SQLite 存储层）单元测试。"""
from __future__ import annotations

import asyncio
import time

import pytest

from src.account_store import AccountStore


@pytest.fixture
def store(tmp_path):
    instance = AccountStore(tmp_path / "acmer_store.db")
    asyncio.run(instance.initialize())
    return instance


def _account(handle: str, uid: str | None = None) -> dict:
    uid = uid or handle
    return {
        "handle": handle,
        "platform_user_id": uid,
        "display_name": handle,
        "profile_url": f"https://codeforces.com/profile/{handle}",
        "verified_at": time.time(),
    }


def test_initialize_and_version(store):
    assert asyncio.run(store.schema_version()) == 1
    stats = asyncio.run(store.stats())
    assert stats["accounts"] == 0


def test_save_binding_conflict(store):
    async def scenario():
        base = _account("tourist")
        await store.save_binding("user-1", "codeforces", base)
        with pytest.raises(ValueError):
            await store.save_binding("user-2", "codeforces", base)
        # 同一用户重复保存（覆盖）是允许的
        await store.save_binding("user-1", "codeforces", _account("tourist2"))
        accounts = await store.get_user_accounts("user-1")
        assert accounts["codeforces"]["handle"] == "tourist2"

    asyncio.run(scenario())


def test_group_members_and_pending(store):
    async def scenario():
        assert await store.set_group_member("g1", "u1", True) is True
        # 幂等：已启用再次启用不视为变更
        assert await store.set_group_member("g1", "u1", True) is False
        assert await store.set_group_member("g1", "u1", False) is True
        assert await store.set_group_member("g1", "u1", False) is False
        assert await store.set_group_member("g1", "u2", True) is True
        assert await store.get_group_member_ids("g1") == ["u2"]
        # preserve_opt_out：退出状态不可被“重新加入”覆盖
        assert (
            await store.set_group_member("g1", "u1", True, preserve_opt_out=True)
            is False
        )

        await store.create_pending(
            "u1",
            "codeforces",
            token_hash="hash-abc",
            group_id="g1",
            expires_at=time.time() + 60,
        )
        pending = await store.get_pending("u1", "codeforces")
        assert pending is not None and pending["token_hash"] == "hash-abc"
        await store.clear_pending("u1", "codeforces")
        assert await store.get_pending("u1", "codeforces") is None
        assert await store.purge_expired_pending() == 0

    asyncio.run(scenario())


def _rating_legacy(user_id: str, platform: str, entries: list[tuple[float, int]]):
    return {
        "account_rating_snapshots": {
            user_id: {
                platform: [
                    {"timestamp": ts, "rating": rating}
                    for ts, rating in entries
                ]
            }
        }
    }


def test_weekly_delta_via_migrated_history(store):
    async def scenario():
        ref = time.time()
        legacy = _rating_legacy(
            "u1", "codeforces", [(ref - 8 * 86400, 1500), (ref, 1600)]
        )
        await store.migrate_from_kv(legacy)
        delta = await store.get_weekly_deltas(
            [("u1", "codeforces")], now=ref
        )
        assert delta[("u1", "codeforces")] == 100

    asyncio.run(scenario())


def test_record_ratings_dedupe_same_value(store):
    async def scenario():
        ref = time.time()
        legacy = _rating_legacy("u1", "codeforces", [(ref, 1600)])
        await store.migrate_from_kv(legacy)
        stats = await store.stats()
        assert stats["rating_history"] == 1
        # 同值且 15 分钟内：不新增
        await store.record_ratings([("u1", "codeforces", 1600)])
        stats = await store.stats()
        assert stats["rating_history"] == 1

    asyncio.run(scenario())


def test_record_ratings_same_second_different_value(store):
    async def scenario():
        # 同一秒内两次值变化的快照都应保留（旧 KV 允许列表内同秒多记录）
        await store.record_ratings([("u1", "codeforces", 1500)])
        await store.record_ratings([("u1", "codeforces", 1600)])
        stats = await store.stats()
        assert stats["rating_history"] == 2

    asyncio.run(scenario())


def test_rating_history_cap_90(store):
    async def scenario():
        ref = time.time()
        entries = [
            (ref - (95 - index) * 0.001, 1000 + index) for index in range(95)
        ]
        legacy = _rating_legacy("u1", "codeforces", entries)
        await store.migrate_from_kv(legacy)
        stats = await store.stats()
        # 迁移后立即遵守“每用户每平台最多 90 条”。
        assert stats["rating_history"] == 90
        # 任意一次 record_ratings 仍保持 90 条上限。
        await store.record_ratings([("u1", "codeforces", 9999)])
        stats = await store.stats()
        assert stats["rating_history"] == 90

    asyncio.run(scenario())


def test_migrate_from_kv_counts_and_data(store):
    async def scenario():
        now = time.time()
        legacy = {
            "linked_accounts": {
                "u1": {
                    "codeforces": _account("tourist"),
                    "luogu": _account("luogu_177", "1770958"),
                }
            },
            "group_rank_members": {
                "g1": {"u1": {"enabled": True, "updated_at": now}}
            },
            "pending_account_bindings": {
                "u1:codeforces": {
                    "token_hash": "abc",
                    "created_at": now,
                    "expires_at": now + 60,
                }
            },
            "account_rating_snapshots": {
                "u1": {
                    "codeforces": [
                        {"timestamp": now - 100, "rating": 1500},
                        {"timestamp": now, "rating": 1600},
                    ]
                }
            },
        }
        counts = await store.migrate_from_kv(legacy)
        assert counts["accounts"] == 2
        assert counts["members"] == 1
        assert counts["pending"] == 1
        assert counts["history"] == 2

        accounts = await store.get_user_accounts("u1")
        assert accounts["codeforces"]["platform_user_id"] == "tourist"
        assert accounts["luogu"]["platform_user_id"] == "1770958"
        member_ids = await store.get_group_member_ids("g1")
        assert member_ids == ["u1"]

    asyncio.run(scenario())


def test_group_platform_accounts_join(store):
    async def scenario():
        await store.save_binding("u1", "codeforces", _account("alice"))
        await store.save_binding("u2", "codeforces", _account("bob"))
        await store.save_binding("u2", "luogu", _account("bob_luogu", "42"))
        await store.set_group_member("g1", "u1", True)
        await store.set_group_member("g1", "u2", True)
        rows = await store.get_group_platform_accounts("g1", "codeforces")
        assert {row["user_id"] for row in rows} == {"u1", "u2"}
        assert all(row["platform"] == "codeforces" for row in rows)
        luogu_rows = await store.get_group_platform_accounts("g1", "luogu")
        assert {row["user_id"] for row in luogu_rows} == {"u2"}

    asyncio.run(scenario())


def test_profile_cache_roundtrip(store):
    async def scenario():
        now = time.time()
        await store.upsert_profile_cache(
            [
                {
                    "platform": "codeforces",
                    "handle_norm": "tourist",
                    "kind": "basic",
                    "payload": '{"platform":"codeforces","handle":"tourist"}',
                    "fetched_at": now,
                    "expires_at": now + 600,
                }
            ]
        )
        rows = await store.load_profile_cache()
        assert len(rows) == 1
        assert rows[0]["kind"] == "basic"
        assert rows[0]["handle_norm"] == "tourist"

    asyncio.run(scenario())


def test_fetch_failures_roundtrip(store):
    async def scenario():
        now = time.time()
        await store.upsert_fetch_failures(
            [
                {
                    "platform": "codeforces",
                    "handle_norm": "ghost",
                    "kind": "",
                    "reason": "未找到该 Codeforces 用户",
                    "temporary": False,
                    "expires_at": now + 300,
                }
            ]
        )
        rows = await store.load_fetch_failures()
        assert len(rows) == 1
        assert rows[0]["reason"] == "未找到该 Codeforces 用户"
        assert rows[0]["temporary"] == 0

    asyncio.run(scenario())


def test_save_binding_atomic_conflict(store):
    async def scenario():
        base = _account("tourist")
        await store.save_binding_atomic("u1", "codeforces", base)
        with pytest.raises(ValueError):
            await store.save_binding_atomic("u2", "codeforces", base)
        # 同用户覆盖更新是允许的
        await store.save_binding_atomic("u1", "codeforces", _account("tourist2"))
        accounts = await store.get_user_accounts("u1")
        assert accounts["codeforces"]["handle"] == "tourist2"

    asyncio.run(scenario())


def test_migrate_rejects_repeat(store):
    async def scenario():
        await store.migrate_from_kv({})
        with pytest.raises(RuntimeError):
            await store.migrate_from_kv({})

    asyncio.run(scenario())


def test_set_display_name_only_when_changed(store):
    async def scenario():
        await store.save_binding("u1", "codeforces", _account("alice"))
        assert await store.set_user_display_name("u1", "新昵称") is True
        assert await store.set_user_display_name("u1", "新昵称") is False

    asyncio.run(scenario())


def test_rank_snapshot_roundtrip(store):
    async def scenario():
        rows = [
            {
                "user_id": "u2",
                "handle": "bob",
                "display_name": "Bob",
                "display_value": "1800",
                "metric_label": "Rating",
                "sort_value": 1800,
                "delta": 10,
            },
            {
                "user_id": "u1",
                "handle": "alice",
                "display_name": "Alice",
                "display_value": "1700",
                "metric_label": "Rating",
                "sort_value": 1700,
                "delta": -5,
            },
        ]
        await store.replace_rank_snapshot("g1", "codeforces", rows)
        got = await store.get_rank_rows("g1", "codeforces")
        assert [r["user_id"] for r in got] == ["u2", "u1"]
        # 替换为空可清空
        await store.replace_rank_snapshot("g1", "codeforces", [])
        assert await store.get_rank_rows("g1", "codeforces") == []

    asyncio.run(scenario())


def test_rank_meta_dirty_and_stale(store):
    async def scenario():
        await store.mark_rank_dirty("g1", "codeforces")
        await store.touch_rank_meta("g1", "codeforces")
        # g2 只有脏标记、从未成功刷新 → 应被视为 stale
        await store.mark_rank_dirty("g2", "codeforces")
        stale = await store.list_stale_rank_meta(
            max_age=3600, active_platforms=["codeforces", "luogu"]
        )
        assert any(
            item["group_id"] == "g2" and item["platform"] == "codeforces"
            for item in stale
        )

    asyncio.run(scenario())
