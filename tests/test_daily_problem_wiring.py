"""每日一题接线测试（A2）：早报追加、指令同源、开关与降级。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models import Contest, GroupConfig
from src.problem_service import Problem
from test_main_accounts import _load_main_module
from test_settlement_tick import FakeContestFetcher, FakeRegistry


class FakeProblemService:
    def __init__(self, pool):
        self.pool = list(pool)
        self.picks = 0

    async def ensure_index(self, platform):
        return list(self.pool)

    @staticmethod
    def _exclude_ids(sets):
        out = set()
        for items in sets or []:
            out.update(str(item) for item in items)
        return out

    def pick_daily(self, *, group_id, day, pool, rating=None, exclude=None, seed_extra=""):
        self.picks += 1
        return pool[0] if pool else None

    def recommend(self, *, pool, solved, weak_tags, rating=None, limit=3):
        solved = {str(item) for item in solved or []}
        return [p for p in pool if p.problem_id not in solved][:limit]


def _pool():
    return [
        Problem("nowcoder", "1001", "牛客题目 #1001", 1200, ["枚举"], "https://ac.nowcoder.com/acm/problem/1001"),
        Problem("nowcoder", "1002", "牛客题目 #1002", 1400, ["图论"], "https://ac.nowcoder.com/acm/problem/1002"),
    ]


def _build_bot(main_module, *, pool, settings=None, today_contests=None):
    bot = main_module.AcmerGroupBot.__new__(main_module.AcmerGroupBot)
    bot.problem_service = FakeProblemService(pool)
    bot.fetcher = FakeContestFetcher({"nowcoder": today_contests or []})
    bot.account_registry = FakeRegistry({"g1": []}, {})
    bot.account_fetcher = type("F", (), {"_cache": {}})()
    bot._kv = {}

    async def get_kv_data(key, default=None):
        return bot._kv.get(key, default)

    async def put_kv_data(key, value):
        bot._kv[key] = value

    async def get_settings():
        base = {
            "push_platforms": ["nowcoder"],
            "daily_problem_enabled": True,
            "daily_problem_platform": "nowcoder",
            "daily_problem_count": 1,
            "recommend_enabled": True,
        }
        base.update(settings or {})
        return base

    class _RankService:
        async def read(self, *args, **kwargs):
            return [{"rating": 1200}, {"rating": 1400}], []

    bot.rank_service = _RankService()

    class _Renderer:
        """只实现 _adaptive_results 需要的判定：短文本不转图。"""

        @staticmethod
        def needs_image(value):
            return False

        @staticmethod
        def configure(*args, **kwargs):
            return None

    bot.output_renderer = _Renderer()
    bot._settings_cache = None
    bot.get_kv_data = get_kv_data
    bot.put_kv_data = put_kv_data
    bot.get_settings = get_settings
    return bot


async def _collect(agen):
    """在已运行的事件循环里收集异步生成器结果。"""
    return [item async for item in agen]


class FakeEvent:
    def __init__(self, group_id="g1"):
        self._group_id = group_id

    def get_group_id(self):
        return self._group_id

    def plain_result(self, text):
        return ("plain", text)

    def image_result(self, path):
        return ("image", path)


def test_morning_text_appends_daily_problem():
    async def scenario():
        main_module = _load_main_module()
        contest = Contest(
            platform="nowcoder",
            name="牛客练习赛157",
            start_time=main_module.datetime.now(main_module.timezone.utc),
            duration_minutes=120,
            url="",
            contest_id="140236",
        )
        bot = _build_bot(main_module, pool=_pool(), today_contests=[contest])
        text = await bot.build_morning_text(GroupConfig(group_id="g1", push_platforms=["nowcoder"]))
        assert text and "今日一题" in text
        assert "牛客题目 #1001" in text
        assert "https://ac.nowcoder.com/acm/problem/1001" in text
        # 同一天第二次调用命中 KV，不再抽题
        picks_before = bot.problem_service.picks
        await bot.build_morning_text(GroupConfig(group_id="g1", push_platforms=["nowcoder"]))
        assert bot.problem_service.picks == picks_before

    asyncio.run(scenario())


def test_morning_text_respects_switch_and_empty_pool():
    async def scenario():
        main_module = _load_main_module()
        contest = Contest(
            platform="nowcoder",
            name="比赛",
            start_time=main_module.datetime.now(main_module.timezone.utc),
            duration_minutes=120,
            url="",
            contest_id="1",
        )
        bot = _build_bot(
            main_module,
            pool=_pool(),
            today_contests=[contest],
            settings={"daily_problem_enabled": False},
        )
        text = await bot.build_morning_text(GroupConfig(group_id="g1", push_platforms=["nowcoder"]))
        assert "今日一题" not in text
        # 题目池为空 → 不追加且不报错
        bot = _build_bot(main_module, pool=[], today_contests=[contest])
        text = await bot.build_morning_text(GroupConfig(group_id="g1", push_platforms=["nowcoder"]))
        assert text and "今日一题" not in text

    asyncio.run(scenario())


def test_daily_problem_command_is_same_as_morning():
    async def scenario():
        main_module = _load_main_module()
        bot = _build_bot(main_module, pool=_pool())
        results = await _collect(bot._reply_daily_problem(FakeEvent()))
        assert results and results[0][0] == "plain"
        assert "牛客题目 #1001" in results[0][1]
        # 与早报同源：KV 已写入题号
        keys = [key for key in bot._kv if key.startswith("daily_g1_")]
        assert keys and bot._kv[keys[0]]["problem_id"] == "1001"

    asyncio.run(scenario())


def test_daily_problem_command_degrades_without_pool():
    async def scenario():
        main_module = _load_main_module()
        bot = _build_bot(main_module, pool=[])
        results = await _collect(bot._reply_daily_problem(FakeEvent()))
        assert "题目池尚未就绪" in results[0][1]

    asyncio.run(scenario())


def test_recommend_skips_solved_and_respects_switch():
    async def scenario():
        main_module = _load_main_module()
        bot = _build_bot(main_module, pool=_pool())

        class Profile:
            analysis = {
                "solved_problem_ids": ["1001"],
                "category_distribution": [{"label": "图论", "count": 2}],
            }
            rating = 1300

        results = await _collect(
            bot._maybe_recommend_problems(FakeEvent(), "nowcoder", Profile())
        )
        assert results and "推荐补题" in results[0][1]
        assert "1002" in results[0][1]
        assert "1001" not in results[0][1]

        bot = _build_bot(main_module, pool=_pool(), settings={"recommend_enabled": False})
        assert (
            await _collect(
                bot._maybe_recommend_problems(FakeEvent(), "nowcoder", Profile())
            )
            == []
        )

    asyncio.run(scenario())
