"""测试推送（WebUI 立即试跑）回归测试。

背景（2026-09-27 故障）：
1) build_test_text 调用了 1.11.0 就被移除的 build_weekly_boards_text（属 CASE-01 同类：
   调用 self 上不存在的方法），只是被 try/except 吞成一条 warning；
2) 预览会顺序抓 4 个平台，缓存过期后 fetch_platform 会在请求路径里同步抓取，
   网络慢（CF 网关 504）时后台请求被挂住 → 表现为「点了没反应」。

修正后的设计（1.20.12）：**预览只读缓存（允许 stale）、绝不触网、不加超时**，
刷新交给每 tick 的后台预热。真实早报仍走 fetch_platform（允许耐心刷新）。
本文件锁定这三条。
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_main_accounts import _load_main_module
from test_settlement_tick import GROUP, _build_bot

ROOT = Path(__file__).resolve().parent.parent


class _Contest:
    name = "Codeforces Round 9999"
    end_time = None  # build_morning_text 会读它（None 表示时长未知）

    def is_upcoming(self):
        return True

    def start_cn(self):
        return datetime(2030, 1, 1, 8, 0, tzinfo=timezone.utc)

    @property
    def start_time(self):
        return datetime(2030, 1, 1, tzinfo=timezone.utc)

    def format_detail(self):
        return "CF9999 详情"


def _bot(monkeypatch):
    m = _load_main_module()
    bot = _build_bot(
        m,
        groups=[GROUP],
        contests={},
        members={GROUP.group_id: ["u1"]},
        accounts={},
        settlement=None,
        settings={"push_platforms": ["codeforces", "atcoder", "nowcoder", "luogu"]},
    )

    async def hang(platform, force=False):
        raise AssertionError("预览路径不允许触网（fetch_platform）")

    bot.fetcher.fetch_platform = hang  # 一旦预览触网，用例立刻失败
    return m, bot


def test_no_call_to_removed_weekly_boards_method():
    """源码级守卫：main.py 不得再出现 build_weekly_boards_text（该方法已不存在）。"""
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "build_weekly_boards_text" not in source


def test_test_push_preview_never_hits_network(monkeypatch):
    """无缓存时：预览必须立刻返回提示，且完全不碰网络。"""
    m, bot = _bot(monkeypatch)
    started = time.time()
    text = asyncio.run(bot.build_test_text(GROUP))
    elapsed = time.time() - started
    assert elapsed < 1.0, f"预览耗时 {elapsed:.1f}s，说明仍在触网"
    assert "测试推送" in text
    assert "刷新" in text


def test_test_push_preview_uses_stale_cache(monkeypatch):
    """缓存已过期也能用：预览读 stale 数据，不触发刷新。"""
    m, bot = _bot(monkeypatch)
    bot.fetcher._cache["codeforces"] = (time.time() - 99999, [_Contest()])
    text = asyncio.run(bot.build_test_text(GROUP))
    assert "CF9999 详情" in text


def test_morning_text_without_flag_still_uses_network(monkeypatch):
    """真实早报（不带 cached_only）必须仍走 fetch_platform，不能被改成只读缓存。"""
    m, bot = _bot(monkeypatch)
    calls = {"n": 0}

    async def counted(platform, force=False):
        calls["n"] += 1
        return [], None

    bot.fetcher.fetch_platform = counted
    asyncio.run(bot.build_morning_text(GROUP))
    assert calls["n"] > 0
