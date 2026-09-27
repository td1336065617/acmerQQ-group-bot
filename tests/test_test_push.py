"""测试推送（WebUI 立即试跑）回归测试。

背景（2026-09-27 故障）：
1) build_test_text 调用了 1.11.0 就被移除的 build_weekly_boards_text（属 CASE-01 同类：
   调用 self 上不存在的方法），只是被 try/except 吞成一条 warning；
2) 预览会顺序抓 4 个平台，网络慢（CF 网关 504）时后台请求被挂住 → 表现为「点了没反应」。
本文件锁定：不再调用不存在的方法（源码级守卫）+ 预览必须有硬超时。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_main_accounts import _load_main_module
from test_settlement_tick import GROUP, _build_bot

ROOT = Path(__file__).resolve().parent.parent


def test_no_call_to_removed_weekly_boards_method():
    """源码级守卫：main.py 不得再出现 build_weekly_boards_text（该方法已不存在）。"""
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "build_weekly_boards_text" not in source


def test_build_test_text_returns_quickly_when_fetcher_hangs(monkeypatch):
    """抓取卡住时，预览必须在超时内返回，不能把后台请求挂死。"""
    m = _load_main_module()
    monkeypatch.setattr(m, "TEST_PUSH_TOTAL_TIMEOUT", 0.2)
    monkeypatch.setattr(m, "TEST_PUSH_PLATFORM_TIMEOUT", 0.2)
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
        await asyncio.sleep(5)
        return [], None

    async def no_morning(group):
        return None

    bot.fetcher.fetch_platform = hang
    bot.build_morning_text = no_morning

    started = time.time()
    text = asyncio.run(bot.build_test_text(GROUP))
    elapsed = time.time() - started
    assert elapsed < 2.0, f"预览耗时 {elapsed:.1f}s，超时保护失效"
    assert "测试推送" in text
    assert "刷新" in text  # 提示稍后重试，而不是空白/报错


def test_build_test_text_uses_fetched_contest(monkeypatch):
    """正常路径：抓得到比赛时应展示比赛详情。"""
    m = _load_main_module()
    bot = _build_bot(
        m,
        groups=[GROUP],
        contests={},
        members={GROUP.group_id: ["u1"]},
        accounts={},
        settlement=None,
        settings={"push_platforms": ["codeforces"]},
    )

    class _Contest:
        name = "Codeforces Round 9999"

        def is_upcoming(self):
            return True

        @property
        def start_time(self):
            return __import__("datetime").datetime(2030, 1, 1, tzinfo=__import__("datetime").timezone.utc)

        def format_detail(self):
            return "CF9999 详情"

    async def one_contest(platform, force=False):
        return [_Contest()], None

    async def no_morning(group):
        return None

    bot.fetcher.fetch_platform = one_contest
    bot.build_morning_text = no_morning
    text = asyncio.run(bot.build_test_text(GROUP))
    assert "CF9999 详情" in text
