"""每日早报附带周榜：早报正文保持原样 + 追加两张榜单图片。"""
from __future__ import annotations

import asyncio

from src.models import GroupConfig
from src.scheduler import PushScheduler
from test_main_accounts import _load_main_module

bot_main = _load_main_module()


def _row(name, delta, current):
    return {
        "user_id": f"u_{name}",
        "display_name": name,
        "handle": name.lower(),
        "delta": delta,
        "value": delta,
        "display_value": f"{delta:+d}",
        "current_display_value": str(current),
    }


class _FakeRankService:
    def __init__(self, sections):
        self.sections = sections
        self.calls = []

    async def read(self, group_id, platform, **kwargs):
        self.calls.append((platform, kwargs.get("progress")))
        return list(self.sections.get(platform, [])), []


def _make_bot(sections):
    bot = bot_main.AcmerGroupBot.__new__(bot_main.AcmerGroupBot)
    bot.rank_service = _FakeRankService(sections)

    async def fake_settings():
        return {"push_platforms": list(sections.keys()) or ["codeforces"]}

    bot.get_settings = fake_settings
    return bot


def _group():
    return GroupConfig(
        group_id="g1",
        push_platforms=["codeforces", "atcoder"],
        morning_push_time="08:00",
    )


def test_weekly_board_cards_build_progress_and_regress():
    sections = {
        "codeforces": [
            _row("涨王", 101, 2482),
            _row("小涨", 42, 1900),
            _row("跌王", -59, 2381),
            _row("小跌", -12, 1500),
            _row("持平", 0, 1600),
        ]
    }
    bot = _make_bot(sections)
    # 卡片渲染在测试环境不可用 → image 为 None，用 text 兜底验证内容
    async def no_render(*args, **kwargs):
        return None

    bot._render_overview_card = no_render
    boards = asyncio.run(bot.build_weekly_board_cards(_group()))
    titles = [b["title"] for b in boards]
    # 顺序：先进步榜、后退步榜
    assert titles == ["本群本周进步榜", "本群本周退步榜"]
    progress_text = boards[0]["text"]
    regress_text = boards[1]["text"]
    assert "涨王" in progress_text and "+101" in progress_text
    # 退步榜只含下降成员（不含上涨的“涨王”）
    assert "跌王" in regress_text and "涨王" not in regress_text
    assert "-59" in regress_text
    assert all(p is True for _p, p in bot.rank_service.calls)


def test_weekly_board_cards_empty_when_no_rows():
    bot = _make_bot({"codeforces": []})

    async def no_render(*args, **kwargs):
        return None

    bot._render_overview_card = no_render
    assert asyncio.run(bot.build_weekly_board_cards(_group())) == []


def test_weekly_board_cards_survives_platform_error():
    class _Boom(_FakeRankService):
        async def read(self, group_id, platform, **kwargs):
            if platform == "atcoder":
                raise RuntimeError("boom")
            return list(self.sections.get(platform, [])), []

    bot = bot_main.AcmerGroupBot.__new__(bot_main.AcmerGroupBot)
    bot.rank_service = _Boom({"codeforces": [_row("涨王", 30, 1800)]})

    async def fake_settings():
        return {"push_platforms": ["codeforces", "atcoder"]}

    bot.get_settings = fake_settings

    async def no_render(*args, **kwargs):
        return None

    bot._render_overview_card = no_render
    boards = asyncio.run(bot.build_weekly_board_cards(_group()))
    assert "涨王" in boards[0]["text"]  # 一个平台失败不影响另一个


# ------------------------------------------------------------------
# 调度：早报文字保持原样 + 之后追加两张图；无比赛仍推图
# ------------------------------------------------------------------
class _FakePlugin:
    def __init__(self, morning_text, boards_ok=True, has_boards=True):
        self._morning = morning_text
        self._boards_ok = boards_ok
        self._has_boards = has_boards
        self.sent: list[str] = []
        self.board_pushes = 0
        self.kv: dict = {}
        self.attempt_ok = True
        self.attempt_checks = 0
        self.attempt_notes = 0

    async def build_morning_text(self, group):
        return self._morning

    async def send_notification(self, group, text):
        self.sent.append(text)
        return True

    async def push_weekly_boards(self, group):
        self.board_pushes += 1
        return self._boards_ok and self._has_boards or self._boards_ok

    async def get_kv_data(self, key, default=None):
        return self.kv.get(key, default)

    async def put_kv_data(self, key, value):
        self.kv[key] = value

    async def push_attempt_allowed(self, kind, key):
        self.attempt_checks += 1
        return self.attempt_ok

    async def note_push_attempt(self, kind, key):
        self.attempt_notes += 1
        return self.attempt_notes


