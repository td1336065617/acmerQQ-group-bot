"""长文本自适应输出测试。"""
from __future__ import annotations

from pathlib import Path

from src.output_renderer import AdaptiveOutputRenderer, text_chunks


def test_short_text_stays_plain_text():
    renderer = AdaptiveOutputRenderer(max_chars=20, max_lines=3)
    assert renderer.needs_image("第一行\n第二行") is False
    assert renderer.needs_image("一\n二\n三\n四") is True
    assert renderer.needs_image("这是一段明显超过当前长度限制的文本内容，请转图片") is True


def test_configure_updates_thresholds():
    renderer = AdaptiveOutputRenderer(max_chars=20, max_lines=3)
    renderer.configure(max_chars=100, max_lines=5)
    assert renderer.max_chars == 100
    assert renderer.max_lines == 5
    assert renderer.needs_image("一\n二\n三\n四") is False
    assert renderer.needs_image("x" * 101) is True


def test_html_escapes_and_keeps_structure(tmp_path: Path):
    renderer = AdaptiveOutputRenderer(cache_dir=tmp_path)
    html = renderer._html_for_text(
        "📋 比赛列表\n1. <测试>\n   日期：2026-09-04\n📚 数据源：<官方>"
    )
    assert "&lt;测试&gt;" in html
    assert 'class="line item"' in html
    assert 'class="line detail"' in html
    assert 'class="line source"' in html
    assert "PINK PEARL" in html
    assert "#e467a5" in html


def test_render_reuses_same_content(tmp_path: Path, monkeypatch):
    renderer = AdaptiveOutputRenderer(cache_dir=tmp_path)
    calls = 0

    def fake_render(kind, executable, html_path, image_path, height):
        nonlocal calls
        calls += 1
        image_path.write_bytes(b"fake-png")
        return True

    monkeypatch.setattr(
        AdaptiveOutputRenderer,
        "_find_renderers",
        staticmethod(lambda: [("chromium", "fake-browser")]),
    )
    monkeypatch.setattr(
        AdaptiveOutputRenderer,
        "_run_external_renderer",
        staticmethod(fake_render),
    )

    first = renderer.render("📋 很长的比赛列表")
    second = renderer.render("📋 很长的比赛列表")

    assert first is not None and first.is_file()
    assert second == first
    assert calls == 1
    assert list(tmp_path.glob("response-*.html"))


def test_text_chunks_respect_limit():
    chunks = list(text_chunks("第一行\n" + "x" * 3200, max_chunk=1000))
    assert len(chunks) >= 4
    assert all(len(chunk) <= 1000 for chunk in chunks)
