"""跨平台知识点归一：把各平台的原始标签映射到一套规范标签。

为什么需要：牛客/洛谷的标签是中文、Codeforces 是英文、AtCoder 是题目系列，
不做归一就无法"按知识点轮换出题"或"按薄弱知识点推荐"。

实现文档：`docs/下一阶段功能实现文档.md` §A2.2。
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Sequence

#: 规范标签（抽题轮换与薄弱点统计都基于它）
CANONICAL_TAGS: tuple = (
    "模拟",
    "枚举",
    "贪心",
    "二分",
    "排序",
    "前缀和",
    "差分",
    "双指针",
    "动态规划",
    "背包",
    "图论",
    "最短路",
    "生成树",
    "连通性",
    "拓扑排序",
    "树",
    "数据结构",
    "并查集",
    "堆",
    "线段树",
    "字符串",
    "哈希",
    "数学",
    "数论",
    "组合数学",
    "概率期望",
    "几何",
    "计算几何",
    "搜索",
    "深度优先搜索",
    "广度优先搜索",
    "分治",
    "位运算",
    "博弈",
    "构造",
)

#: 牛客题库标签 → 规范标签（未收录的原样保留，但不参与轮换）
NOWCODER_TAG_ALIASES: Dict[str, str] = {
    "暴力": "枚举",
    "模拟法": "模拟",
    "模拟题": "模拟",
    "动态规划DP": "动态规划",
    "dp": "动态规划",
    "贪心算法": "贪心",
    "二分查找": "二分",
    "二分答案": "二分",
    "图论基础": "图论",
    "最短路算法": "最短路",
    "最小生成树": "生成树",
    "并查集（DSU）": "并查集",
    "线段树/树状数组": "线段树",
    "字符串匹配": "字符串",
    "数论基础": "数论",
    "组合": "组合数学",
    "概率与期望": "概率期望",
    "计算几何": "计算几何",
    "DFS": "深度优先搜索",
    "BFS": "广度优先搜索",
    "位运算技巧": "位运算",
    "构造题": "构造",
    "思维": "构造",
}

#: Codeforces 官方 tag → 规范标签
CODEFORCES_TAG_ALIASES: Dict[str, str] = {
    "brute force": "枚举",
    "implementation": "模拟",
    "greedy": "贪心",
    "binary search": "二分",
    "sortings": "排序",
    "two pointers": "双指针",
    "dp": "动态规划",
    "knapsack": "背包",
    "graphs": "图论",
    "shortest paths": "最短路",
    "spanning trees": "生成树",
    "dsu": "并查集",
    "dfs and similar": "深度优先搜索",
    "bfs": "广度优先搜索",
    "trees": "树",
    "data structures": "数据结构",
    "heaps": "堆",
    "segment tree": "线段树",
    "strings": "字符串",
    "hashing": "哈希",
    "math": "数学",
    "number theory": "数论",
    "combinatorics": "组合数学",
    "probabilities": "概率期望",
    "geometry": "几何",
    "bitmasks": "位运算",
    "games": "博弈",
    "constructive algorithms": "构造",
    "divide and conquer": "分治",
    "ternary search": "二分",
    "meet-in-the-middle": "枚举",
    "flows": "图论",
    "graph matchings": "图论",
    "matrices": "数学",
    "expression parsing": "字符串",
    "chinese remainder theorem": "数论",
    "fft": "数学",
    "schedules": "贪心",
    "interactive": "模拟",
    "2-sat": "图论",
    "shortest path": "最短路",
}

#: 洛谷标签 → 规范标签（洛谷标签名与规范标签高度重合，只需少量别名）
LUOGU_TAG_ALIASES: Dict[str, str] = {
    "动态规划,dp": "动态规划",
    "动态规划": "动态规划",
    "图论": "图论",
    "字符串": "字符串",
    "数学": "数学",
    "模拟": "模拟",
    "枚举": "枚举",
    "贪心": "贪心",
    "二分": "二分",
    "排序": "排序",
    "搜索": "搜索",
    "递推": "动态规划",
    "分治": "分治",
    "位运算": "位运算",
    "数据结构": "数据结构",
    "最短路": "最短路",
    "生成树": "生成树",
    "连通性": "连通性",
    "拓扑排序": "拓扑排序",
    "树形数据结构": "树",
    "并查集": "并查集",
    "堆": "堆",
    "线段树": "线段树",
    "哈希": "哈希",
    "数论": "数论",
    "组合数学": "组合数学",
    "概率论": "概率期望",
    "计算几何": "计算几何",
    "博弈论": "博弈",
    "构造": "构造",
}

#: AtCoder：题目系列不是知识点，仅保留可判定的几类
ATCODER_TAG_ALIASES: Dict[str, str] = {
    "abc": "模拟",
    "arc": "构造",
    "agc": "构造",
    "ahc": "模拟",
}

_PLATFORM_ALIASES: Dict[str, Dict[str, str]] = {
    "nowcoder": NOWCODER_TAG_ALIASES,
    "codeforces": CODEFORCES_TAG_ALIASES,
    "luogu": LUOGU_TAG_ALIASES,
    "atcoder": ATCODER_TAG_ALIASES,
}

#: 规范标签的小写索引，便于直接命中原生标签（如牛客的"贪心"）
_CANONICAL_INDEX = {tag.casefold(): tag for tag in CANONICAL_TAGS}


def canonical_tag(platform: str, raw: object) -> str:
    """把单个原始标签转成规范标签；无法识别时返回空串。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    aliases = _PLATFORM_ALIASES.get(str(platform), {})
    mapped = aliases.get(text) or aliases.get(text.casefold())
    if mapped:
        return mapped
    direct = _CANONICAL_INDEX.get(text.casefold())
    if direct:
        return direct
    return ""


def canonical_tags(platform: str, raw_tags: Iterable[object]) -> List[str]:
    """批量归一：去重、保序、丢弃无法识别的标签。"""
    out: List[str] = []
    seen: set = set()
    for raw in raw_tags or []:
        tag = canonical_tag(platform, raw)
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return out


def rotate_tag(weekday: int) -> str:
    """按星期几轮换的规范标签（同一天所有群一致，便于"今日一题"稳定）。"""
    index = int(weekday) % len(CANONICAL_TAGS)
    return CANONICAL_TAGS[index]


def canonical_tag_order() -> Sequence[str]:
    return CANONICAL_TAGS
