"""离线单测：模型与工具函数。"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.models import Contest, GroupConfig, OfflineContest
from src.utils import (
    is_contest_in_recent_window,
    normalize_command,
    validate_hhmm,
)


def test_validate_hhmm():
    assert validate_hhmm("08:00") == "08:00"
    assert validate_hhmm("23:59") == "23:59"
    with pytest.raises(ValueError):
        validate_hhmm("8:00")
    with pytest.raises(ValueError):
        validate_hhmm("24:00")


def test_normalize_command_casefolds_english_only():
    assert normalize_command(" XCPC线下赛 ") == "xcpc线下赛"
    assert normalize_command("AtCoder比赛") == "atcoder比赛"
    assert normalize_command("线下赛") == "线下赛"


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


def test_recent_window_includes_upcoming_and_running_contests():
    now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    upcoming = Contest(
        platform="codeforces",
        name="未来比赛",
        start_time=datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc),
        duration_minutes=120,
    )
    outside = Contest(
        platform="codeforces",
        name="范围外比赛",
        start_time=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
    )
    running = Contest(
        platform="atcoder",
        name="进行中比赛",
        start_time=datetime(2026, 9, 4, 11, 0, tzinfo=timezone.utc),
        duration_minutes=120,
    )
    ended = Contest(
        platform="atcoder",
        name="已结束比赛",
        start_time=datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc),
        duration_minutes=60,
    )

    assert is_contest_in_recent_window(upcoming, now, 7) is True
    assert is_contest_in_recent_window(outside, now, 7) is False
    assert is_contest_in_recent_window(running, now, 7) is True
    assert is_contest_in_recent_window(ended, now, 7) is False


def test_recent_window_supports_date_only_offline_contests():
    now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    within = OfflineContest(
        name="近期线下赛",
        start_date=date(2026, 9, 10),
        source_url="https://www.xcpc.link/",
    )
    outside = OfflineContest(
        name="较远线下赛",
        start_date=date(2026, 9, 12),
        source_url="https://www.xcpc.link/",
    )
    assert is_contest_in_recent_window(within, now, 7) is True
    assert is_contest_in_recent_window(outside, now, 7) is False