def _tick(plugin):
    scheduler = PushScheduler(plugin)
    from datetime import datetime

    from src.models import CN_TZ

    now = datetime(2026, 9, 13, 8, 0, tzinfo=CN_TZ)
    asyncio.run(scheduler._maybe_morning_push(_group(), now))
    return plugin


def test_morning_push_sends_text_then_boards():
    plugin = _FakePlugin("🌅 今日比赛早报")
    _tick(plugin)
    # 早报正文保持原样（不含榜单文字）
    assert plugin.sent == ["🌅 今日比赛早报"]
    # 榜单以图片形式单独推送
    assert plugin.board_pushes == 1


def test_morning_push_sends_boards_without_contest():
    """核心需求：当天没有比赛时不发早报正文，但仍推送两张榜单图。"""
    plugin = _FakePlugin(None)
    _tick(plugin)
    assert plugin.sent == []
    assert plugin.board_pushes == 1
    assert plugin.kv.get("morning_g1_20260913") is True


def test_morning_push_marks_done_when_boards_empty():
    plugin = _FakePlugin(None, boards_ok=True)
    _tick(plugin)
    assert plugin.kv.get("morning_g1_20260913") is True


def test_morning_push_retries_when_boards_fail_without_text():
    plugin = _FakePlugin(None, boards_ok=False)
    _tick(plugin)
    # 没有正文时，榜单失败不标记完成 → 下个周期重试
    assert plugin.kv.get("morning_g1_20260913") is not True


def test_morning_push_keeps_text_when_boards_fail():
    plugin = _FakePlugin("🌅 今日比赛早报", boards_ok=False)
    _tick(plugin)
    assert plugin.sent == ["🌅 今日比赛早报"]
    # 正文已送达即标记完成，避免重复早报
    assert plugin.kv.get("morning_g1_20260913") is True


def test_morning_push_late_still_fires():
    """BUG-026：早报时间已过（补发窗口）也要推，当天仍只会成功一次。"""
    from datetime import datetime

    from src.models import CN_TZ

    plugin = _FakePlugin("🌅 今日比赛早报")
    scheduler = PushScheduler(plugin)

    now = datetime(2026, 9, 13, 9, 30, tzinfo=CN_TZ)   # 群里设的是 08:00
    asyncio.run(scheduler._maybe_morning_push(_group(), now))
    assert plugin.sent == ["🌅 今日比赛早报"]
    assert plugin.kv.get("morning_g1_20260913") is True
    assert plugin.attempt_notes == 1


def test_morning_push_respects_attempt_gate():
    """BUG-026 护栏：尝试次数用尽后不再触发（避免每 30 秒重试）。"""
    from datetime import datetime

    from src.models import CN_TZ

    plugin = _FakePlugin("🌅 今日比赛早报")
    plugin.attempt_ok = False
    scheduler = PushScheduler(plugin)

    now = datetime(2026, 9, 13, 9, 30, tzinfo=CN_TZ)
    asyncio.run(scheduler._maybe_morning_push(_group(), now))
    assert plugin.sent == []
    assert plugin.attempt_checks == 1
    assert plugin.attempt_notes == 0


def test_morning_push_is_silent_when_already_sent(caplog):
    """BUG-045：补发窗口内「已发过」的群不能再每 tick 打日志。"""
    import logging
    from datetime import datetime

    from src.models import CN_TZ

    plugin = _FakePlugin("今日比赛早报")
    plugin.kv["morning_g1_20260913"] = True
    scheduler = PushScheduler(plugin)
    now = datetime(2026, 9, 13, 9, 30, tzinfo=CN_TZ)
    with caplog.at_level(logging.INFO):
        asyncio.run(scheduler._maybe_morning_push(_group(), now))
    assert plugin.sent == []
    assert not any("早报时间到" in r.message for r in caplog.records)
