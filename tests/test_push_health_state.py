"""群推送健康状态读写与清理的单元测试（S2，台账 M2.1-M2.6）。

覆盖：读缓存摊薄 KV 读、权限类状态结构与退避递进、乱序写丢弃、
成功删除自愈、暂停窗口判定、blocked 清理边界（可达性与不造键）。
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import push_health
from src.models import GroupConfig

from test_main_accounts import _load_main_module

GROUP = GroupConfig(group_id="G1", platform_id="爱莉希雅", umo="爱莉希雅:GroupMessage:G1")
DENIED_ERR = {"kind": push_health.K_PERMISSION, "raw": "接口返回 主动消息失败, 无权限"}


class FakeKV:
    def __init__(self):
        self.store: dict = {}
        self.reads = 0
        self.deletes: list = []


def _bot(kv: FakeKV, groups=(GROUP,), recent=None):
    m = _load_main_module()
    bot = m.AcmerGroupBot.__new__(m.AcmerGroupBot)

    async def get_kv_data(key, default=None):
        kv.reads += 1
        return kv.store.get(key, default)

    async def put_kv_data(key, value):
        kv.store[key] = value

    async def delete_kv_data(key):
        kv.deletes.append(key)
        kv.store.pop(key, None)

    async def get_groups():
        return list(groups)

    bot.get_kv_data = get_kv_data
    bot.put_kv_data = put_kv_data
    bot.delete_kv_data = delete_kv_data
    bot.get_groups = get_groups
    bot.settlement = types.SimpleNamespace(_recent=recent or {})
    return bot


def test_health_read_is_cache_amortized():
    """同进程 30 秒内重复读只落一次 KV（防读放大）。"""
    kv = FakeKV()
    bot = _bot(kv)
    kv.store[push_health.health_key("爱莉希雅", "G1")] = {"count": 1, "suspended_until": time.time() + 60}
    first = asyncio.run(bot._health_of("G1", "爱莉希雅"))
    second = asyncio.run(bot._health_of("G1", "爱莉希雅"))
    assert first and second and first is not None
    assert kv.reads == 1, f"读了 {kv.reads} 次 KV，应只有 1 次"


def test_note_denied_structure_and_backoff():
    kv = FakeKV()
    bot = _bot(kv)
    t0 = 10_000_000.0
    asyncio.run(bot._note_push_denied(GROUP, "爱莉希雅", DENIED_ERR, now=t0))
    key = push_health.health_key("爱莉希雅", "G1")
    state = kv.store[key]
    assert state["count"] == 1
    assert state["state"] == "denied"
    assert state["suspended_until"] == t0 + 30 * 60          # 首次 30 分钟
    assert state["last_at"] == t0 and state["since"] == t0
    assert state["last_error_class"] == push_health.K_PERMISSION
    assert state["platform_id"] == "爱莉希雅"
    assert len(state["last_error"]) <= push_health.LAST_ERROR_MAX_CHARS
    fail = kv.store["pushfail_group_G1"]
    assert fail["n"] == 1 and fail["error_class"] == push_health.K_PERMISSION
    # 第二次：count=2 → 退避 360 分钟
    asyncio.run(bot._note_push_denied(GROUP, "爱莉希雅", DENIED_ERR, now=t0 + 1))
    state2 = kv.store[key]
    assert state2["count"] == 2
    assert state2["suspended_until"] == t0 + 1 + 360 * 60
    assert kv.store["pushfail_group_G1"]["n"] == 2


def test_note_denied_discards_stale_write():
    """已存在更晚写入时，本次（乱序/并发）写必须被丢弃。"""
    kv = FakeKV()
    bot = _bot(kv)
    key = push_health.health_key("爱莉希雅", "G1")
    kv.store[key] = {"count": 7, "last_at": 10_000_500.0}
    asyncio.run(bot._note_push_denied(GROUP, "爱莉希雅", DENIED_ERR, now=10_000_000.0))
    assert kv.store[key]["count"] == 7  # 未被旧写覆盖


def test_clear_health_deletes_key_and_refreshes_cache():
    kv = FakeKV()
    bot = _bot(kv)
    key = push_health.health_key("爱莉希雅", "G1")
    asyncio.run(bot._note_push_denied(GROUP, "爱莉希雅", DENIED_ERR))
    assert key in kv.store
    asyncio.run(bot._clear_push_health(GROUP))
    assert key in kv.deletes and key not in kv.store
    # 缓存同步刷新：立即再读必须是 None（而不是 30 秒内还读到旧状态）
    assert asyncio.run(bot._health_of("G1", "爱莉希雅")) is None


def test_push_suspended_window():
    """窗口判定四条路径：KV 冷读、缓存写入、删除即时生效、陈旧状态按时间戳判定。"""
    kv = FakeKV()
    bot = _bot(kv)
    key = push_health.health_key("爱莉希雅", "G1")
    # 1) 冷启动读 KV（进程刚重启，缓存为空）
    kv.store[key] = {"suspended_until": time.time() + 600}
    assert asyncio.run(bot.push_suspended(GROUP)) is True
    # 2) 自愈后走缓存写入 API → 立即生效（不必等 30 秒 TTL）
    bot._cache_health(key, None)
    assert asyncio.run(bot.push_suspended(GROUP)) is False
    # 3) 陈旧/过期状态：闸门比时间戳，过期即放行
    bot._cache_health(key, {"suspended_until": time.time() - 1})
    assert asyncio.run(bot.push_suspended(GROUP)) is False
    # 4) 再次进入窗口
    bot._cache_health(key, {"suspended_until": time.time() + 600})
    assert asyncio.run(bot.push_suspended(GROUP)) is True


def test_prune_removes_only_reachable_expired_blocked():
    """只删：在最近记录内、出窗超 36 小时、且确为 blocked 的键；不造键。"""
    now = time.time()
    recent = {
        ("luogu", "old"): {"platform": "luogu", "contest_id": "old", "end_time": now - 40 * 3600},
        ("luogu", "new"): {"platform": "luogu", "contest_id": "new", "end_time": now - 10 * 3600},
        ("luogu", "fresh"): {"platform": "luogu", "contest_id": "fresh", "end_time": now - 1 * 3600},
    }
    kv = FakeKV()
    kv.store["settle_G1_luogu_old"] = {"blocked": True, "reason": push_health.K_PERMISSION}
    kv.store["settle_G1_luogu_new"] = {"blocked": True, "reason": push_health.K_PERMISSION}
    kv.store["settle_G1_luogu_fresh"] = {"skipped": True, "members": ""}
    kv.store["settle_G1_luogu_ghost"] = {"skipped": True, "members": "x"}  # 不在最近记录里
    bot = _bot(kv, recent=recent)
    removed = asyncio.run(bot._prune_blocked_markers())
    assert removed == 1
    assert kv.deletes == ["settle_G1_luogu_old"]
    assert "settle_G1_luogu_new" in kv.store          # 未出窗保留
    assert "settle_G1_luogu_fresh" in kv.store        # 非 blocked 保留
    assert "settle_G1_luogu_ghost" in kv.store        # 不可达不动（也没造键）
    # 只在最近记录里的场次才被尝试：不存在的键不会被 create
    assert "settle_G1_luogu_missing" not in kv.store
