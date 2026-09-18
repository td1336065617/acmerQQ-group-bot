"""牛客资料卡数据范围测试：练习页翻页上限、题库索引、覆盖率文案。

对应方案 `docs/牛客数据获取范围优化方案.md` §2.7.1。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.account_fetcher import (
    NOWCODER_ANALYSIS_MAX_PAGES,
    NOWCODER_ANALYSIS_PAGE_SIZE,
    NOWCODER_ANALYSIS_SCAN_LIMIT,
    NOWCODER_PROBLEM_INDEX_VERSION,
    AccountFetcher,
    AccountFetchError,
)

# ----------------------------------------------------------------------
# 页数计算（纯函数）
# ----------------------------------------------------------------------


def test_practice_pages_uses_submission_count():
    pages = AccountFetcher._nowcoder_practice_pages
    assert pages(0, None) == 1
    assert pages(1, None) == 1
    assert pages(200, None) == 1
    assert pages(201, None) == 2
    assert pages(3816, None) == 20
    assert pages(20000, None) == NOWCODER_ANALYSIS_MAX_PAGES
    # 超过上限时按上限截断（覆盖率文案会提示“最多读取 20000 条”）
    assert pages(26114, None) == NOWCODER_ANALYSIS_MAX_PAGES


def test_practice_pages_falls_back_to_data_total():
    pages = AccountFetcher._nowcoder_practice_pages
    # 提交数缺失时用 data-total（它是当前 pageSize 下的页数）
    assert pages(None, 3) == 3
    assert pages(None, 500) == NOWCODER_ANALYSIS_MAX_PAGES
    assert pages(None, None) == 1


# ----------------------------------------------------------------------
# 练习页抓取
# ----------------------------------------------------------------------


def _practice_html(
    submission_count: int,
    rows: list[tuple[str, str, str, str]],
    data_total: int | None = None,
) -> str:
    """构造练习页 HTML：rows = [(submission_id, problem_id, result, language)]"""
    body = "".join(
        f"""
        <tr>
          <td><a href="submission?submissionId={sid}">{sid}</a></td>
          <td><a href="/acm/problem/{pid}">题目{pid}</a></td>
          <td>{result}</td><td>100</td><td>1</td><td>2</td><td>3</td>
          <td>{language}</td><td>2026-09-15 12:00:00</td>
        </tr>"""
        for sid, pid, result, language in rows
    )
    pager = (
        f'<div class="pagination"><ul data-total="{data_total}"></ul></div>'
        if data_total is not None
        else ""
    )
    return f"""
    <div class="my-state-item"><div class="state-num">2</div><span>题已挑战</span></div>
    <div class="my-state-item"><div class="state-num">1</div><span>题已通过</span></div>
    <div class="my-state-item"><div class="state-num">{submission_count}</div><span>次提交</span></div>
    <table><tbody>{body}</tbody></table>
    {pager}
    """


def _paginated_fetcher(monkeypatch, pages: dict[int, str | None], index_path: Path):
    """把 _fetch_text 换成按 page 参数返回固定 HTML 的假实现。"""
    calls: list[int] = []

    async def fake_fetch_text(url, *, headers=None, retries=2, timeout=10.0):
        if "practice-coding" not in url:
            return "<html></html>"
        page = int(parse_qs(urlparse(url).query).get("page", ["1"])[0])
        calls.append(page)
        text = pages.get(page, "")
        if text is None:
            raise AccountFetchError("模拟分页失败")
        return text

    fetcher = AccountFetcher(problem_index_path=index_path)
    monkeypatch.setattr(fetcher, "_fetch_text", fake_fetch_text)
    return fetcher, calls


def test_analysis_reads_all_pages_and_reports_complete(monkeypatch, tmp_path):
    async def scenario():
        # 提交数 3816 → 20 页 @200；每页 200 行、第 1 页也满
        rows_per_page = {
            page: [
                (str(page * 1000 + i), "100", "答案正确", "C++")
                for i in range(200)
            ]
            for page in range(1, 20)
        }
        rows_per_page[20] = [
            (str(20 * 1000 + i), "100", "答案正确", "C++") for i in range(16)
        ]
        pages = {
            page: _practice_html(
                3816,
                rows,
                data_total=20 if page == 1 else None,
            )
            for page, rows in rows_per_page.items()
        }
        fetcher, calls = _paginated_fetcher(monkeypatch, pages, tmp_path / "idx.json")
        analysis = await fetcher._fetch_nowcoder_analysis("1")
        assert len(calls) == 20
        assert analysis["submission_count"] == 3816
        assert analysis["coverage"].startswith("练习页读取 3816/3816 条提交")
        # 完整读取时给出 AC 率
        assert analysis["acceptance_rate"] == 100.0
        assert "最多读取" not in analysis["coverage"]

    asyncio.run(scenario())


def test_analysis_truncates_at_scan_limit(monkeypatch, tmp_path):
    async def scenario():
        # 提交数超过上限：只读 100 页（每页 200 行），并提示上限；不给 AC 率
        rows = [
            (str(100000 + i), "100", "答案正确", "C++") for i in range(200)
        ]
        pages = {
            page: _practice_html(
                26114, rows, data_total=131 if page == 1 else None
            )
            for page in range(1, 101)
        }
        fetcher, calls = _paginated_fetcher(monkeypatch, pages, tmp_path / "idx.json")
        analysis = await fetcher._fetch_nowcoder_analysis("1")
        assert len(calls) == NOWCODER_ANALYSIS_MAX_PAGES
        assert analysis["acceptance_rate"] is None
        assert (
            f"最多读取 {NOWCODER_ANALYSIS_SCAN_LIMIT} 条 / "
            f"{NOWCODER_ANALYSIS_MAX_PAGES} 页" in analysis["coverage"]
        )

    asyncio.run(scenario())


def test_analysis_stops_on_empty_page(monkeypatch, tmp_path):
    async def scenario():
        rows = [
            (str(200000 + i), "100", "答案正确", "C++") for i in range(200)
        ]
        # 提交数 20000 → 100 页；第 3 页为空（提交被清空/隐私）→
        # 当前批次（第 2~7 页）结束后立即停止，不再请求第 8~100 页。
        pages = {1: _practice_html(20000, rows, data_total=100)}
        for page in range(2, 101):
            pages[page] = _practice_html(20000, [] if page == 3 else rows)
        fetcher, calls = _paginated_fetcher(monkeypatch, pages, tmp_path / "idx.json")
        analysis = await fetcher._fetch_nowcoder_analysis("1")
        assert sorted(calls) == [1, 2, 3, 4, 5, 6, 7]
        assert analysis["acceptance_rate"] is None

    asyncio.run(scenario())


def test_analysis_marks_missing_pages(monkeypatch, tmp_path):
    async def scenario():
        rows = [(f"1-{i}", "100", "答案正确", "C++") for i in range(200)]
        pages = {
            1: _practice_html(400, rows, data_total=2),
            2: None,  # 抓取失败
        }
        fetcher, calls = _paginated_fetcher(monkeypatch, pages, tmp_path / "idx.json")
        analysis = await fetcher._fetch_nowcoder_analysis("1")
        assert sorted(calls) == [1, 2]
        assert analysis["acceptance_rate"] is None
        assert "部分分页读取失败" in analysis["coverage"]

    asyncio.run(scenario())


def test_analysis_dedupes_rows_across_pages(monkeypatch, tmp_path):
    async def scenario():
        first = [
            (str(300000 + i), "100", "答案正确", "C++") for i in range(200)
        ]
        second = first[-10:] + [
            (str(400000 + i), "100", "答案错误", "Python") for i in range(90)
        ]
        pages = {
            1: _practice_html(300, first, data_total=2),
            # 第 2 页因分页边界偏移重复了第 1 页的最后 10 条
            2: _practice_html(300, second),
        }
        fetcher, _ = _paginated_fetcher(monkeypatch, pages, tmp_path / "idx.json")
        analysis = await fetcher._fetch_nowcoder_analysis("1")
        # 200 + 100 - 10 条重复 = 290 条
        assert analysis["coverage"].startswith("练习页读取 290/300 条提交")
        assert analysis["language_distribution"] == [
            {"label": "C++", "count": 200},
            {"label": "Python", "count": 90},
        ]

    asyncio.run(scenario())


def test_analysis_falls_back_to_smaller_page_size(monkeypatch, tmp_path):
    async def scenario():
        rows = [(f"1-{i}", "100", "答案正确", "C++") for i in range(2)]
        seen: list[int] = []

        async def fake_fetch_text(url, *, headers=None, retries=2, timeout=10.0):
            if "practice-coding" not in url:
                return "<html></html>"
            size = int(parse_qs(urlparse(url).query).get("pageSize", ["0"])[0])
            seen.append(size)
            if size == NOWCODER_ANALYSIS_PAGE_SIZE:
                # 模拟对端收紧 pageSize：首页无任何数据
                return "<html><body>pageSize is too big</body></html>"
            return _practice_html(2, rows, data_total=None)

        fetcher = AccountFetcher(problem_index_path=tmp_path / "idx.json")
        monkeypatch.setattr(fetcher, "_fetch_text", fake_fetch_text)
        analysis = await fetcher._fetch_nowcoder_analysis("1")
        assert seen[:2] == [NOWCODER_ANALYSIS_PAGE_SIZE, 100]
        assert analysis["submission_count"] == 2

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 题库索引
# ----------------------------------------------------------------------


def _index_page_payload(items: list[dict], count: int = 14446) -> dict:
    return {
        "msg": "OK",
        "code": 0,
        "data": {"problemCount": count, "problemSets": items},
    }


def _problem_entry(
    problem_id: int, difficulty, tags: list[str], name: str = ""
) -> dict:
    return {
        "problemId": problem_id,
        "name": name or f"题目{problem_id}",
        "difficulty": difficulty,
        "tagList": [{"name": tag} for tag in tags],
    }


def test_parse_index_page_normalizes_sentinel_difficulty():
    problems, count = AccountFetcher._parse_nowcoder_problem_index_page(
        _index_page_payload(
            [
                _problem_entry(1, 1500, ["图论", "图论", "  "]),
                _problem_entry(2, 3, ["暴力"]),  # 未评定哨兵值
                _problem_entry(3, -1, []),
                _problem_entry(4, None, []),
            ]
        )
    )
    assert count == 14446
    # n = 题目名（每日一题/推荐补题直接展示），d = 难度，t = 知识点
    assert problems["1"] == {"n": "题目1", "d": 1500, "t": ["图论"]}
    assert problems["2"] == {"n": "题目2", "d": None, "t": ["暴力"]}
    assert problems["3"] == {"n": "题目3", "d": None, "t": []}
    assert problems["4"] == {"n": "题目4", "d": None, "t": []}


def test_index_version_bumped_for_problem_names():
    """索引新增题目名字段 → 版本号必须提升，否则旧索引（无题目名）会被继续使用。"""
    from src.account_fetcher import NOWCODER_PROBLEM_INDEX_VERSION

    assert NOWCODER_PROBLEM_INDEX_VERSION >= 2


def test_index_meta_carries_problem_name():
    meta = AccountFetcher._meta_from_index("100", {"n": "小月的筹码", "d": 1500, "t": ["图论"]})
    assert meta["title"] == "小月的筹码"
    assert meta["difficulty"] == 1500


def test_build_index_uses_problem_count_to_page(monkeypatch, tmp_path):
    async def scenario():
        fetcher = AccountFetcher(problem_index_path=tmp_path / "idx.json")
        requested: list[int] = []

        async def fake_page(page: int):
            requested.append(page)
            return (
                {
                    str(page * 1000 + i): {"d": 800, "t": []}
                    for i in range(50)
                },
                14446 if page == 1 else None,
            )

        monkeypatch.setattr(fetcher, "_fetch_nowcoder_problem_index_page", fake_page)
        problems = await fetcher.build_nowcoder_problem_index()
        # ceil(14446 / 50) = 289 页，全部请求过
        assert sorted(requested) == list(range(1, 290))
        assert len(problems) == 289 * 50

    asyncio.run(scenario())


def test_build_index_fails_on_too_many_missing_pages(monkeypatch, tmp_path):
    async def scenario():
        fetcher = AccountFetcher(problem_index_path=tmp_path / "idx.json")

        async def fake_page(page: int):
            if page % 2 == 0:
                raise AccountFetchError("模拟失败")
            return (
                {str(page * 1000 + i): {"d": 800, "t": []} for i in range(50)},
                200 if page == 1 else None,
            )

        monkeypatch.setattr(fetcher, "_fetch_nowcoder_problem_index_page", fake_page)
        try:
            await fetcher.build_nowcoder_problem_index()
        except AccountFetchError as exc:
            assert "抓取失败" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("缺页率过高时应判定构建失败")

    asyncio.run(scenario())


def test_problem_metadata_prefers_index_and_touches_no_network(monkeypatch, tmp_path):
    async def scenario():
        fetcher = AccountFetcher(problem_index_path=tmp_path / "idx.json")
        fetcher._nowcoder_problem_index = {
            "100": {"d": 1500, "t": ["图论"]},
            "101": {"d": None, "t": []},
        }
        fetcher._nowcoder_problem_index_loaded_at = time.time()

        async def unexpected(*args, **kwargs):
            raise AssertionError("索引命中时不应发起网络请求")

        monkeypatch.setattr(fetcher, "_fetch_text", unexpected)
        metadata = await fetcher._fetch_nowcoder_problem_metadata(["100", "101"])
        assert metadata["100"]["difficulty"] == 1500
        assert metadata["100"]["tags"] == ["图论"]
        assert metadata["101"]["difficulty"] is None

    asyncio.run(scenario())


def test_problem_metadata_falls_back_to_single_lookup_and_caches(monkeypatch, tmp_path):
    async def scenario():
        fetcher = AccountFetcher(problem_index_path=tmp_path / "idx.json")
        fetcher._nowcoder_problem_index = {"100": {"d": 1500, "t": ["图论"]}}
        fetcher._nowcoder_problem_index_loaded_at = time.time()
        calls: list[str] = []

        async def fake_fetch_text(url, *, headers=None, retries=2, timeout=10.0):
            calls.append(url)
            if "/json" in url:
                return json.dumps(_index_page_payload([_problem_entry(200, 900, ["模拟"])]))
            return "<html></html>"

        monkeypatch.setattr(fetcher, "_fetch_text", fake_fetch_text)
        metadata = await fetcher._fetch_nowcoder_problem_metadata(["100", "200"])
        assert len(calls) == 1 and "/json" in calls[0]
        assert metadata["200"]["difficulty"] == 900
        # 单题结果写回索引，供后续用户复用
        assert fetcher._nowcoder_problem_index["200"] == {"d": 900, "t": ["模拟"]}
        assert fetcher._nowcoder_problem_index_dirty is True

    asyncio.run(scenario())


def test_problem_metadata_limited_when_index_unavailable(monkeypatch, tmp_path):
    async def scenario():
        fetcher = AccountFetcher(problem_index_path=tmp_path / "missing.json")
        problems = [str(100 + i) for i in range(400)]
        calls: list[str] = []

        async def fake_fetch_text(url, *, headers=None, retries=2, timeout=10.0):
            calls.append(url)
            if "/json" in url:
                problem_id = parse_qs(urlparse(url).query)["keyword"][0]
                return json.dumps(
                    _index_page_payload([_problem_entry(int(problem_id), 800, [])])
                )
            return "<html></html>"

        monkeypatch.setattr(fetcher, "_fetch_text", fake_fetch_text)
        metadata = await fetcher._fetch_nowcoder_problem_metadata(problems)
        # 索引不可用时退回逐题抓取，且限制在降级上限内
        assert len(calls) == 300
        assert len(metadata) == 300

    asyncio.run(scenario())


def test_index_persistence_round_trip_and_version_check(tmp_path):
    path = tmp_path / "idx.json"
    fetcher = AccountFetcher(problem_index_path=path)
    fetcher._nowcoder_problem_index = {
        str(i): {"d": 800, "t": ["模拟"]} for i in range(6000)
    }
    fetcher._nowcoder_problem_index_loaded_at = time.time()
    fetcher._nowcoder_problem_index_dirty = True
    fetcher.save_nowcoder_problem_index()
    assert path.is_file()

    other = AccountFetcher(problem_index_path=path)
    assert other.load_nowcoder_problem_index() == 6000
    assert other.nowcoder_problem_index_ready() is True
    assert other._nowcoder_problem_index["0"] == {"d": 800, "t": ["模拟"]}

    # 版本不匹配 → 忽略旧索引
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["version"] = NOWCODER_PROBLEM_INDEX_VERSION + 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    third = AccountFetcher(problem_index_path=path)
    assert third.load_nowcoder_problem_index() == 0

    # 条目数过少 → 忽略（认为抓取残缺）
    payload["version"] = NOWCODER_PROBLEM_INDEX_VERSION
    payload["problems"] = {"1": {"d": 800, "t": []}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    fourth = AccountFetcher(problem_index_path=path)
    assert fourth.load_nowcoder_problem_index() == 0


def test_warm_index_sets_backoff_on_failure(monkeypatch, tmp_path):
    async def scenario():
        fetcher = AccountFetcher(problem_index_path=tmp_path / "idx.json")
        calls = 0

        async def failed():
            nonlocal calls
            calls += 1
            raise AccountFetchError("network down")

        monkeypatch.setattr(fetcher, "build_nowcoder_problem_index", failed)
        assert await fetcher.warm_nowcoder_problem_index() is False
        assert calls == 1
        # 退避窗口内不再重试
        assert await fetcher.warm_nowcoder_problem_index() is False
        assert calls == 1

    asyncio.run(scenario())


def test_warm_index_skips_fresh_index(monkeypatch, tmp_path):
    async def scenario():
        fetcher = AccountFetcher(problem_index_path=tmp_path / "idx.json")
        fetcher._nowcoder_problem_index = {"1": {"d": 800, "t": []}}
        fetcher._nowcoder_problem_index_loaded_at = time.time()

        async def unexpected():
            raise AssertionError("索引新鲜时不应重建")

        monkeypatch.setattr(fetcher, "build_nowcoder_problem_index", unexpected)
        assert await fetcher.warm_nowcoder_problem_index() is False

    asyncio.run(scenario())


def test_analysis_uses_index_for_all_solved_problems(monkeypatch, tmp_path):
    async def scenario():
        fetcher = AccountFetcher(problem_index_path=tmp_path / "idx.json")
        # 通过题 400 道（原先被截断到 300 道）
        fetcher._nowcoder_problem_index = {
            str(1000 + i): {"d": 1500, "t": ["图论"]} for i in range(400)
        }
        fetcher._nowcoder_problem_index_loaded_at = time.time()
        rows = [
            (f"s-{i}", str(1000 + i), "答案正确", "C++") for i in range(400)
        ]
        pages = {1: _practice_html(400, rows, data_total=None)}

        async def fake_fetch_text(url, *, headers=None, retries=2, timeout=10.0):
            page = int(parse_qs(urlparse(url).query).get("page", ["1"])[0])
            return pages.get(page, "")

        monkeypatch.setattr(fetcher, "_fetch_text", fake_fetch_text)
        analysis = await fetcher._fetch_nowcoder_analysis("1")
        assert "题目元数据 400/400" in analysis["coverage"]
        assert analysis["difficulty_distribution"] == [
            {"label": "1400–1799", "count": 400}
        ]
        assert "题目索引构建中" not in analysis["coverage"]

    asyncio.run(scenario())


def test_missing_problems_are_negative_cached(monkeypatch, tmp_path):
    """题库列表里没有的题目（比赛题/定制自测题）只查一次，之后不再补查。"""

    async def scenario():
        fetcher = AccountFetcher(problem_index_path=tmp_path / "idx.json")
        fetcher._nowcoder_problem_index = {"100": {"d": 1500, "t": []}}
        fetcher._nowcoder_problem_index_loaded_at = time.time()
        calls: list[str] = []

        async def fake_fetch_text(url, *, headers=None, retries=2, timeout=10.0):
            calls.append(url)
            # 关键词能命中，但返回的是另一道题 → 不应张冠李戴
            return json.dumps(
                _index_page_payload([_problem_entry(999, 800, ["别的题"])])
            )

        monkeypatch.setattr(fetcher, "_fetch_text", fake_fetch_text)
        first = await fetcher._fetch_nowcoder_problem_metadata(["100", "255137"])
        assert "255137" not in first
        assert len(calls) == 1
        assert "255137" in fetcher._nowcoder_problem_absent

        # 第二次分析：负缓存生效，不再发起请求
        calls.clear()
        await fetcher._fetch_nowcoder_problem_metadata(["100", "255137"])
        assert calls == []

    asyncio.run(scenario())


def test_missing_problem_lookup_is_limited(monkeypatch, tmp_path):
    async def scenario():
        fetcher = AccountFetcher(problem_index_path=tmp_path / "idx.json")
        fetcher._nowcoder_problem_index = {"100": {"d": 1500, "t": []}}
        fetcher._nowcoder_problem_index_loaded_at = time.time()
        calls = 0

        async def fake_fetch_text(url, *, headers=None, retries=2, timeout=10.0):
            nonlocal calls
            calls += 1
            problem_id = parse_qs(urlparse(url).query)["keyword"][0]
            return json.dumps(
                _index_page_payload([_problem_entry(int(problem_id), 800, [])])
            )

        monkeypatch.setattr(fetcher, "_fetch_text", fake_fetch_text)
        problems = [str(5000 + i) for i in range(250)]
        metadata = await fetcher._fetch_nowcoder_problem_metadata(problems)
        assert calls == 100  # NOWCODER_PROBLEM_META_LOOKUP_LIMIT
        assert len(metadata) == 100

    asyncio.run(scenario())


def test_absent_problems_persist_with_index(tmp_path):
    path = tmp_path / "idx.json"
    fetcher = AccountFetcher(problem_index_path=path)
    fetcher._nowcoder_problem_index = {
        str(i): {"d": 800, "t": []} for i in range(6000)
    }
    fetcher._nowcoder_problem_index_loaded_at = time.time()
    fetcher._nowcoder_problem_absent = {"255137", "255222"}
    fetcher._nowcoder_problem_index_dirty = True
    fetcher.save_nowcoder_problem_index()

    other = AccountFetcher(problem_index_path=path)
    other.load_nowcoder_problem_index()
    assert other._nowcoder_problem_absent == {"255137", "255222"}


def test_flush_persists_index_without_sqlite_store(tmp_path):
    """没有 SQLite 后端时，flush 也要把题库索引（含负缓存）落盘。"""

    async def scenario():
        path = tmp_path / "idx.json"
        fetcher = AccountFetcher(problem_index_path=path)
        fetcher._nowcoder_problem_index = {
            str(i): {"d": 800, "t": []} for i in range(6000)
        }
        fetcher._nowcoder_problem_index_loaded_at = time.time()
        fetcher._nowcoder_problem_absent = {"255137"}
        fetcher._nowcoder_problem_index_dirty = True
        await fetcher.flush_persistent_cache()
        assert path.is_file()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["absent"] == ["255137"]
        assert fetcher._nowcoder_problem_index_dirty is False

    asyncio.run(scenario())
