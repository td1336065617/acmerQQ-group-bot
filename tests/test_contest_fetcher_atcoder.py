"""AtCoder 解析：空结果不再静默缓存（BUG-048）。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.contest_fetcher as cf

GOOD_HTML = """
<div id="contest-table-upcoming">
<tr><td><a href="/contests/abc999">AtCoder Beginner Contest 999</a></td>
<td class="text-center">01:40</td>
<td><time>2026-10-03 21:00:00+0900</time></td></tr>
</div>
"""

BAD_HTML = """
<div id="contest-table-upcoming">
<tr><td><a href="/contests/abc999">AtCoder Beginner Contest 999</a></td>
<td class="text-center">01:40</td>
<td><time>2026/10/03 21:00 JST</time></td></tr>
</div>
"""


def _fetcher(html: str):
    fetcher = cf.ContestFetcher(cache_ttl=300, offline_cache_ttl=1800)
    fetcher.session = object()          # 只为通过「session 未初始化」检查

    async def fake_fetch(session, url, **kwargs):
        return html

    cf.fetch_text_with_retry = fake_fetch
    return fetcher


def test_atcoder_parses_time_with_offset():
    fetcher = _fetcher(GOOD_HTML)
    contests = asyncio.run(fetcher._fetch_atcoder())
    assert len(contests) == 1
    assert contests[0].duration_minutes == 100
    assert contests[0].start_time.hour == 12      # 21:00 JST → 12:00 UTC


def test_atcoder_empty_parse_raises():
    """页面有行但解析不出时间时必须报错（而不是把空列表缓存整个 TTL）。"""
    fetcher = _fetcher(BAD_HTML)
    raised = False
    try:
        asyncio.run(fetcher._fetch_atcoder())
    except ValueError as exc:
        raised = "解析为空" in str(exc)
    assert raised
