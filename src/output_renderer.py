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
RENDER_FORMAT_VERSION = 4
BODY_LETTER_SPACING = 0.35
TITLE_LETTER_SPACING = 1.0

# Chromium、Firefox 与 Pillow 必须使用同一套简体中文字库。只写
# ``Noto Sans CJK SC`` 而不指定 TTC face 时，Pillow 会默认加载第 0
# 个日文字库面，导致菜单里的中文出现字形错乱或方框。
MENU_FONT_FAMILY = (
    '"Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC", '
    '"WenQuanYi Zen Hei", "Microsoft YaHei", "Noto Color Emoji", '
    "sans-serif"
)

_ITEM_RE = re.compile(r"^\s*\d+[.、]\s*")


def _text_units(value: str) -> List[str]:
    """按近似字素切分，避免把 emoji 的变体选择符拆开。"""
    units: List[str] = []
    for char in str(value):
        if units and (
            unicodedata.combining(char)
            or "\ufe00" <= char <= "\ufe0f"
            or char == "\u200d"
            or units[-1].endswith("\u200d")
        ):
            units[-1] += char
        else:
            units.append(char)
    return units


def _tracked_width(draw, value: str, font, tracking: float) -> float:
    units = _text_units(value)
    width = sum(
        draw.textbbox((0, 0), unit, font=font)[2]
        - draw.textbbox((0, 0), unit, font=font)[0]
        for unit in units
    )
    return width + max(0, len(units) - 1) * tracking


