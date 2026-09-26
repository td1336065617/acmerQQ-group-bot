"""群训练周报（A3）测试：ISO 周幂等、跨周边界、无数据静默、降级与配置回退。

用 `AcmerGroupBot.__new__` 组装最小插件实例（沿用 test_main_accounts 的做法），
不启动 AstrBot、不访问网络；活跃统计通过 monkeypatch `collect_weekly_activity`
注入，避免真实请求。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models import CN_TZ, GroupConfig
from src.weekly_stats import (
    WeeklyActivity,
    clear_weekly_activity_cache,
    collect_weekly_activity,
    summarize_atcoder_rows,
    summarize_cf_rows,
)
from test_main_accounts import _load_main_module  # 复用既有的 main.py 装载器
from test_settlement_tick import FakeContestFetcher, FakeRegistry

GROUP = GroupConfig(group_id="g1", push_platforms=["codeforces", "atcoder"])

#: 2026-09-21 是周一（ISO 2026-W39），2026-09-20 是周日（ISO 2026-W38）
MONDAY = datetime(2026, 9, 21, 20, 0, tzinfo=CN_TZ)
SUNDAY = datetime(2026, 9, 20, 20, 0, tzinfo=CN_TZ)


def _rank_rows(platform: str, delta: int = 120) -> dict:
    return {
        platform: [
            {
                "user_id": "u1",
                "display_name": "张三",
                "handle": "zhangsan",
                "delta": delta,
                "current_display_value": "1500",
            }
        ]
    }


def _activity_ok(user_id: str = "u1") -> WeeklyActivity:
    return WeeklyActivity(
        platform="codeforces",
        user_id=user_id,
        active_days=3,
        submissions=12,
        solved=5,
        source="cf-user-status",
    )


class FakeRankService:
    def __init__(self, rows_by_platform=None, error=None):
        self.rows_by_platform = rows_by_platform or {}
        self.error = error
        self.calls = 0

    async def read(self, group_id, platform, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.rows_by_platform.get(platform, [])), []


def _build_bot(
    main_module,
    *,
    groups=None,
    rank_rows=None,
    settings=None,
    board_cards=None,
    send_ok=True,
):
    bot = main_module.AcmerGroupBot.__new__(main_module.AcmerGroupBot)
    rows = _rank_rows("codeforces") if rank_rows is None else rank_rows
    bot.rank_service = FakeRankService(rows)
    bot.account_registry = FakeRegistry(
        {"g1": ["u1"]},
        {"u1": {"codeforces": {"handle": "zhangsan", "display_name": "张三"}}},
    )
    bot.account_fetcher = object()
    bot.fetcher = FakeContestFetcher({})
    bot._kv = {}
    bot._sent = []
    bot._images = []

    async def get_kv_data(key, default=None):
        return bot._kv.get(key, default)

    async def put_kv_data(key, value):
        bot._kv[key] = value

    async def get_groups():
        return list(groups if groups is not None else [GROUP])

    async def get_settings():
        base = {
            "push_platforms": ["codeforces", "atcoder"],
            "weekly_report_enabled": True,
            "weekly_report_weekday": 1,
            "weekly_report_time": "20:00",
        }
        base.update(settings or {})
        return base

    async def send_notification(group, text):
        bot._sent.append((group.group_id, text))
        return send_ok

    async def send_group_image(group, path, caption=""):
        bot._images.append((group.group_id, str(path), caption))
        return send_ok

    async def build_weekly_board_cards(group):
        return list(board_cards or [])

    bot.get_kv_data = get_kv_data
    bot.put_kv_data = put_kv_data
    bot.get_groups = get_groups
    bot.get_settings = get_settings
    bot.send_notification = send_notification
    bot._send_group_image = send_group_image
    bot.build_weekly_board_cards = build_weekly_board_cards
    return bot


def _patch_activity(monkeypatch, main_module, result=None):
    """把周报里的活跃统计换成固定结果，避免真实网络请求。"""
    calls = []

    async def fake(plugin, platform, members, since_ts):
        calls.append((platform, [item[2] for item in members], since_ts))
        if result is None:
            return {}
        return {item[0]: result for item in members}

    monkeypatch.setattr(main_module, "collect_weekly_activity", fake)
    return calls


def test_tick_pushes_once_per_iso_week(monkeypatch):
    async def scenario():
        main_module = _load_main_module()
        _patch_activity(monkeypatch, main_module, _activity_ok())
        bot = _build_bot(main_module)
        assert await bot.tick_weekly_report(MONDAY) == 1
        assert bot._kv.get("weekly_g1_2026-W39") is True
        # 推送结果同时写入后台「运行状态」日志
        assert bot._kv["push_log"][-1]["kind"] == "weekly_report"
        assert bot._kv["push_log"][-1]["ok"] is True
        assert "本周训练周报" in bot._sent[0][1]
        # 同一 ISO 周内第二次 tick 不再推送
        assert await bot.tick_weekly_report(MONDAY) == 0
        assert len(bot._sent) == 1

    asyncio.run(scenario())


def test_iso_week_boundary_sunday_and_monday_are_different_weeks(monkeypatch):
    async def scenario():
        main_module = _load_main_module()
        _patch_activity(monkeypatch, main_module, _activity_ok())
        # 周日 23:59 与周一 00:01 属于不同的 ISO 周
        sunday_late = SUNDAY.replace(hour=23, minute=59)
        monday_early = MONDAY.replace(hour=0, minute=1)
        assert main_module.AcmerGroupBot._iso_week_key(
            sunday_late
        ) != main_module.AcmerGroupBot._iso_week_key(monday_early)
        assert main_module.AcmerGroupBot._iso_week_key(sunday_late) == "2026-W38"
        assert main_module.AcmerGroupBot._iso_week_key(monday_early) == "2026-W39"

        # 配置为周日推送 → 写 W38；改成周一推送 → 跨周后应再推一次（W39）
        bot = _build_bot(main_module, settings={"weekly_report_weekday": 7})
        assert await bot.tick_weekly_report(SUNDAY) == 1
        assert "weekly_g1_2026-W38" in bot._kv
        bot2 = _build_bot(main_module)
        assert await bot2.tick_weekly_report(MONDAY) == 1
        assert "weekly_g1_2026-W39" in bot2._kv

    asyncio.run(scenario())


def test_no_data_is_silent(monkeypatch):
    async def scenario():
        main_module = _load_main_module()
        _patch_activity(monkeypatch, main_module, None)
        bot = _build_bot(main_module, rank_rows={})
        assert await bot.build_weekly_report(GROUP) is None
        assert await bot.tick_weekly_report(MONDAY) == 0
        assert "weekly_g1_2026-W39" not in bot._kv      # 无数据不写幂等键
        assert bot._sent == []
        # 但「无数据」也要计一次尝试：否则每个群每 tick 都会重算周报（BUG-046）
        assert bot._kv.get("pushattempt_weekly_g1_2026-W39", {}).get("n") == 1

    asyncio.run(scenario())


def test_time_not_reached_does_not_push(monkeypatch):
    async def scenario():
        main_module = _load_main_module()
        _patch_activity(monkeypatch, main_module, _activity_ok())
        bot = _build_bot(main_module)
        # 本周还没到点 -> 不推
        assert await bot.tick_weekly_report(MONDAY.replace(hour=19, minute=59)) == 0
        assert bot._kv == {}
        # 到点之后（含补发窗口，BUG-026）-> 推一次
        assert await bot.tick_weekly_report(MONDAY.replace(hour=20, minute=1)) == 1
        # 关闭开关后同样不推
        bot = _build_bot(main_module, settings={"weekly_report_enabled": False})
        assert await bot.tick_weekly_report(MONDAY) == 0

    asyncio.run(scenario())


def test_activity_failure_falls_back_to_rating_only(monkeypatch):
    async def scenario():
        main_module = _load_main_module()
        # 活跃统计全部失败（返回空）→ 仍应只用 Rating 变化出报
        _patch_activity(monkeypatch, main_module, None)
        bot = _build_bot(main_module)
        report = await bot.build_weekly_report(GROUP)
        assert report is not None
        text = report["text"]
        assert "参与人数：1 人" in text
        assert "进步最多：张三 +120" in text
        assert "未取得 CF/AtCoder 活跃统计" in text
        assert "牛客、洛谷本周报不统计活跃" in text
        assert await bot.tick_weekly_report(MONDAY) == 1

    asyncio.run(scenario())


def test_report_reuses_weekly_board_cards(monkeypatch):
    async def scenario():
        main_module = _load_main_module()
        _patch_activity(monkeypatch, main_module, _activity_ok())
        cards = [
            {
                "title": "本群本周进步榜",
                "image": None,
                "text": "📈 本群本周进步榜\n【Codeforces】\n1. 张三 +120",
            }
        ]
        bot = _build_bot(main_module, board_cards=cards)
        assert await bot.tick_weekly_report(MONDAY) == 1
        texts = [text for _gid, text in bot._sent]
        assert any("本周训练周报" in text for text in texts)
        assert any("本群本周进步榜" in text for text in texts)

    asyncio.run(scenario())


def test_weekly_config_invalid_values_fall_back(monkeypatch):
    main_module = _load_main_module()
    bot = main_module.AcmerGroupBot.__new__(main_module.AcmerGroupBot)
    bot._settings_cache = None
    bot.output_renderer = None
    bot.fetcher = FakeContestFetcher({})

    async def kv_bad(key, default=None):
        return {
            "weekly_report_weekday": 99,
            "weekly_report_time": "25:99",
        }

    bot.get_kv_data = kv_bad
    settings = asyncio.run(bot.get_settings())
    assert settings["weekly_report_enabled"] is True
    assert settings["weekly_report_weekday"] == 1
    assert settings["weekly_report_time"] == "20:00"

    bot._settings_cache = None

    async def kv_good(key, default=None):
        return {
            "weekly_report_enabled": False,
            "weekly_report_weekday": "7",
            "weekly_report_time": "21:30",
        }

    bot.get_kv_data = kv_good
    settings = asyncio.run(bot.get_settings())
    assert settings["weekly_report_enabled"] is False
    assert settings["weekly_report_weekday"] == 7
    assert settings["weekly_report_time"] == "21:30"


def test_weekly_stats_summaries_filter_window():
    since = datetime(2026, 9, 14, tzinfo=timezone.utc).timestamp()
    cf_rows = [
        {"creationTimeSeconds": since + 100, "verdict": "OK",
         "problem": {"contestId": 1, "index": "A"}},
        {"creationTimeSeconds": since + 200, "verdict": "WRONG_ANSWER",
         "problem": {"contestId": 1, "index": "B"}},
        {"creationTimeSeconds": since - 100, "verdict": "OK",
         "problem": {"contestId": 2, "index": "C"}},
    ]
    activity = summarize_cf_rows(cf_rows, since)
    assert (activity.active_days, activity.submissions, activity.solved) == (1, 2, 1)
    assert activity.source == "cf-user-status"

    atcoder_rows = [
        {"epoch_second": since + 10, "result": "AC", "problem_id": "abc1_a"},
        {"epoch_second": since + 20, "result": "AC", "problem_id": "abc1_a"},
        {"epoch_second": since + 30, "result": "WA", "problem_id": "abc1_b"},
        {"epoch_second": since - 10, "result": "AC", "problem_id": "abc1_c"},
    ]
    activity = summarize_atcoder_rows(atcoder_rows, since)
    assert (activity.active_days, activity.submissions, activity.solved) == (1, 3, 1)
    assert activity.source == "atcoder-submissions"


def test_collect_weekly_activity_skips_failures_and_caches():
    async def scenario():
        clear_weekly_activity_cache()

        class FakeFetcher:
            def __init__(self):
                self.calls = []

            async def _cf_json(self, method, params, *, timeout=10.0):
                self.calls.append(params["handle"])
                if params["handle"] == "bad":
                    raise RuntimeError("boom")
                return {
                    "status": "OK",
                    "result": [
                        {
                            "creationTimeSeconds": 1_800_000_000,
                            "verdict": "OK",
                            "problem": {"contestId": 9, "index": "A"},
                        }
                    ],
                }

        class FakePlugin:
            def __init__(self):
                self.account_fetcher = FakeFetcher()

        plugin = FakePlugin()
        members = [("u1", "张三", "good"), ("u2", "李四", "bad")]
        result = await collect_weekly_activity(
            plugin, "codeforces", members, 1_700_000_000
        )
        assert set(result) == {"u1"}
        assert result["u1"].submissions == 1
        # 失败成员不写缓存：下次调用仍会重试 bad
        result = await collect_weekly_activity(
            plugin, "codeforces", members, 1_700_000_000
        )
        assert set(result) == {"u1"}
        assert plugin.account_fetcher.calls.count("bad") == 2
        assert plugin.account_fetcher.calls.count("good") == 1  # 命中 6 小时缓存
        clear_weekly_activity_cache()

    asyncio.run(scenario())


def test_weekly_report_is_repushed_later_in_week(monkeypatch):
    """BUG-026：计划周一 20:00，但周二 10:00 才被触发时也要补发（同一 ISO 周只推一次）。"""

    async def scenario():
        main_module = _load_main_module()
        _patch_activity(monkeypatch, main_module)
        bot = _build_bot(main_module, groups=[GROUP])
        late = datetime(2026, 9, 22, 10, 0, tzinfo=CN_TZ)     # 周二上午
        assert await bot.tick_weekly_report(late) == 1
        # 同周再触发 -> 幂等键命中，不再重复
        assert await bot.tick_weekly_report(late) == 0

    asyncio.run(scenario())
