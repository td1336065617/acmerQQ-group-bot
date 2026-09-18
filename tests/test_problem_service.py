"""题目池与抽题/推荐测试（A2）。"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.problem_service import (
    DIFFICULTY_BANDS,
    PROBLEM_INDEX_MIN_ENTRIES,
    Problem,
    ProblemService,
    band_of_difficulty,
    band_of_rating,
    weak_tags_from_analysis,
)
from src.problem_tags import CANONICAL_TAGS, canonical_tags, rotate_tag


class FakeAccountFetcher:
    """最小替身：只提供题目索引相关能力。"""

    def __init__(self, index=None, index_path=None):
        self._nowcoder_problem_index = dict(index or {})
        self._problem_index_path = Path(index_path) if index_path else Path("/tmp/none.json")
        self.calls: list = []

    def nowcoder_problem_index_ready(self):
        return bool(self._nowcoder_problem_index)

    def index_path(self):
        return self._problem_index_path

    #: 索引条目阈值是 500，假数据必须超过它，否则会被判为残缺
    FILLER = 600

    async def _cf_json(self, method, params, *, timeout=10.0):
        self.calls.append(("cf", method, timeout))
        special = [
            {
                "contestId": 1000,
                "index": "A",
                "name": "Warm up",
                "rating": 800,
                "tags": ["implementation", "brute force"],
            },
            {
                "contestId": 1000,
                "index": "B",
                "name": "Graphs",
                "rating": 1500,
                "tags": ["graphs", "dfs and similar"],
            },
            {
                "contestId": 1001,
                "index": "C",
                "name": "No rating yet",
                "tags": ["math"],
            },
        ]
        filler = [
            {
                "contestId": 2000 + i,
                "index": "A",
                "name": f"Filler {i}",
                "rating": 800 + (i % 10) * 200,
                "tags": ["greedy"],
            }
            for i in range(self.FILLER)
        ]
        return {"status": "OK", "result": {"problems": special + filler}}

    async def _fetch_json(self, url, **kwargs):
        self.calls.append(("json", url))
        if url.endswith("problems.json"):
            rows = [
                {"id": "abc100_a", "contest_id": "abc100", "title": "A - Warm"},
                {"id": "abc100_d", "contest_id": "abc100", "title": "D - Hard"},
            ]
            rows += [
                {
                    "id": f"abc{200 + i}_a",
                    "contest_id": f"abc{200 + i}",
                    "title": f"Filler {i}",
                }
                for i in range(self.FILLER)
            ]
            return rows
        models = {"abc100_a": {"difficulty": 200}, "abc100_d": {"difficulty": 1600}}
        models.update(
            {f"abc{200 + i}_a": {"difficulty": 900 + (i % 5) * 200} for i in range(self.FILLER)}
        )
        return models


class FakePlugin:
    def __init__(self, fetcher):
        self.account_fetcher = fetcher


def _service(index=None, index_path=None):
    return ProblemService(FakePlugin(FakeAccountFetcher(index, index_path)))


def _index(count: int = 60, start: int = 1000):
    """构造一批牛客题库索引条目：难度 800/1000/1200…，标签在几个规范标签间轮换。"""
    tags = ["枚举", "贪心", "动态规划", "图论", "数学"]
    return {
        str(start + i): {
            "d": 800 + (i % 6) * 200,
            "t": [tags[i % len(tags)]],
        }
        for i in range(count)
    }


# ----------------------------------------------------------------------
# 标签归一
# ----------------------------------------------------------------------


def test_canonical_tags_maps_platform_aliases():
    assert canonical_tags("codeforces", ["dp", "graphs", "greedy"]) == [
        "动态规划",
        "图论",
        "贪心",
    ]
    assert canonical_tags("nowcoder", ["暴力", "枚举"]) == ["枚举"]       # 去重
    assert canonical_tags("luogu", ["动态规划", "未知标签"]) == ["动态规划"]
    assert canonical_tags("atcoder", ["abc"]) == ["模拟"]


def test_rotate_tag_is_stable_per_weekday():
    assert rotate_tag(0) == rotate_tag(len(CANONICAL_TAGS))   # 周期性
    assert rotate_tag(1) != rotate_tag(2)


# ----------------------------------------------------------------------
# 难度档
# ----------------------------------------------------------------------


def test_band_mapping():
    assert band_of_rating(800) == 0
    assert band_of_rating(1200) == 2
    assert band_of_rating(None) == 2                # 无 Rating 时取中间档
    assert band_of_difficulty(1500) == 3
    assert band_of_difficulty(None) is None
    assert DIFFICULTY_BANDS[0][0] is None


# ----------------------------------------------------------------------
# 牛客池
# ----------------------------------------------------------------------


def test_nowcoder_pool_reads_index():
    service = _service(_index(5))
    pool = service.nowcoder_pool()
    assert len(pool) == 5
    assert all(problem.platform == "nowcoder" for problem in pool)
    assert pool[0].url.startswith("https://ac.nowcoder.com/acm/problem/")
    assert all(isinstance(problem.difficulty, int) for problem in pool)


def test_nowcoder_pool_empty_without_index():
    assert _service().nowcoder_pool() == []


# ----------------------------------------------------------------------
# 抽题（确定性）
# ----------------------------------------------------------------------


def test_pick_daily_is_deterministic():
    service = _service()
    pool = service.nowcoder_pool() if False else [
        Problem("nowcoder", str(1000 + i), f"题{i}", 800 + (i % 6) * 200,
                canonical_tags("nowcoder", ["枚举"]), "")
        for i in range(40)
    ]
    first = service.pick_daily(group_id="g1", day="2026-09-18", pool=pool, rating=1200)
    second = service.pick_daily(group_id="g1", day="2026-09-18", pool=pool, rating=1200)
    assert first is not None and first.problem_id == second.problem_id


def test_pick_daily_varies_by_group_and_day():
    pool = [
        Problem("nowcoder", str(1000 + i), f"题{i}", 1200, ["枚举"], "")
        for i in range(30)
    ]
    service = _service()
    picks = {
        service.pick_daily(group_id=gid, day="2026-09-18", pool=pool, rating=1200).problem_id
        for gid in ("g1", "g2", "g3", "g4")
    }
    assert len(picks) >= 2                      # 不同群应能抽到不同题
    days = {
        service.pick_daily(group_id="g1", day=day, pool=pool, rating=1200).problem_id
        for day in ("2026-09-18", "2026-09-19", "2026-09-20")
    }
    assert len(days) >= 2


def test_pick_daily_respects_band_and_exclusions():
    pool = [
        Problem("nowcoder", "low", "简单", 500, ["枚举"], ""),
        Problem("nowcoder", "mid", "适中", 1200, ["枚举"], ""),
        Problem("nowcoder", "high", "困难", 3000, ["枚举"], ""),
        Problem("nowcoder", "unrated", "未评定", None, ["枚举"], ""),
    ]
    pool.append(Problem("nowcoder", "mid2", "也适中", 1400, ["枚举"], ""))
    service = _service()
    picked = service.pick_daily(
        group_id="g1", day="2026-09-18", pool=pool, rating=1200
    )
    assert picked.problem_id in {"mid", "mid2"}          # 只有这两道在 band±1 内
    # 排除其中一道后仍能抽到另一道
    picked = service.pick_daily(
        group_id="g1", day="2026-09-18", pool=pool, rating=1200, exclude={"mid"}
    )
    assert picked.problem_id == "mid2"
    # 全部排除 → 无候选
    assert (
        service.pick_daily(
            group_id="g1",
            day="2026-09-18",
            pool=pool,
            rating=1200,
            exclude={"mid", "mid2"},
        )
        is None
    )
    # 池为空
    assert service.pick_daily(group_id="g1", day="2026-09-18", pool=[]) is None


def test_pick_daily_prefers_rotating_tag():
    weekday = 3
    tag = rotate_tag(weekday)
    day = "2026-09-17"                            # 周四
    assert day  # 仅用于可读性
    pool = [
        Problem("nowcoder", "a", "其他标签", 1200, ["数学" if tag != "数学" else "图论"], ""),
        Problem("nowcoder", "b", "命中轮换标签", 1200, [tag], ""),
    ]
    service = _service()
    picked = service.pick_daily(group_id="g1", day=day, pool=pool, rating=1200)
    assert picked.problem_id == "b"


# ----------------------------------------------------------------------
# 推荐
# ----------------------------------------------------------------------


def test_recommend_prefers_weak_tags_and_skips_solved():
    pool = [
        Problem("nowcoder", "solved", "已通过", 1200, ["图论"], ""),
        Problem("nowcoder", "graph", "图论题", 1200, ["图论"], ""),
        Problem("nowcoder", "math", "数学题", 1200, ["数学"], ""),
        Problem("nowcoder", "hard", "太难", 3000, ["图论"], ""),
    ]
    service = _service()
    picks = service.recommend(
        pool=pool, solved=["solved"], weak_tags=["图论"], rating=1200, limit=2
    )
    assert [p.problem_id for p in picks][0] == "graph"
    assert "solved" not in {p.problem_id for p in picks}
    assert "hard" not in {p.problem_id for p in picks}


def test_recommend_falls_back_when_weak_tag_has_no_candidate():
    pool = [Problem("nowcoder", "math", "数学题", 1200, ["数学"], "")]
    service = _service()
    picks = service.recommend(
        pool=pool, solved=[], weak_tags=["博弈"], rating=1200, limit=3
    )
    assert [p.problem_id for p in picks] == ["math"]


def test_weak_tags_from_analysis_sorted_ascending():
    analysis = {
        "category_distribution": [
            {"label": "图论", "count": 40},
            {"label": "数学", "count": 5},
            {"label": "枚举", "count": 12},
        ]
    }
    assert weak_tags_from_analysis(analysis, limit=2) == ["数学", "枚举"]


# ----------------------------------------------------------------------
# 落盘索引
# ----------------------------------------------------------------------


def test_ensure_index_builds_and_persists(tmp_path):
    async def scenario():
        path = tmp_path / "problem_index_codeforces.json"
        service = _service(index_path=path)
        problems = await service.ensure_index("codeforces")
        assert len(problems) == FakeAccountFetcher.FILLER + 3
        assert path.is_file()
        # 只解析出带 contestId/index 的题；无 rating 的题保留但 difficulty=None
        by_id = {p.problem_id: p for p in problems}
        assert by_id["1000B"].difficulty == 1500
        assert by_id["1000B"].tags == ["图论", "深度优先搜索"]
        assert by_id["1001C"].difficulty is None

        # 二次调用命中内存缓存（不再请求）
        calls_before = len(service.plugin.account_fetcher.calls)
        again = await service.ensure_index("codeforces")
        assert len(again) == len(problems)
        assert len(service.plugin.account_fetcher.calls) == calls_before

        # 新实例从磁盘加载
        fresh = _service(index_path=path)
        loaded = await fresh.ensure_index("codeforces")
        assert len(loaded) == len(problems)
        assert fresh.plugin.account_fetcher.calls == []

    asyncio.run(scenario())


def test_ensure_index_ignores_incomplete_disk_file(tmp_path):
    async def scenario():
        path = tmp_path / "problem_index_luogu.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "fetched_at": 9e9,
                    "problems": [{"id": "P1000", "title": "A+B", "difficulty": 500}],
                }
            ),
            encoding="utf-8",
        )
        service = _service(index_path=path)
        # 条目数不足阈值 → 视为残缺，且洛谷构建在本替身里没有数据 → 返回空
        assert len(await service.ensure_index("luogu")) < PROBLEM_INDEX_MIN_ENTRIES

    asyncio.run(scenario())


def test_atcoder_index_uses_kenkoooo_models(tmp_path):
    async def scenario():
        service = _service(index_path=tmp_path / "problem_index_atcoder.json")
        problems = await service.ensure_index("atcoder")
        by_id = {p.problem_id: p for p in problems}
        assert by_id["abc100_a"].difficulty == 200
        assert by_id["abc100_d"].difficulty == 1600
        assert by_id["abc100_a"].url == "https://atcoder.jp/contests/abc100/tasks/abc100_a"

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 展示文案：只报题目名
# ----------------------------------------------------------------------


def test_display_reports_only_problem_name():
    problem = Problem(
        "nowcoder", "223861", "小月的筹码", 1600, ["排序", "二分", "分治"], ""
    )
    assert problem.display() == "小月的筹码"
    # 没有题目名时退回题号，且仍然不带难度/知识点
    fallback = Problem("nowcoder", "999", "", 1200, ["枚举"], "")
    assert fallback.display() == "题目 999"
    assert "难度" not in fallback.display() and "枚举" not in fallback.display()


def test_nowcoder_pool_uses_problem_name_from_index():
    service = _service({"1001": {"n": "小月的筹码", "d": 1600, "t": ["排序"]}})
    pool = service.nowcoder_pool()
    assert [p.title for p in pool] == ["小月的筹码"]
    assert pool[0].display() == "小月的筹码"


def test_nowcoder_pool_falls_back_when_name_missing():
    """旧索引没有 n 字段时退回题号，避免展示空白。"""
    service = _service({"1002": {"d": 1200, "t": ["枚举"]}})
    pool = service.nowcoder_pool()
    assert pool[0].title == "牛客题目 #1002"
