"""群排行卡显示平台内排名（rating_rank）的回归测试。

覆盖：快照表幂等迁移、快照写入/读回、HTML 两处排行卡、Pillow 两处兜底路径。
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

from src.account_cards import (
    CARD_FORMAT_VERSION,
    AccountCardRenderer,
    _member_account_line,
)
from src.account_store import AccountStore


def _row(
    user_id: str = "u1",
    handle: str = "zhangsan",
    rating_rank: int | None = 48,
) -> dict:
    return {
        "user_id": user_id,
        "handle": handle,
        "display_name": "张三",
        "value": 1800,
        "display_value": "1800",
        "metric_label": "Rating",
        "current_metric_label": "Rating",
        "sort_value": 1800,
        "delta": 20,
        "rating": 1800,
        "rating_rank": rating_rank,
        "current_display_value": "1800",
    }


# ----------------------------------------------------------------------
# 快照表迁移
# ----------------------------------------------------------------------

_LEGACY_SNAPSHOT_SQL = """
CREATE TABLE rank_snapshot (
    group_id      TEXT NOT NULL,
    platform      TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    handle        TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    metric_label  TEXT NOT NULL,
    display_value TEXT NOT NULL,
    sort_value    INTEGER NOT NULL,
    delta         INTEGER,
    current_metric_label TEXT NOT NULL DEFAULT '',
    current_display_value TEXT NOT NULL DEFAULT '',
    rating        INTEGER,
    updated_at    REAL NOT NULL,
    PRIMARY KEY (group_id, platform, user_id)
);
CREATE TABLE progress_snapshot (
    group_id      TEXT NOT NULL,
    platform      TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    handle        TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    metric_label  TEXT NOT NULL,
    display_value TEXT NOT NULL,
    sort_value    INTEGER NOT NULL,
    delta         INTEGER,
    current_metric_label TEXT NOT NULL DEFAULT '',
    current_display_value TEXT NOT NULL DEFAULT '',
    rating        INTEGER,
    updated_at    REAL NOT NULL,
    PRIMARY KEY (group_id, platform, user_id)
);
"""


def _columns(db_path, table: str) -> set:
    conn = sqlite3.connect(str(db_path))
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def test_initialize_migrates_legacy_db_twice(tmp_path):
    """线上旧库缺 rating_rank：连续 initialize 两次不报错、列补上、旧数据保留。"""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_LEGACY_SNAPSHOT_SQL)
        conn.execute(
            "INSERT INTO rank_snapshot("
            "group_id, platform, user_id, handle, display_name, metric_label,"
            "display_value, sort_value, delta, updated_at"
            ") VALUES ('g1', 'codeforces', 'u1', 'alice', 'Alice', 'Rating',"
            "'1700', 1700, 10, ?)",
            (time.time(),),
        )
        conn.commit()
    finally:
        conn.close()

    assert "rating_rank" not in _columns(db_path, "rank_snapshot")
    assert "rating_rank" not in _columns(db_path, "progress_snapshot")

    store = AccountStore(db_path)
    asyncio.run(store.initialize())
    asyncio.run(store.initialize())

    assert "rating_rank" in _columns(db_path, "rank_snapshot")
    assert "rating_rank" in _columns(db_path, "progress_snapshot")
    # 迁移不丢旧数据，旧行的新列为 NULL
    rows = asyncio.run(store.get_rank_rows("g1", "codeforces"))
    assert len(rows) == 1
    assert rows[0]["handle"] == "alice"
    assert rows[0]["rating_rank"] is None


# ----------------------------------------------------------------------
# 快照写入 / 读回
# ----------------------------------------------------------------------


def test_rank_snapshot_roundtrips_rating_rank(tmp_path):
    async def scenario():
        store = AccountStore(tmp_path / "rank.db")
        await store.initialize()
        rows = [_row("u1", "zhangsan", 48), _row("u2", "lisi", None)]
        await store.replace_rank_snapshot("g1", "codeforces", rows)
        got = {
            row["user_id"]: row["rating_rank"]
            for row in await store.get_rank_rows("g1", "codeforces")
        }
        assert got == {"u1": 48, "u2": None}

        # progress_snapshot 同样落盘
        await store.replace_rank_snapshot(
            "g1", "codeforces", rows, mode="progress"
        )
        got_progress = {
            row["user_id"]: row["rating_rank"]
            for row in await store.get_rank_rows(
                "g1", "codeforces", mode="progress"
            )
        }
        assert got_progress == {"u1": 48, "u2": None}

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# HTML 路径
# ----------------------------------------------------------------------


def test_ranking_html_contains_platform_rank():
    html = AccountCardRenderer._ranking_html(
        [_row()],
        title="排行",
        subtitle="测试",
        metric_label="Rating",
        note="",
    )
    assert "平台排名 #48" in html
    assert "zhangsan · 平台排名 #48" in html


def test_overview_html_contains_platform_rank():
    html = AccountCardRenderer._overview_html(
        {"codeforces": [_row()]},
        title="总览",
        subtitle="测试",
        metric_label="Rating",
        note="",
    )
    assert "zhangsan · 平台排名 #48" in html


def test_ranking_html_omits_missing_platform_rank():
    html = AccountCardRenderer._ranking_html(
        [_row(rating_rank=None)],
        title="排行",
        subtitle="测试",
        metric_label="Rating",
        note="",
    )
    assert "平台排名" not in html
    assert "#None" not in html
    assert _member_account_line(_row(rating_rank=None)) == "zhangsan"

    overview = AccountCardRenderer._overview_html(
        {"codeforces": [_row(rating_rank=None)]},
        title="总览",
        subtitle="测试",
        metric_label="Rating",
        note="",
    )
    assert "平台排名" not in overview
    assert "#None" not in overview


def test_card_format_version_bumped_for_rank_card():
    # 排行卡结构变化必须让旧缓存图失效
    assert CARD_FORMAT_VERSION >= 18


# ----------------------------------------------------------------------
# Pillow 兜底路径
# ----------------------------------------------------------------------


def _spy_draw_text(monkeypatch) -> list:
    from PIL import ImageDraw

    drawn: list = []
    original = ImageDraw.ImageDraw.text

    def spy(self, xy, text, *args, **kwargs):
        drawn.append(str(text))
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    return drawn


def test_pillow_ranking_draws_platform_rank(tmp_path, monkeypatch):
    drawn = _spy_draw_text(monkeypatch)
    out = tmp_path / "rank.png"
    ok = AccountCardRenderer._pillow_ranking(
        [_row()],
        "排行",
        "测试",
        "Rating",
        "",
        None,
        "近7日变化",
        "delta",
        out,
    )
    assert ok and out.is_file()
    assert "zhangsan · 平台排名 #48" in drawn


def test_pillow_ranking_omits_missing_platform_rank(tmp_path, monkeypatch):
    drawn = _spy_draw_text(monkeypatch)
    out = tmp_path / "rank-none.png"
    ok = AccountCardRenderer._pillow_ranking(
        [_row(rating_rank=None)],
        "排行",
        "测试",
        "Rating",
        "",
        None,
        "近7日变化",
        "delta",
        out,
    )
    assert ok and out.is_file()
    assert "平台排名" not in "".join(drawn)
    assert "#None" not in "".join(drawn)


def test_pillow_overview_draws_platform_rank(tmp_path, monkeypatch):
    drawn = _spy_draw_text(monkeypatch)
    renderer = AccountCardRenderer(cache_dir=tmp_path)
    out = tmp_path / "overview.png"
    ok = renderer._pillow_overview(
        {"codeforces": [_row()]},
        "总览",
        "测试",
        "Rating",
        "",
        "近7日变化",
        "delta",
        out,
    )
    assert ok and out.is_file()
    assert "zhangsan · 平台排名 #48" in drawn


# ----------------------------------------------------------------------
# 文字兜底
# ----------------------------------------------------------------------


def test_rank_text_fallback_includes_platform_rank():
    import sys
    from pathlib import Path

    tests_dir = str(Path(__file__).resolve().parent)
    sys.path.insert(0, tests_dir)
    try:
        from test_main_accounts import _load_main_module
    finally:
        sys.path.remove(tests_dir)
    main = _load_main_module()

    line = main._rank_fallback_row(1, _row(), "当前 Rating")
    assert "平台排名 #48" in line
    assert "#None" not in line

    missing = main._rank_fallback_row(1, _row(rating_rank=None), "当前 Rating")
    assert "平台排名" not in missing
    assert "#None" not in missing