def _draw_tracked(draw, xy, value: str, font, fill, tracking: float) -> None:
    cursor = float(xy[0])
    y = xy[1]
    for unit in _text_units(value):
        draw.text((round(cursor), y), unit, font=font, fill=fill)
        box = draw.textbbox((0, 0), unit, font=font)
        cursor += box[2] - box[0] + tracking


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
        self.max_chars = 1
        self.max_lines = 1
        self.configure(max_chars, max_lines)
        self._render_lock = threading.Lock()

    def configure(self, max_chars: int, max_lines: int) -> None:
        """更新文字直发阈值；配置由调用方负责校验业务范围。"""
        self.max_chars = max(1, int(max_chars))
        self.max_lines = max(1, int(max_lines))

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
        # Chromium 根据后缀判断截图格式，临时文件也保留 .png 后缀。
        image_tmp = self.cache_dir / f".response-{digest}.tmp.png"

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
    html, body {{ margin: 0; padding: 0; background: #21162d; }}
    body {{ color: #563b65; font-family: {MENU_FONT_FAMILY}; font-variant-east-asian: simplified; text-rendering: optimizeLegibility; -webkit-font-smoothing: antialiased; }}
    .page {{ width: {RENDER_WIDTH}px; margin: 0 auto; padding: 42px 56px 52px; position:relative; overflow:hidden;
      background:
        radial-gradient(circle at 92% 8%, rgba(255,177,218,.26), transparent 24%),
        radial-gradient(circle at 6% 90%, rgba(145,223,247,.2), transparent 25%),
        linear-gradient(135deg,#281936 0%,#49264d 55%,#203a4a 100%); }}
    .page:before {{ content:""; position:absolute; width:240px; height:240px; right:-120px; top:38px; border:1px solid rgba(255,215,237,.42); border-radius:50%;
      box-shadow:0 0 0 18px rgba(255,215,237,.07),0 0 0 42px rgba(162,225,248,.05); pointer-events:none; }}
    .page:after {{ content:"✿"; position:absolute; right:82px; top:78px; color:rgba(255,225,241,.62); font-size:52px; transform:rotate(14deg); pointer-events:none; }}
    .title {{ color: #fff7fb; font-size: 36px; font-weight: 900; line-height: 1.45; margin-bottom: 28px; letter-spacing:1.6px;
      text-shadow:0 3px 18px rgba(243,132,190,.42); }}
    .title:before {{ content:"✦ PINK PEARL CONTEST GARDEN ✦"; display:block; margin-bottom:8px; color:#ffd5e8; font-size:14px; line-height:1.5; letter-spacing:3.6px; }}
    .panel {{ position:relative; padding: 29px 32px 34px; background:linear-gradient(145deg,rgba(255,252,255,.98),rgba(255,231,245,.94)); border: 1px solid rgba(255,211,235,.95); border-radius: 18px;
      box-shadow: 0 14px 28px rgba(28,10,44,.24), inset 0 0 24px rgba(255,255,255,.7); }}
    .panel:before {{ content:""; position:absolute; left:24px; right:24px; top:0; height:3px; border-radius:99px; background:linear-gradient(90deg,#e467a5,#b9eaf8,#e467a5); opacity:.78; }}
    .line {{ white-space: pre-wrap; overflow-wrap: anywhere; font-size: 20px; line-height: 1.72; padding: 5px 0; letter-spacing:.35px; }}
    .title + .panel {{ padding-top: 26px; }}
    .item {{ color: #c44786; font-weight: 750; letter-spacing:.45px; }}
    .detail {{ color: #705276; font-size: 18px; line-height:1.78; letter-spacing:.35px; }}
    .section {{ color: #71466f; font-weight: 800; margin-top: 16px; letter-spacing:.45px; }}
    .source {{ color: #6c93a8; font-size: 16px; line-height:1.7; margin-bottom: 12px; letter-spacing:.3px; }}
    .divider {{ color: #bd83a7; font-size: 17px; letter-spacing:1.5px; }}
    .normal {{ color: #563b65; }}
    .blank {{ height: 14px; }}
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
        return max(MIN_RENDER_HEIGHT, min(MAX_RENDER_HEIGHT, 165 + rows * 45))

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
    def _font_index_from_env(default: int = 0) -> int:
        try:
            return max(0, int(os.environ.get("ACMER_QQ_BOT_FONT_INDEX", default)))
        except (TypeError, ValueError):
            return max(0, default)

    @staticmethod
    def _find_cjk_font_spec(*, bold: bool = False) -> Tuple[Optional[str], int]:
        """返回真正的简体中文字体路径及 TTC face index。"""
        configured = os.environ.get("ACMER_QQ_BOT_FONT")
        if configured and Path(configured).is_file():
            default_index = (
                2
                if Path(configured).suffix.casefold() == ".ttc"
                and "NotoSansCJK" in Path(configured).name
                else 0
            )
            return configured, AdaptiveOutputRenderer._font_index_from_env(
                default_index
            )

        fc_match = shutil.which("fc-match")
        if fc_match:
            style = "Bold" if bold else "Regular"
            queries = (
                f"Noto Sans CJK SC:style={style}",
                "Noto Sans CJK SC",
                ":lang=zh-cn",
            )
            for query in queries:
                try:
                    result = subprocess.run(
                        [
                            fc_match,
                            "-f",
                            "%{file}|%{index}",
                            query,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                    path_text, _, index_text = (
                        result.stdout.strip().partition("|")
                    )
                    if result.returncode != 0 or not Path(path_text).is_file():
                        continue
                    try:
                        index = int(index_text or "0")
                    except ValueError:
                        index = 0
                    return path_text, max(0, index)
                except (OSError, subprocess.SubprocessError):
                    break

        if bold:
            candidates = [
                ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 2),
                ("/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc", 2),
                ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 2),
                ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
            ]
        else:
            candidates = [
                ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 2),
                ("/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc", 2),
                ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
                ("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf", 0),
            ]
        for path, index in candidates:
            if Path(path).is_file():
                return path, index
        return None, 0

    @staticmethod
    def _find_cjk_font() -> Optional[str]:
        """兼容旧调用方，只返回字体路径。"""
        path, _ = AdaptiveOutputRenderer._find_cjk_font_spec()
        return path

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

        regular_spec = cls._find_cjk_font_spec()
        bold_spec = cls._find_cjk_font_spec(bold=True)
        if not regular_spec[0] or not bold_spec[0]:
            return False
        try:
            title_font = ImageFont.truetype(
                bold_spec[0], 36, index=bold_spec[1]
            )
            body_font = ImageFont.truetype(
                regular_spec[0], 20, index=regular_spec[1]
            )
            detail_font = ImageFont.truetype(
                regular_spec[0], 18, index=regular_spec[1]
            )
            source_font = ImageFont.truetype(
                regular_spec[0], 16, index=regular_spec[1]
            )
        except (OSError, ValueError):
            return False

        measure_image = PILImage.new("RGB", (1, 1), "white")
        measure_draw = ImageDraw.Draw(measure_image)

        def line_height(font) -> int:
            box = measure_draw.textbbox((0, 0), "比赛信息Ag", font=font)
            return max(24, box[3] - box[1] + 9)

        def wrap(
            value: str,
            font,
            max_width: int,
            tracking: float,
        ) -> List[str]:
            result: List[str] = []
            for paragraph in value.splitlines() or [""]:
                current = ""
                for char in paragraph:
                    candidate = current + char
                    if current and _tracked_width(
                        measure_draw, candidate, font, tracking
                    ) > max_width:
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
                body_rows.append((kind, " ", body_font, "#705276", 0.0))
                continue
            font = (
                source_font
                if kind == "source"
                else detail_font
                if kind == "detail"
                else body_font
            )
            color = {
                "item": "#c44786",
                "section": "#71466f",
                "source": "#6c93a8",
                "divider": "#bd83a7",
            }.get(kind, "#563b65")
            tracking = (
                0.45
                if kind in {"item", "section"}
                else 0.3
                if kind == "source"
                else BODY_LETTER_SPACING
            )
            for wrapped in wrap(line, font, inner_width, tracking):
                body_rows.append((kind, wrapped, font, color, tracking))

        eyebrow_height = line_height(source_font)
        title_height = line_height(title_font)
        body_height = sum(
            line_height(font) + 7 for _, _, font, _, _ in body_rows
        )
        image_height = max(
            MIN_RENDER_HEIGHT,
            min(
                MAX_RENDER_HEIGHT,
                42
                + eyebrow_height
                + 8
                + title_height
                + 30
                + body_height
                + 58,
            ),
        )
        image = PILImage.new("RGB", (RENDER_WIDTH, image_height), "#fff4fa")
        draw = ImageDraw.Draw(image)
        _draw_tracked(
            draw,
            (56, 42),
            "ELYSIAN // PINK PEARL ARCHIVE",
            source_font,
            "#c44786",
            0.55,
        )
        title_y = 42 + eyebrow_height + 8
        _draw_tracked(
            draw,
            (56, title_y),
            title,
            title_font,
            "#7a456f",
            TITLE_LETTER_SPACING,
        )

        panel_top = title_y + title_height + 22
        panel_bottom = image_height - 32
        draw.rounded_rectangle(
            (56, panel_top, RENDER_WIDTH - 56, panel_bottom),
            radius=18,
            fill="#fffdfd",
            outline="#f0c9dc",
            width=1,
        )
        draw.line(
            (82, panel_top + 2, RENDER_WIDTH - 82, panel_top + 2),
            fill="#e467a5",
            width=3,
        )
        y = panel_top + 25
        for _, value, font, color, tracking in body_rows:
            _draw_tracked(
                draw,
                (56 + 30, y),
                value,
                font,
                color,
                tracking,
            )
            y += line_height(font) + 7
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
