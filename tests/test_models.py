"""离线单测：模型与工具函数。"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.models import Contest, GroupConfig, OfflineContest
from src.utils import validate_hhmm


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


def test_offline_contest_format():
    contest = OfflineContest(
        name="CCPC 广州站",
        start_date=date(2026, 10, 3),
        end_date=date(2026, 10, 4),
        venue="广州",
        organizer="某大学",
        official_url="https://example.com/notice",
        source_url="https://www.xcpc.ink/",
    )
    text = contest.format_detail()
    assert "2026-10-03 至 2026-10-04" in text
    assert "赛站/地点：广州" in text
    assert "官方通知：https://example.com/notice" in text
    assert "数据源：XCPC Link（https://www.xcpc.ink/）" in text
