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


def _broken_ctx(name="爱莉希雅"):
    inst = _BrokenInst(name, "qq_official")
    return type("Ctx", (), {"platform_manager": type("PM", (), {"platform_insts": [inst]})()})()


def test_warm_scene_failure_is_logged(caplog):
    """A1：预热补写会话失败必须记下群号与异常。"""
    with caplog.at_level(logging.WARNING):
        assert warm_scene(_broken_ctx(), "G-warm-1", "爱莉希雅") is False
    msgs = [r.getMessage() for r in caplog.records]
    assert any("恢复群 G-warm-1 的会话场景失败" in m and "RuntimeError" in m for m in msgs), msgs


def test_warm_scene_failure_is_deduped(caplog):
    """同一个群+平台只告警一次，避免预热重试刷屏。"""
    ctx = _broken_ctx()
    with caplog.at_level(logging.DEBUG):
        assert warm_scene(ctx, "G-warm-2", "爱莉希雅") is False
        assert warm_scene(ctx, "G-warm-2", "爱莉希雅") is False
    warns = [r for r in caplog.records if r.levelno == logging.WARNING and "恢复群 G-warm-2" in r.getMessage()]
    assert len(warns) == 1, [r.getMessage() for r in caplog.records]


def test_warm_scene_alert_resets_after_success(caplog):
    """恢复成功后再次失败要能重新告警（否则去重会变成永久静音）。"""

    def ctx_with(inst):
        return type("Ctx", (), {"platform_manager": type("PM", (), {"platform_insts": [inst]})()})()

    broken = _BrokenInst("爱莉希雅", "qq_official")
    healthy = FakeInst("爱莉希雅", "qq_official")
    with caplog.at_level(logging.DEBUG):
        assert warm_scene(ctx_with(broken), "G-warm-3", "爱莉希雅") is False
        assert warm_scene(ctx_with(healthy), "G-warm-3", "爱莉希雅") is True
        assert warm_scene(ctx_with(broken), "G-warm-3", "爱莉希雅") is False
    warns = [r for r in caplog.records if r.levelno == logging.WARNING and "恢复群 G-warm-3" in r.getMessage()]
    assert len(warns) == 2, [r.getMessage() for r in caplog.records]


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

# ---------------------------------------------------------------------------
# 周报/周榜卡片降级为文字的发送侧留痕（W1/W2，实现文档：周报卡片降级留痕）
# ---------------------------------------------------------------------------


def _weekly_bot(m, report, boards=None):
    """最小真机：跑真实 _push_weekly_report_for_group / push_weekly_boards，
    只替换数据构建与发送（发送成功、记录到 sent/images）。"""
    bot = m.AcmerGroupBot.__new__(m.AcmerGroupBot)
    kv: dict = {}
    sent: list = []
    images: list = []

    async def get_kv_data(key, default=None):
        return kv.get(key, default)

    async def put_kv_data(key, value):
        kv[key] = value

    async def delete_kv_data(key):
        kv.pop(key, None)

    async def get_settings():
        return {}

    async def send_notification(group, text, **kwargs):
        sent.append(text)
        return True

    async def send_group_image(group, image_path, **kwargs):
        images.append(str(image_path))
        return True

    async def build_weekly_report(group):
        return report

    async def build_weekly_board_cards(group):
        return boards if boards is not None else report.get("cards") or []

    async def log_push(*args, **kwargs):
        return None

    bot.get_kv_data = get_kv_data
    bot.put_kv_data = put_kv_data
    bot.delete_kv_data = delete_kv_data
    bot.get_settings = get_settings
    bot.send_notification = send_notification
    bot._send_group_image = send_group_image
    bot.build_weekly_report = build_weekly_report
    bot.build_weekly_board_cards = build_weekly_board_cards
    bot._log_push = log_push
    return bot, kv, sent, images


def test_weekly_card_without_image_is_logged(caplog):
    """W1：周报卡片降级为文字必须留痕（群、卡名、原因、改发说明）。"""
    from datetime import datetime, timezone

    m = _load_main_module()
    report = {
        "text": "统计正文",
        "cards": [{"title": "本周进步榜", "image": None, "text": "卡片兜底文字"}],
    }
    bot, kv, sent, images = _weekly_bot(m, report)
    with caplog.at_level(logging.WARNING, logger="astrbot"):
        ok = asyncio.run(
            bot._push_weekly_report_for_group(
                GROUP, datetime.now(timezone.utc), week_key="2026-W40"
            )
        )
    assert ok is True
    warns = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(
        GROUP.group_id in w and "本周进步榜" in w and "渲染未产出图片" in w for w in warns
    ), warns
    assert any("改发卡片文字" in w for w in warns), warns
    assert "卡片兜底文字" in sent  # 确实降级成了文字
    assert images == []  # 没走图片路径


def test_weekly_card_with_image_has_no_warning(tmp_path, caplog):
    """有图且文件存在：走图片路径、零告警（锁不误报）。"""
    from datetime import datetime, timezone

    m = _load_main_module()
    image_file = tmp_path / "weekly.png"
    image_file.write_bytes(b"png")
    report = {
        "text": "统计正文",
        "cards": [{"title": "本周进步榜", "image": str(image_file), "text": "兜底"}],
    }
    bot, kv, sent, images = _weekly_bot(m, report)
    with caplog.at_level(logging.WARNING, logger="astrbot"):
        ok = asyncio.run(
            bot._push_weekly_report_for_group(
                GROUP, datetime.now(timezone.utc), week_key="2026-W41"
            )
        )
    assert ok is True
    assert images == [str(image_file)]
    assert "兜底" not in sent  # 没降级
    assert not any("未取到图片" in r.getMessage() for r in caplog.records)


def test_weekly_card_missing_file_logs_path(tmp_path, caplog):
    """image 有值但文件不存在（如缓存被清理）：告警须含「文件缺失」与路径。"""
    from datetime import datetime, timezone

    m = _load_main_module()
    missing = str(tmp_path / "evicted.png")
    report = {
        "text": "t",
        "cards": [{"title": "本周退步榜", "image": missing, "text": "兜底文字"}],
    }
    bot, kv, sent, images = _weekly_bot(m, report)
    with caplog.at_level(logging.WARNING, logger="astrbot"):
        ok = asyncio.run(
            bot._push_weekly_report_for_group(
                GROUP, datetime.now(timezone.utc), week_key="2026-W42"
            )
        )
    assert ok is True
    warns = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("图片文件缺失" in w and "evicted.png" in w for w in warns), warns
    assert "兜底文字" in sent


def test_boards_card_without_image_is_logged(caplog):
    """W2：早报周榜卡片降级同样必须留痕。"""
    m = _load_main_module()
    boards = [{"title": "本群本周进步榜", "image": None, "text": "榜单文字"}]
    bot, kv, sent, images = _weekly_bot(m, {"text": "", "cards": boards}, boards=boards)
    with caplog.at_level(logging.WARNING, logger="astrbot"):
        assert asyncio.run(bot.push_weekly_boards(GROUP)) is True
    warns = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(
        GROUP.group_id in w and "本群本周进步榜" in w and "改发卡片文字" in w for w in warns
    ), warns
    assert "榜单文字" in sent
