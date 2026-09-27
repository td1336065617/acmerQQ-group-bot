"""可排查性（observability）回归：降级路径必须留痕。

背景（2026-09-27）：两次事故的共同点是"失败了却不打日志"——渲染静默改文字、
会话预热失败只报数量、预览读缓存异常只出现在消息里。本文件锁定这三处必须有日志。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from platform_compat import warm_scene

from test_main_accounts import _load_main_module
from test_session_warmup import FakeInst
from test_settlement_tick import GROUP, _build_bot


class _BrokenInst(FakeInst):
    def remember_session_scene(self, session_id: str, scene: str) -> None:
        raise RuntimeError("模拟平台补写失败")


def test_warm_scene_failure_is_logged(caplog):
    """A1：预热补写会话失败必须记下群号与异常。"""
    inst = _BrokenInst("爱莉希雅", "qq_official")
    ctx = type("Ctx", (), {"platform_manager": type("PM", (), {"platform_insts": [inst]})()})()
    with caplog.at_level(logging.WARNING):
        assert warm_scene(ctx, "G1", "爱莉希雅") is False
    msgs = [r.getMessage() for r in caplog.records]
    assert any("恢复群 G1 的会话场景失败" in m and "RuntimeError" in m for m in msgs), msgs


def test_preview_cache_error_is_logged(caplog):
    """A3：预览读缓存异常既要提示使用者，也要进日志。"""
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

    def boom(platform, *, max_age=None):
        raise OSError("模拟缓存读取失败")

    bot.fetcher.cached_platform = boom
    with caplog.at_level(logging.WARNING):
        rows = bot._preview_contests(["codeforces"])
    assert rows and rows[0][2] and "读取缓存失败" in rows[0][2]
    assert any("测试推送预览读取 codeforces 缓存失败" in r.getMessage() for r in caplog.records)


def test_renderer_failures_are_logged(caplog, tmp_path, monkeypatch):
    """A2：渲染器逐个失败时，日志里要能看到是哪个渲染器、因为什么。"""
    from src.account_cards import AccountCardRenderer
    from src.output_renderer import AdaptiveOutputRenderer

    monkeypatch.setattr(
        AdaptiveOutputRenderer,
        "_find_renderers",
        staticmethod(lambda: [("chromium", "/nonexistent-browser")]),
    )
    monkeypatch.setattr(
        AdaptiveOutputRenderer,
        "_run_external_renderer",
        staticmethod(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("模拟浏览器失败"))),
    )

    def bad_fallback(*args, **kwargs):
        raise RuntimeError("模拟 Pillow 失败")

    renderer = AccountCardRenderer(cache_dir=tmp_path)
    renderer._render("<html></html>", {"kind": "settlement", "sections": {"codeforces": []}},
                     bad_fallback, [], "t", "s", "n", "打星")
    with caplog.at_level(logging.WARNING):
        renderer._render("<html></html>", {"kind": "settlement", "sections": {"codeforces": []}},
                         bad_fallback, [], "t", "s", "n", "打星")
    msgs = [r.getMessage() for r in caplog.records]
    assert any("卡片渲染不可用" in m and "chromium" in m and "模拟 Pillow 失败" in m for m in msgs), msgs
