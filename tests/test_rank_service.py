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
                                          progress=False, record_metrics=True,
                                          full_detail=False):
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


def test_progress_read_persists_and_uses_snapshot(tmp_path):
    """本周进步榜与群排行共用快照机制：首次计算落库，第二次直接读快照。"""

    async def scenario():
        store = AccountStore(tmp_path / "progress.db")
        await store.initialize()
        plugin = FakePlugin(store)
        service = RankService(plugin)

        rows, errors = await service.read("g1", "codeforces", progress=True)
        assert rows and errors == []
        # 落库到 progress_snapshot（与 rank_snapshot 相互独立）
        assert len(await store.get_rank_rows("g1", "codeforces", mode="progress")) == 1
        assert await store.get_rank_rows("g1", "codeforces") == []

        # 首次（限量）计算后会投递一次后台完整刷新，等它结束
        for _ in range(100):
            if not service._jobs:
                break
            await asyncio.sleep(0.02)
        calls_after_build = plugin.compute_calls
        assert calls_after_build >= 1

        rows2, _ = await service.read("g1", "codeforces", progress=True)
        assert rows2
        assert plugin.compute_calls == calls_after_build  # 命中快照，不再全群重算

        # 标脏后 progress 也应失效（rank 的脏标记不影响 progress 快照表）
        await store.mark_rank_dirty("g1", "codeforces", mode="progress")
        rows3, _ = await service.read("g1", "codeforces", progress=True)
        assert rows3
        meta = None
        for _ in range(100):
            meta = await store.get_rank_meta(
                "g1", "codeforces", mode="progress"
            )
            if meta is not None and float(meta["dirty_at"]) == 0:
                break
            await asyncio.sleep(0.02)
        assert meta is not None
        assert float(meta["refreshed_at"]) >= float(meta["dirty_at"])

    asyncio.run(scenario())


def test_rank_and_progress_snapshots_are_isolated(tmp_path):
    """两种快照表互不覆盖：写 rank 不改变 progress，反之亦然。"""

    async def scenario():
        store = AccountStore(tmp_path / "isolation.db")
        await store.initialize()
        rank_rows = [dict(ROWS[0], display_value="1700", sort_value=1700)]
        progress_rows = [dict(ROWS[0], display_value="+42", sort_value=42)]
        await store.replace_rank_snapshot("g1", "codeforces", rank_rows)
        await store.replace_rank_snapshot(
            "g1", "codeforces", progress_rows, mode="progress"
        )
        ranks = await store.get_rank_rows("g1", "codeforces")
        progress = await store.get_rank_rows("g1", "codeforces", mode="progress")
        assert ranks[0]["display_value"] == "1700"
        assert progress[0]["display_value"] == "+42"

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


def test_read_with_meta_reports_fresh_and_snapshot_at(tmp_path):
    """BUG-032：read_with_meta 要能如实给出 fresh 与 snapshot_at（后台徽标据此显示）。"""

    async def scenario():
        store = AccountStore(tmp_path / "rank.db")
        await store.initialize()
        plugin = FakePlugin(store)
        service = RankService(plugin)
        service._closing = True          # 避免过期快照触发后台刷新任务

        rows, errors, meta = await service.read_with_meta("g1", "codeforces")
        assert rows and errors == []
        assert meta["fresh"] is True and meta["snapshot_at"] > 0

        fresh_meta = await store.get_rank_meta("g1", "codeforces")
        assert fresh_meta is not None

        # 标记为脏 -> 快照变陈旧，但依然返回旧数据并给出 fresh=False
        await store.mark_rank_dirty("g1", "codeforces")
        rows2, _errors2, meta2 = await service.read_with_meta("g1", "codeforces", allow_stale=True)
        assert rows2
        assert meta2["fresh"] is False
        assert meta2["snapshot_at"] == float((await store.get_rank_meta("g1", "codeforces")).get("refreshed_at") or 0.0)

    asyncio.run(scenario())
