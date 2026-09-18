"""题目池与抽题/推荐服务（A2）。

数据来源（实现文档 §A2.4）：
- 牛客：复用 1.12.0 落地的题库索引（内存 + 落盘，零新增抓取）；
- Codeforces：`problemset.problems`（1 次请求，含 tags，部分新题无 rating）；
- AtCoder：kenkooo `problem-models.json`（估计难度）+ `problems.json`（标题）；
- 洛谷：`problem/list?_contentOnly=1` 分页 + `/_lfe/tags` 标签字典。

设计要点：
- **确定性抽题**：候选集内用 `sha1(群ID:日期[:额外种子])` 取模，不依赖随机数，
  保证"早报里的今日一题"与"`每日一题` 指令"返回同一题，且可复现；
- **难度统一刻度**：CF rating 与 AtCoder 估计难度直接可比；洛谷 1~8 档按区间映射；
  牛客索引本身就是该刻度；
- 索引缺失/未构建时**降级为不抽题**（早报不加行、指令给友好提示），不阻塞主流程。
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set
from urllib.parse import urlencode

from astrbot.api import logger

from .problem_tags import canonical_tags, rotate_tag
from .utils import LENTILLE_RE, fetch_text_with_retry

#: 各平台题目索引的落盘 TTL（题库变化很慢）
PROBLEM_INDEX_TTL = 7 * 24 * 3600
#: 索引条目过少视为抓取残缺，直接忽略
PROBLEM_INDEX_MIN_ENTRIES = 500
#: 单次抽题排除的"已通过题"上限（防止大群并集过大）
EXCLUDE_LIMIT = 5000
#: CF problemset 实测 297KB，最慢一次 28.7 秒
CF_PROBLEMSET_TIMEOUT = 30.0
#: 洛谷题库分页（50 条/页）
LUOGU_PROBLEM_PAGE_SIZE = 50
#: 洛谷全量约 351 页；按难度分档抓取时只取需要的档位
LUOGU_MAX_PAGES = 400

CF_PROBLEMSET_URL = "https://codeforces.com/api/problemset.problems"
ATCODER_PROBLEMS_URL = "https://kenkoooo.com/atcoder/resources/problems.json"
ATCODER_MODELS_URL = "https://kenkoooo.com/atcoder/resources/problem-models.json"
LUOGU_PROBLEM_LIST_URL = "https://www.luogu.com.cn/problem/list"
LUOGU_TAGS_URL = "https://www.luogu.com.cn/_lfe/tags"

#: 难度档（牛客刻度）：与 src/account_fetcher.py 的展示分档保持一致
DIFFICULTY_BANDS = (
    (None, 599),
    (600, 999),
    (1000, 1399),
    (1400, 1799),
    (1800, 2199),
    (2200, 2599),
    (2600, None),
)

#: Rating → 难度档序号（取"该 Rating 对应的题目难度"）
RATING_TO_BAND = (
    (900, 0),
    (1100, 1),
    (1300, 2),
    (1600, 3),
    (1900, 4),
    (2200, 5),
    (10**9, 6),
)

#: 洛谷难度 1~8 → 牛客刻度
LUOGU_DIFFICULTY_TO_SCORE = {
    1: 500,
    2: 800,
    3: 1000,
    4: 1300,
    5: 1600,
    6: 2000,
    7: 2400,
    8: 2900,
}


@dataclass
class Problem:
    """统一题目模型。difficulty 为 None 表示未评定（不参与抽题）。"""

    platform: str
    problem_id: str
    title: str
    difficulty: Optional[int]
    tags: List[str] = field(default_factory=list)
    url: str = ""

    def display(self) -> str:
        """对外展示文案：**只报题目名**（难度与知识点用于选题目，不展示）。"""
        return str(self.title or "").strip() or f"题目 {self.problem_id}"


def band_of_difficulty(difficulty: Optional[int]) -> Optional[int]:
    if difficulty is None:
        return None
    for index, (low, high) in enumerate(DIFFICULTY_BANDS):
        if (low is None or difficulty >= low) and (high is None or difficulty <= high):
            return index
    return None


def band_of_rating(rating: Optional[int]) -> int:
    """平台 Rating → 建议难度档。"""
    if rating is None:
        return 2
    for threshold, band in RATING_TO_BAND:
        if rating <= threshold:
            return band
    return 6


def band_range(difficulty: Optional[int]) -> tuple:
    index = band_of_difficulty(difficulty)
    if index is None:
        return (None, None)
    return DIFFICULTY_BANDS[index]


class ProblemService:
    """题目池管理 + 抽题/推荐；由插件持有一个实例。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self._index_cache: Dict[str, tuple] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # 题目池
    # ------------------------------------------------------------------
    def nowcoder_pool(self) -> List[Problem]:
        """牛客题库索引 → Problem 列表（本地查表，零请求）。"""
        fetcher = getattr(self.plugin, "account_fetcher", None)
        if fetcher is None or not fetcher.nowcoder_problem_index_ready():
            return []
        pool: List[Problem] = []
        for problem_id, entry in fetcher._nowcoder_problem_index.items():
            difficulty = entry.get("d")
            raw_tags = entry.get("t") or []
            # 索引里存了题目名（"n"）；旧索引没有该字段时退回题号
            title = str(entry.get("n") or "").strip() or f"牛客题目 #{problem_id}"
            pool.append(
                Problem(
                    platform="nowcoder",
                    problem_id=str(problem_id),
                    title=title,
                    difficulty=difficulty if isinstance(difficulty, int) else None,
                    tags=canonical_tags("nowcoder", raw_tags),
                    url=f"https://ac.nowcoder.com/acm/problem/{problem_id}",
                )
            )
        return pool

    def index_path(self, platform: str) -> Path:
        fetcher = getattr(self.plugin, "account_fetcher", None)
        if fetcher is not None and hasattr(fetcher, "index_path"):
            base = fetcher.index_path().parent
        else:  # pragma: no cover - 兼容未初始化场景
            base = Path("data")
        return base / f"problem_index_{platform}.json"

    def _load_disk_index(self, platform: str) -> List[Problem]:
        cached = self._index_cache.get(platform)
        if cached is not None and time.time() - cached[0] < PROBLEM_INDEX_TTL:
            return cached[1]
        path = self.index_path(platform)
        try:
            if not path.is_file():
                return []
            with path.open("r", encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("读取 %s 题目索引失败：%s", platform, exc)
            return []
        items = payload.get("problems") if isinstance(payload, dict) else None
        if not isinstance(items, list) or len(items) < PROBLEM_INDEX_MIN_ENTRIES:
            return []
        fetched_at = payload.get("fetched_at")
        try:
            fetched_at = float(fetched_at)
        except (TypeError, ValueError):
            fetched_at = 0.0
        if fetched_at and time.time() - fetched_at > PROBLEM_INDEX_TTL:
            return []
        problems = [
            Problem(
                platform=platform,
                problem_id=str(item.get("id") or ""),
                title=str(item.get("title") or ""),
                difficulty=item.get("difficulty"),
                tags=list(item.get("tags") or []),
                url=str(item.get("url") or ""),
            )
            for item in items
            if isinstance(item, dict) and item.get("id")
        ]
        self._index_cache[platform] = (fetched_at or time.time(), problems)
        return problems

    def _save_disk_index(self, platform: str, problems: Sequence[Problem]) -> None:
        path = self.index_path(platform)
        payload = {
            "version": 1,
            "fetched_at": time.time(),
            "problems": [
                {
                    "id": problem.problem_id,
                    "title": problem.title,
                    "difficulty": problem.difficulty,
                    "tags": problem.tags,
                    "url": problem.url,
                }
                for problem in problems
            ],
        }
        temp_path = path.with_name(f".{path.name}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with temp_path.open("w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, separators=(",", ":"))
            os.replace(temp_path, path)
            self._index_cache[platform] = (time.time(), list(problems))
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("写入 %s 题目索引失败：%s", platform, exc)
            try:
                temp_path.unlink()
            except OSError:
                pass

    async def ensure_index(self, platform: str) -> List[Problem]:
        """取（必要时构建）某平台题目池；失败返回空列表。"""
        if platform == "nowcoder":
            return self.nowcoder_pool()
        problems = self._load_disk_index(platform)
        if problems:
            return problems
        lock = self._locks.get(platform)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[platform] = lock
        async with lock:
            problems = self._load_disk_index(platform)
            if problems:
                return problems
            try:
                if platform == "codeforces":
                    problems = await self._build_codeforces_index()
                elif platform == "atcoder":
                    problems = await self._build_atcoder_index()
                elif platform == "luogu":
                    problems = await self._build_luogu_index()
                else:
                    return []
            except Exception as exc:  # noqa: BLE001 - 构建失败不影响主流程
                logger.warning("构建 %s 题目索引失败：%s", platform, exc)
                return []
            if len(problems) < PROBLEM_INDEX_MIN_ENTRIES:
                logger.warning(
                    "%s 题目索引条目过少（%d），放弃本次结果",
                    platform,
                    len(problems),
                )
                return []
            self._save_disk_index(platform, problems)
            logger.info("%s 题目索引已更新：%d 题", platform, len(problems))
            return problems

    async def _build_codeforces_index(self) -> List[Problem]:
        fetcher = self.plugin.account_fetcher
        payload = await fetcher._cf_json(
            "problemset.problems", {}, timeout=CF_PROBLEMSET_TIMEOUT
        )
        if not isinstance(payload, dict) or payload.get("status") != "OK":
            raise ValueError("CF problemset 返回异常")
        result = payload.get("result") or {}
        problems: List[Problem] = []
        for item in result.get("problems") or []:
            if not isinstance(item, dict):
                continue
            contest_id = item.get("contestId")
            index = str(item.get("index") or "").strip()
            if not contest_id or not index:
                continue
            rating = item.get("rating")
            problems.append(
                Problem(
                    platform="codeforces",
                    problem_id=f"{contest_id}{index}",
                    title=f"{index}. {str(item.get('name') or '').strip()}",
                    difficulty=int(rating) if isinstance(rating, int) else None,
                    tags=canonical_tags("codeforces", item.get("tags") or []),
                    url=f"https://codeforces.com/contest/{contest_id}/problem/{index}",
                )
            )
        return problems

    async def _build_atcoder_index(self) -> List[Problem]:
        fetcher = self.plugin.account_fetcher
        problems_payload = await fetcher._fetch_json(
            ATCODER_PROBLEMS_URL, timeout=30.0
        )
        models = await fetcher._fetch_json(ATCODER_MODELS_URL, timeout=30.0)
        if not isinstance(problems_payload, list):
            raise ValueError("AtCoder problems 返回异常")
        models = models if isinstance(models, dict) else {}
        problems: List[Problem] = []
        for item in problems_payload:
            if not isinstance(item, dict):
                continue
            problem_id = str(item.get("id") or "").strip()
            if not problem_id:
                continue
            model = models.get(problem_id) or {}
            raw_difficulty = model.get("difficulty") if isinstance(model, dict) else None
            difficulty = (
                int(raw_difficulty)
                if isinstance(raw_difficulty, (int, float)) and raw_difficulty
                else None
            )
            contest_id = str(item.get("contest_id") or "")
            problems.append(
                Problem(
                    platform="atcoder",
                    problem_id=problem_id,
                    title=str(item.get("title") or problem_id),
                    difficulty=difficulty,
                    tags=canonical_tags(
                        "atcoder", [contest_id.split("_")[0] if contest_id else ""]
                    ),
                    url=f"https://atcoder.jp/contests/{contest_id}/tasks/{problem_id}",
                )
            )
        return problems

    async def _build_luogu_index(self, difficulties: Iterable[int] = ()) -> List[Problem]:
        """洛谷题库：分页抓取 `lentille-context`；可按难度档收窄（1~8）。"""
        fetcher = self.plugin.account_fetcher
        tag_names = await self._luogu_tag_names()
        wanted = [str(item) for item in difficulties if item]
        problems: List[Problem] = []
        seen: Set[str] = set()
        for page in range(1, LUOGU_MAX_PAGES + 1):
            params = {"_contentOnly": "1", "page": str(page)}
            if wanted:
                params["difficulty"] = ",".join(wanted)
            url = f"{LUOGU_PROBLEM_LIST_URL}?{urlencode(params)}"
            text = await fetch_text_with_retry(
                fetcher.session,
                url,
                headers={"x-luogu-type": "content-only"},
                timeout=20.0,
            )
            match = LENTILLE_RE.search(text)
            if not match:
                break
            payload = json.loads(html.unescape(match.group(1)))
            listing = ((payload.get("data") or {}).get("problems")) or {}
            rows = listing.get("result") or []
            if not rows:
                break
            for item in rows:
                if not isinstance(item, dict):
                    continue
                pid = str(item.get("pid") or "").strip()
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                raw_tags = [
                    tag_names.get(int(tag_id), "")
                    for tag_id in (item.get("tags") or [])
                    if isinstance(tag_id, int)
                ]
                level = item.get("difficulty")
                problems.append(
                    Problem(
                        platform="luogu",
                        problem_id=pid,
                        title=f"{pid} {str(item.get('title') or '').strip()}".strip(),
                        difficulty=LUOGU_DIFFICULTY_TO_SCORE.get(
                            level if isinstance(level, int) else -1
                        ),
                        tags=canonical_tags("luogu", raw_tags),
                        url=f"https://www.luogu.com.cn/problem/{pid}",
                    )
                )
            per_page = int(listing.get("perPage") or LUOGU_PROBLEM_PAGE_SIZE)
            if len(rows) < per_page:
                break
        return problems

    async def _luogu_tag_names(self) -> Dict[int, str]:
        fetcher = self.plugin.account_fetcher
        try:
            payload = await fetcher._fetch_json(LUOGU_TAGS_URL, timeout=20.0)
        except Exception as exc:  # noqa: BLE001 - 标签字典失败只影响知识点
            logger.warning("读取洛谷标签字典失败：%s", exc)
            return {}
        tags = (payload or {}).get("tags") if isinstance(payload, dict) else None
        if not isinstance(tags, list):
            return {}
        names: Dict[int, str] = {}
        for item in tags:
            if not isinstance(item, dict):
                continue
            tag_id = item.get("id")
            name = str(item.get("name") or "").strip()
            if isinstance(tag_id, int) and name:
                names[tag_id] = name
        return names

    # ------------------------------------------------------------------
    # 抽题与推荐
    # ------------------------------------------------------------------
    @staticmethod
    def _exclude_ids(solved_sets: Iterable[Iterable[str]]) -> Set[str]:
        exclude: Set[str] = set()
        for items in solved_sets:
            for item in items or []:
                exclude.add(str(item))
                if len(exclude) >= EXCLUDE_LIMIT:
                    return exclude
        return exclude

    def pick_daily(
        self,
        *,
        group_id: str,
        day: str,
        pool: Sequence[Problem],
        rating: Optional[int] = None,
        exclude: Optional[Set[str]] = None,
        seed_extra: str = "",
    ) -> Optional[Problem]:
        """确定性抽题：同一 (群, 日期, 种子) 永远得到同一题。"""
        if not pool:
            return None
        band = band_of_rating(rating)
        low, high = DIFFICULTY_BANDS[max(0, band - 1)]
        _, high_next = DIFFICULTY_BANDS[min(len(DIFFICULTY_BANDS) - 1, band + 1)]
        low = 0 if low is None else low
        high = 10**9 if high_next is None else high_next
        excluded = exclude or set()
        try:
            weekday = date.fromisoformat(day).weekday()
        except ValueError:
            weekday = 0
        tag = rotate_tag(weekday)

        def in_range(problem: Problem) -> bool:
            return (
                problem.difficulty is not None
                and low <= problem.difficulty <= high
                and problem.problem_id not in excluded
            )

        candidates = [p for p in pool if in_range(p) and tag in p.tags]
        if not candidates:
            candidates = [p for p in pool if in_range(p)]
        if not candidates:
            return None
        candidates.sort(key=lambda p: (p.difficulty or 0, p.problem_id))
        digest = hashlib.sha1(
            f"{group_id}:{day}:{seed_extra}".encode("utf-8")
        ).hexdigest()
        return candidates[int(digest[:8], 16) % len(candidates)]

    def recommend(
        self,
        *,
        pool: Sequence[Problem],
        solved: Iterable[str],
        weak_tags: Sequence[str],
        rating: Optional[int] = None,
        limit: int = 3,
    ) -> List[Problem]:
        """按"未通过 + 难度贴合 + 命中薄弱知识点"推荐题目。"""
        if not pool:
            return []
        solved_set = {str(item) for item in solved or []}
        band = band_of_rating(rating)
        low, _ = DIFFICULTY_BANDS[max(0, band - 1)]
        _, high = DIFFICULTY_BANDS[min(len(DIFFICULTY_BANDS) - 1, band + 1)]
        low = 0 if low is None else low
        high = 10**9 if high is None else high
        weak = [tag for tag in weak_tags if tag]

        def matches(problem: Problem, tags: Sequence[str]) -> bool:
            return bool(tags) and any(tag in problem.tags for tag in tags)

        primary = [
            problem
            for problem in pool
            if problem.difficulty is not None
            and low <= problem.difficulty <= high
            and problem.problem_id not in solved_set
            and matches(problem, weak)
        ]
        if len(primary) < limit:
            # 放宽知识点，但仍要求未通过 + 难度贴合
            extra = [
                problem
                for problem in pool
                if problem.difficulty is not None
                and low <= problem.difficulty <= high
                and problem.problem_id not in solved_set
                and problem not in primary
            ]
            primary.extend(extra)
        primary.sort(
            key=lambda problem: (
                -sum(1 for tag in weak if tag in problem.tags),
                problem.difficulty or 0,
                problem.problem_id,
            )
        )
        return primary[: max(1, limit)]


def weak_tags_from_analysis(analysis: Dict[str, Any], limit: int = 3) -> List[str]:
    """从分析结果的 category_distribution 里挑"通过数最少"的知识点。"""
    rows = (analysis or {}).get("category_distribution") or []
    counts = [
        (str(row.get("label") or ""), int(row.get("count") or 0))
        for row in rows
        if isinstance(row, dict) and row.get("label")
    ]
    counts.sort(key=lambda item: (item[1], item[0]))
    return [label for label, _ in counts[: max(1, limit)]]
