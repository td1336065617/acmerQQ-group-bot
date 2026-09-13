"""每日早报附带的周榜：文本构建与“无比赛也推送”的调度行为。"""
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


def test_weekly_boards_text_lists_progress_and_regress():
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
    text = asyncio.run(bot.build_weekly_boards_text(_group()))
    assert "📈 本周进步榜" in text
    assert "📉 本周退步榜" in text
    # 进步榜按涨幅降序、退步榜按跌幅降序
    assert text.index("涨王") < text.index("小涨")
    assert text.index("跌王") < text.index("小跌")
    # 持平与无变化成员不出现
    assert "持平" not in text
    # 带当前 Rating 与符号
    assert "+101" in text and "（当前 2482）" in text
    assert "-59" in text
    # 只看 progress 快照
    assert all(progress is True for _p, progress in bot.rank_service.calls)


def test_weekly_boards_text_limits_to_top_n():
    sections = {
        "codeforces": [_row(f"涨{i}", 100 - i, 2000) for i in range(1, 8)]
    }
    bot = _make_bot(sections)
    text = asyncio.run(bot.build_weekly_boards_text(_group()))
    shown = [f"涨{i}" for i in range(1, 8) if f"涨{i}" in text]
    assert len(shown) == bot_main.WEEKLY_BOARD_PUSH_SIZE
    assert "涨1" in text and "涨4" not in text


def test_weekly_boards_text_empty_when_no_data():
    bot = _make_bot({"codeforces": []})
    assert asyncio.run(bot.build_weekly_boards_text(_group())) == ""
    # 只有持平/无基线时同样视为无数据
    bot2 = _make_bot({"codeforces": [_row("持平", 0, 1600)]})
    assert asyncio.run(bot2.build_weekly_boards_text(_group())) == ""


def test_weekly_boards_text_survives_platform_error():
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
    text = asyncio.run(bot.build_weekly_boards_text(_group()))
    assert "涨王" in text  # 一个平台失败不影响另一个平台


# ------------------------------------------------------------------
# 调度：无比赛也必须推送周榜
# ------------------------------------------------------------------
class _FakePlugin:
    def __init__(self, morning_text, boards_text):
        self._morning = morning_text
        self._boards = boards_text
        self.sent: list[str] = []
        self.kv: dict = {}

    async def build_morning_text(self, group):
        return self._morning

    async def build_weekly_boards_text(self, group):
        return self._boards

    async def send_notification(self, group, text):
        self.sent.append(text)
        return True

    async def get_kv_data(self, key, default=None):
        return self.kv.get(key, default)

    async def put_kv_data(self, key, value):
        self.kv[key] = value


def _tick(plugin):
    scheduler = PushScheduler(plugin)
    group = _group()
    from datetime import datetime

    from src.models import CN_TZ

    now = datetime(2026, 9, 13, 8, 0, tzinfo=CN_TZ)
    asyncio.run(scheduler._maybe_morning_push(group, now))
    return plugin


def test_morning_push_includes_boards_with_contest():
    plugin = _fake = _FakePlugin("🌅 今日比赛早报", "📈 本周进步榜")
    _tick(plugin)
    assert len(plugin.sent) == 1
    assert "今日比赛早报" in plugin.sent[0]
    assert "本周进步榜" in plugin.sent[0]


def test_morning_push_still_sends_boards_without_contest():
    """核心需求：当天没有比赛时不发早报正文，但仍要推两个榜单。"""
    plugin = _FakePlugin(None, "📈 本周进步榜\n📉 本周退步榜")
    _tick(plugin)
    assert len(plugin.sent) == 1
    assert "今日比赛早报" not in plugin.sent[0]
    assert "本周进步榜" in plugin.sent[0]
    assert "本周退步榜" in plugin.sent[0]


def test_morning_push_skips_when_both_empty():
    plugin = _FakePlugin(None, "")
    _tick(plugin)
    assert plugin.sent == []
    # 仍然标记为已处理，避免每个 tick 重复计算
    assert plugin.kv.get("morning_g1_20260913") is True


def test_morning_push_sends_contest_only_when_no_boards():
    plugin = _FakePlugin("🌅 今日比赛早报", "")
    _tick(plugin)
    assert len(plugin.sent) == 1
    assert plugin.sent[0] == "🌅 今日比赛早报"
