"""RankService 物化快照与后台刷新集成测试。"""
from __future__ import annotations

import asyncio

from src.account_store import AccountStore
from src.rank_service import RankService

ROWS = [
    {
        "user_id": "u1",
        "handle": "alice",
        "display_name": "Alice",
        "value": 1700,
        "display_value": "1700",
        "metric_label": "Rating",
        "sort_value": 1700,
        "delta": 10,
    }
]


class FakeRegistry:
    def __init__(self, store):
        self.store = store
        self.store_enabled = True


class FakePlugin:
    def __init__(self, store):
        self.account_registry = FakeRegistry(store)
        self.compute_calls = 0

    async def _collect_rank_rows(self, group_id, platform, *, progress=False,
                                 record_metrics=True, allow_stale=True):
        self.compute_calls += 1
        return list(ROWS), []

    async def _collect_rank_rows_uncached(self, group_id, platform, *,
                                          progress=False, record_metrics=True):
        self.compute_calls += 1
        return list(ROWS), [], [("u1", platform, 1700)]


def test_rank_read_persists_and_uses_snapshot(tmp_path):
    async def scenario():
        store = AccountStore(tmp_path / "rank.db")
        await store.initialize()
        plugin = FakePlugin(store)
        service = RankService(plugin)

        # 首次：无快照 → 同步计算并落库
        rows, errors = await service.read("g1", "codeforces")
        assert rows and errors == []
        assert plugin.compute_calls == 1
        assert len(await store.get_rank_rows("g1", "codeforces")) == 1

        # 第二次：快照新鲜 → 不再全群计算
        rows2, _ = await service.read("g1", "codeforces")
        assert rows2 and plugin.compute_calls == 1

    asyncio.run(scenario())


def test_rank_meta_errors_shape():
    errors = RankService._meta_errors(
        {"errors_json": '[[\"u1\",\"未找到该 Codeforces 用户\"]]'}
    )
    assert errors == [("u1", "未找到该 Codeforces 用户")]
    assert RankService._meta_errors(None) == []


def test_rank_dirty_triggers_background_refresh(tmp_path):
    async def scenario():
        store = AccountStore(tmp_path / "rank2.db")
        await store.initialize()
        plugin = FakePlugin(store)
        service = RankService(plugin)
        await service.read("g1", "codeforces")
        assert plugin.compute_calls == 1

        await store.mark_rank_dirty("g1", "codeforces")
        # stale-while-revalidate：立即返回旧快照并投递刷新
        rows, _ = await service.read("g1", "codeforces")
        assert rows
        # 后台任务已投递；等待其完成
        for _ in range(50):
            if plugin.compute_calls >= 2:
                break
            await asyncio.sleep(0.02)
        assert plugin.compute_calls >= 2
        # 刷新后 meta 应为非 stale（dirty_at 被清零）
        meta = await store.get_rank_meta("g1", "codeforces")
        assert meta is not None
        assert float(meta["refreshed_at"]) >= float(meta["dirty_at"])

    asyncio.run(scenario())
