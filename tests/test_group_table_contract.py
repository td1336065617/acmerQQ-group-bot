"""群表格契约测试：行键、平台多选、重载清脏（BUG-014/018/019/038）。"""
from __future__ import annotations

from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "pages/settings/index.html"
TEXT = PAGE.read_text(encoding="utf-8")


def test_row_key_is_platform_scoped():
    assert "function rowKey(g)" in TEXT
    assert "return text(g.platform_id) + '|' + text(g.group_id);" in TEXT
    # 不再用复合键拼 CSS 选择器（| 与引号会让选择器失效）
    assert ".g-enabled[data-gid=" not in TEXT
    assert "tr[data-gid=\"' + " not in TEXT
    assert "findGroupRow" in TEXT


def test_group_platform_is_editable_with_hint():
    assert "g-platform" in TEXT
    assert "与全局取交集" in TEXT
    assert "至少选 1 个" in TEXT


def test_reload_clears_dirty_flags():
    assert "clearDirty('settings')" in TEXT
    assert "clearDirty('groups')" in TEXT
    assert "本地未保存改动已放弃" in TEXT
