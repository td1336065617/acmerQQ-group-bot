"""长文本自适应输出：短内容发文字，长内容按需转成 PNG。"""
from __future__ import annotations

import hashlib
import html
import os
import re
import shutil
import subprocess
import threading
import unicodedata
from pathlib import Path
from typing import List, Optional, Tuple


# QQ 消息不宜发送过长的纯文本；同时限制行数，避免很多短行挤成一条长消息。
MAX_PLAIN_TEXT_CHARS = 1800
MAX_PLAIN_TEXT_LINES = 36
MAX_TEXT_CHUNK = 1500

RENDER_WIDTH = 1200
MIN_RENDER_HEIGHT = 420
MAX_RENDER_HEIGHT = 16000
RENDER_FORMAT_VERSION = 1

_ITEM_RE = re.compile(r"^\s*\d+[.、]\s*")


class AdaptiveOutputRenderer:
    """按输出长度决定文字/图片，并缓存相同内容的渲染结果。"""

    def __init__(
        self,
        cache_dir: Optional[str | Path] = None,
        *,
        max_chars: int = MAX_PLAIN_TEXT_CHARS,
        max_lines: int = MAX_PLAIN_TEXT_LINES,
    ) -> None:
        self.cache_dir = (
            Path(cache_dir)
            if cache_dir is not None
            else Path(__file__).resolve().parent.parent / "data" / "output_cache"
        ).expanduser().resolve()
        self.max_chars = max(1, int(max_chars))
        self.max_lines = max(1, int(max_lines))
        self._render_lock = threading.Lock()

    def needs_image(self, text: str) -> bool:
        value = str(text or "").strip()
        return (
            len(value) > self.max_chars
            or len(value.splitlines()) > self.max_lines
        )

    def render(self, text: str) -> Optional[Path]:
        """将文本写入 HTML 并转成 PNG；失败返回 None。"""
        value = str(text or "").strip()
        if not value:
            return None

        digest = hashlib.sha256(
            f"{RENDER_FORMAT_VERSION}\0{value}".encode("utf-8")
        ).hexdigest()[:24]
        html_path = self.cache_dir / f"response-{digest}.html"
        image_path = self.cache_dir / f"response-{digest}.png"
        html_tmp = self.cache_dir / f".response-{digest}.html.tmp"
        image_tmp = self.cache_dir / f".response-{digest}.png.tmp"

        # 同一进程内可能同时有多个群触发长列表，避免两个线程同时覆盖
        # 同一份临时文件，也避免重复启动浏览器。
        with self._render_lock:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                if image_path.is_file() and image_path.stat().st_size > 0:
                    return image_path
                html_tmp.write_text(
                    self._html_for_text(value), encoding="utf-8"
                )
                os.replace(html_tmp, html_path)
            except (OSError, UnicodeError):
                return None

            height = self._estimate_render_height(value)
            for kind, executable in self._find_renderers():
                image_tmp.unlink(missing_ok=True)
                if kind == "pillow":
                    success = self._render_with_pillow(value, image_tmp)
                else:
                    success = self._run_external_renderer(
                        kind, executable, html_path, image_tmp, height
                    )
                if success:
                    try:
                        os.replace(image_tmp, image_path)
                    except OSError:
                        return None
                    return image_path

            # Pillow 不一定被显式列入渲染器环境变量，最后再尝试一次，
            # 这样没有任何浏览器时仍能生成中文图片。
            image_tmp.unlink(missing_ok=True)
            if self._render_with_pillow(value, image_tmp):
                try:
                    os.replace(image_tmp, image_path)
                except OSError:
                    return None
                return image_path
            return None

    @staticmethod
    def _line_kind(line: str, index: int) -> str:
        stripped = line.strip()
        if index == 0:
            return "title"
        if not stripped:
            return "blank"
        if set(stripped) <= set("-_=─—–━") and len(stripped) >= 3:
            return "divider"
        if stripped.startswith(("👥", "🔑", "—")):
            return "section"
        if stripped.startswith("📚"):
            return "source"
        if _ITEM_RE.match(line) or stripped.startswith("•"):
            return "item"
        if line.startswith(("   ", "\t")):
            return "detail"
        return "normal"

    @classmethod
    def _html_for_text(cls, text: str) -> str:
        lines = text.splitlines() or [text]
        rendered: List[str] = []
        for index, line in enumerate(lines):
            kind = cls._line_kind(line, index)
            if kind == "blank":
                rendered.append('<div class="blank"></div>')
            else:
                rendered.append(
                    f'<div class="line {kind}">{html.escape(line)}</div>'
                )
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>ACM 比赛信息</title>
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; background: #edf2f7; }}
    body {{ color: #1f2937; font-family: "Noto Sans CJK SC", "Microsoft YaHei", Arial, sans-serif; }}
    .page {{ width: {RENDER_WIDTH}px; margin: 0 auto; padding: 42px 56px 52px; }}
    .title {{ color: #0f766e; font-size: 36px; font-weight: 800; line-height: 1.35; margin-bottom: 24px; }}
    .panel {{ padding: 25px 30px 28px; background: #fff; border: 1px solid #dbe4ee; border-radius: 18px; box-shadow: 0 7px 20px rgba(15, 23, 42, .06); }}
    .line {{ white-space: pre-wrap; overflow-wrap: anywhere; font-size: 20px; line-height: 1.55; padding: 4px 0; }}
    .title + .panel {{ padding-top: 22px; }}
    .item {{ color: #0f766e; font-weight: 650; }}
    .detail {{ color: #475569; font-size: 18px; }}
    .section {{ color: #334155; font-weight: 700; margin-top: 12px; }}
    .source {{ color: #64748b; font-size: 16px; margin-bottom: 8px; }}
    .divider {{ color: #94a3b8; font-size: 17px; }}
    .normal {{ color: #334155; }}
    .blank {{ height: 10px; }}
  </style>
</head>
<body>
  <main class="page">
    <div class="title">{html.escape(lines[0])}</div>
    <section class="panel">{"".join(rendered[1:]) or '<div class="line normal"> </div>'}</section>
  </main>
</body>
</html>
"""

    @classmethod
    def _estimate_render_height(cls, text: str) -> int:
        rows = 2
        for index, line in enumerate(text.splitlines() or [text]):
            width = sum(
                2 if unicodedata.east_asian_width(char) in "WFA" else 1
                for char in line
            )
            rows += max(1, (width + 52) // 53)
            if cls._line_kind(line, index) in {"section", "source"}:
                rows += 1
        return max(MIN_RENDER_HEIGHT, min(MAX_RENDER_HEIGHT, 125 + rows * 36))

    @staticmethod
    def _find_renderers() -> List[Tuple[str, str]]:
        configured = (
            os.environ.get("ACMER_QQ_BOT_RENDERER")
            or os.environ.get("MENU_NAVIGATION_RENDERER")
        )
        candidates = [configured] if configured else []
        candidates.extend(
            [
                "chromium",
                "chromium-browser",
                "google-chrome",
                "google-chrome-stable",
                "microsoft-edge",
                "msedge",
                "brave-browser",
                "firefox",
                "wkhtmltoimage",
            ]
        )
        renderers: List[Tuple[str, str]] = []
        seen = set()
        for candidate in candidates:
            if not candidate:
                continue
            if candidate.lower() in {"pillow", "pil"}:
                if ("pillow", "pillow") not in renderers:
                    renderers.append(("pillow", "pillow"))
                continue
            path = shutil.which(candidate)
            if not path or path in seen:
                continue
            seen.add(path)
            name = Path(path).name.lower()
            if "firefox" in name:
                kind = "firefox"
            elif "wkhtmltoimage" in name:
                kind = "wkhtmltoimage"
            else:
                kind = "chromium"
            renderers.append((kind, path))
        return renderers

    @staticmethod
    def _find_cjk_font() -> Optional[str]:
        configured = os.environ.get("ACMER_QQ_BOT_FONT")
        candidates = [configured] if configured else []
        candidates.extend(
            [
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
                "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
                "/usr/share/fonts/truetype/arphic/uming.ttc",
                "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
            ]
        )
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return candidate

        fc_match = shutil.which("fc-match")
        if fc_match:
            try:
                result = subprocess.run(
                    [fc_match, "-f", "%{file}", ":lang=zh"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                path = result.stdout.strip()
                if result.returncode == 0 and Path(path).is_file():
                    return path
            except (OSError, subprocess.SubprocessError):
                pass
        return None

    @staticmethod
    def _run_external_renderer(
        kind: str,
        executable: str,
        html_path: Path,
        image_path: Path,
        height: int,
    ) -> bool:
        if kind == "firefox":
            command = [
                executable,
                "--headless",
                "--no-remote",
                "--screenshot",
                str(image_path),
                "--window-size",
                f"{RENDER_WIDTH},{height}",
                html_path.as_uri(),
            ]
        elif kind == "wkhtmltoimage":
            command = [
                executable,
                "--quiet",
                "--enable-local-file-access",
                "--width",
                str(RENDER_WIDTH),
                "--height",
                str(height),
                html_path.as_uri(),
                str(image_path),
            ]
        else:
            command = [
                executable,
                "--headless",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--hide-scrollbars",
                "--no-first-run",
                "--no-default-browser-check",
                "--force-device-scale-factor=1",
                f"--window-size={RENDER_WIDTH},{height}",
                f"--screenshot={image_path}",
                html_path.as_uri(),
            ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return (
            result.returncode == 0
            and image_path.is_file()
            and image_path.stat().st_size > 0
        )

    @classmethod
    def _render_with_pillow(cls, text: str, image_path: Path) -> bool:
        try:
            from PIL import Image as PILImage
            from PIL import ImageDraw, ImageFont
        except ImportError:
            return False

        font_path = cls._find_cjk_font()
        if not font_path:
            return False
        try:
            title_font = ImageFont.truetype(font_path, 36)
            body_font = ImageFont.truetype(font_path, 20)
            detail_font = ImageFont.truetype(font_path, 18)
            source_font = ImageFont.truetype(font_path, 16)
        except (OSError, ValueError):
            return False

        measure_image = PILImage.new("RGB", (1, 1), "white")
        measure_draw = ImageDraw.Draw(measure_image)

        def line_height(font) -> int:
            box = measure_draw.textbbox((0, 0), "比赛信息Ag", font=font)
            return max(24, box[3] - box[1] + 9)

        def wrap(value: str, font, max_width: int) -> List[str]:
            result: List[str] = []
            for paragraph in value.splitlines() or [""]:
                current = ""
                for char in paragraph:
                    candidate = current + char
                    if current and measure_draw.textbbox(
                        (0, 0), candidate, font=font
                    )[2] > max_width:
                        result.append(current)
                        current = char
                    else:
                        current = candidate
                result.append(current or " ")
            return result

        lines = text.splitlines() or [text]
        inner_width = RENDER_WIDTH - 56 * 2 - 30 * 2
        title = lines[0]
        body_rows = []
        for index, line in enumerate(lines[1:], start=1):
            kind = cls._line_kind(line, index)
            if kind == "blank":
                body_rows.append((kind, " ", body_font, "#334155"))
                continue
            font = (
                source_font
                if kind == "source"
                else detail_font
                if kind == "detail"
                else body_font
            )
            color = {
                "item": "#0f766e",
                "section": "#334155",
                "source": "#64748b",
                "divider": "#94a3b8",
            }.get(kind, "#334155")
            for wrapped in wrap(line, font, inner_width):
                body_rows.append((kind, wrapped, font, color))

        title_height = line_height(title_font)
        body_height = sum(line_height(font) + 4 for _, _, font, _ in body_rows)
        image_height = max(
            MIN_RENDER_HEIGHT,
            min(MAX_RENDER_HEIGHT, 42 + title_height + 30 + body_height + 58),
        )
        image = PILImage.new("RGB", (RENDER_WIDTH, image_height), "#edf2f7")
        draw = ImageDraw.Draw(image)
        draw.text((56, 42), title, font=title_font, fill="#0f766e")

        panel_top = 42 + title_height + 22
        panel_bottom = image_height - 32
        draw.rounded_rectangle(
            (56, panel_top, RENDER_WIDTH - 56, panel_bottom),
            radius=18,
            fill="#ffffff",
            outline="#dbe4ee",
            width=1,
        )
        y = panel_top + 25
        for _, value, font, color in body_rows:
            draw.text((56 + 30, y), value, font=font, fill=color)
            y += line_height(font) + 4
        try:
            image.save(image_path, format="PNG")
        except OSError:
            return False
        return image_path.is_file() and image_path.stat().st_size > 0


def text_chunks(text: str, max_chunk: int = MAX_TEXT_CHUNK):
    """渲染失败时按换行拆分纯文本，避免再次超过单条消息限制。"""
    value = str(text or "")
    start = 0
    while start < len(value):
        end = min(start + max_chunk, len(value))
        if end < len(value):
            newline = value.rfind("\n", start, end)
            if newline > start + 100:
                end = newline
        piece = value[start:end].strip()
        if piece:
            yield piece
        start = end if end > start else start + 1
