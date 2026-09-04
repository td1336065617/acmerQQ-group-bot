"""离线单测：模型与工具函数。"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.announcement_fetcher import parse_icpc_pku_announcements
from src.models import Announcement, Contest, GroupConfig
from src.utils import validate_hhmm

ICPC_LIST_HTML = """
<html><body>
<ul>
  <li><a href=../jj/index.htm>简介</a></li>
  <li><a href=../hz/index.htm>赞助商</a></li>
</ul>
<div class="article">
<ul class="item-list">
<li><a href="b4d55e13589b47d384b6c854702acc86.htm">第51届ICPC 亚洲区域赛（南昌）名额分配方案<span class="s-dt">[2026-09-03]</span></a></li>
<li><a href="c08d46515b284cf5bfd1f26cfa66a8a8.htm">2026 ICPC Asia EC网络预选赛第二场报名公示<span class="s-dt">[2026-09-02]</span></a></li>
<li><a href="f6f3c75549134b46b66bbb20f5e34eea.htm">WF Teams &amp; Coaches<span class="s-dt">[2026-06-18]</span></a></li>
</ul>
</div>
</body></html>
"""


def test_validate_hhmm():
    assert validate_hhmm("08:00") == "08:00"
    assert validate_hhmm("23:59") == "23:59"
    with pytest.raises(ValueError):
        validate_hhmm("8:00")
    with pytest.raises(ValueError):
        validate_hhmm("24:00")


def test_contest_format():
    c = Contest(
        platform="codeforces",
        name="Codeforces Round 1117 (Div. 2)",
        start_time=datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc),
        duration_minutes=120,
        url="https://codeforces.com/contest/2257",
        contest_id="2257",
    )
    text = c.format_detail()
    assert "Codeforces 最近比赛" in text
    assert "20:00" in text  # UTC+8 展示
    assert "120 分钟" in text
    assert "https://codeforces.com/contest/2257" in text


def test_group_config_defaults():
    g = GroupConfig(group_id="g1")
    assert g.enabled is True
    assert g.morning_push_time == "08:00"
    assert "nowcoder" in g.push_platforms
    assert g.reminder_enabled is True
    assert g.announcement_enabled is True


def test_parse_icpc_pku_announcements():
    items = parse_icpc_pku_announcements(
        ICPC_LIST_HTML, "https://icpc.pku.edu.cn/tzgg/"
    )
    # 导航菜单里的 <li> 不能被当成公告
    assert len(items) == 3
    first = items[0]
    assert first.title == "第51届ICPC 亚洲区域赛（南昌）名额分配方案"
    assert first.published == date(2026, 9, 3)
    assert first.announcement_id == "b4d55e13589b47d384b6c854702acc86"
    assert first.url == (
        "https://icpc.pku.edu.cn/tzgg/b4d55e13589b47d384b6c854702acc86.htm"
    )
    # 日期 span 不能混进标题，HTML 实体要还原
    assert "s-dt" not in first.title
    assert items[2].title == "WF Teams & Coaches"


def test_parse_icpc_pku_announcements_bad_html():
    with pytest.raises(ValueError):
        parse_icpc_pku_announcements("<html><body>no list</body></html>")


def test_announcement_format():
    a = Announcement(
        source="icpc_pku",
        title="2026 ICPC Asia EC网络预选赛报名通知",
        url="https://icpc.pku.edu.cn/tzgg/x.htm",
        published=date(2026, 8, 10),
        announcement_id="x",
    )
    assert a.format_line() == (
        "[2026-08-10] 2026 ICPC Asia EC网络预选赛报名通知"
    )
    text = a.format_detail("📢 ICPC北京总部 新公告")
    assert "📢 ICPC北京总部 新公告" in text
    assert "2026-08-10" in text
    assert "https://icpc.pku.edu.cn/tzgg/x.htm" in text
    # 缺少日期时不应报错
    assert Announcement(title="t").published_text() == "日期未知"
