"""账号战绩卡与群排行卡：二次元科幻风格 HTML/PNG 渲染。"""
from __future__ import annotations

import hashlib
import html
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .account_models import AccountProfile, platform_label
from .models import CN_TZ
from .output_renderer import AdaptiveOutputRenderer

CARD_FORMAT_VERSION = 3
CARD_WIDTH = 1200
MIN_CARD_HEIGHT = 760
MAX_CARD_HEIGHT = 5200
PROFILE_MIN_RENDER_HEIGHT = 500
PROFILE_PAGE_OVERHEAD = 245
PROFILE_CARD_BASE_HEIGHT = 270
PROFILE_GRID_GAP = 18
RANKING_PAGE_START = 215
RANKING_LIST_OVERHEAD = 21
RANKING_ROW_HEIGHT = 104
RANKING_NOTE_MARGIN = 18
RANKING_NOTE_PADDING = 28
RANKING_NOTE_LINE_HEIGHT = 24
RANKING_FOOTER_OVERHEAD = 44
RANKING_PAGE_BOTTOM = 62
RANKING_HEIGHT_SAFETY = 24
RANKING_MIN_RENDER_HEIGHT = 520
OVERVIEW_PAGE_START = 215
OVERVIEW_SECTION_BASE = 74
OVERVIEW_ROW_HEIGHT = 65
OVERVIEW_EMPTY_SECTION_HEIGHT = 106
OVERVIEW_GRID_GAP = 24
OVERVIEW_HEIGHT_SAFETY = 16
OVERVIEW_MIN_RENDER_HEIGHT = 520

PLATFORM_COLORS = {
    "codeforces": ("#ff557a", "#ff9a5a"),
    "nowcoder": ("#43d7ff", "#4f8cff"),
    "luogu": ("#b276ff", "#ff63c8"),
    "atcoder": ("#62e58b", "#15b981"),
}


def _escape(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _format_number(value: object, fallback: str = "—") -> str:
    if value is None or value == "":
        return fallback
    return str(value)


def _format_delta(value: object) -> str:
    if value is None or value == "":
        return "—"
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:+d}"


def _primary_metric(profile: AccountProfile) -> tuple[str, str]:
    if profile.platform == "luogu":
        if profile.rating is not None:
            return "Elo", _format_number(profile.rating)
        if profile.rating_rank is not None:
            return "平台排名", f"#{profile.rating_rank}"
        return "平台排名", "—"
    return "Rating", _format_number(profile.rating)


def _updated_text(timestamp: Optional[float] = None) -> str:
    when = (
        datetime.fromtimestamp(timestamp, tz=CN_TZ)
        if timestamp
        else datetime.now(CN_TZ)
    )
    return when.strftime("%Y-%m-%d %H:%M")


class AccountCardRenderer:
    """将账号资料/排行数据渲染为 PNG；浏览器不可用时使用 Pillow。"""

    def __init__(self, cache_dir: Optional[str | Path] = None) -> None:
        self.cache_dir = (
            Path(cache_dir)
            if cache_dir is not None
            else Path(__file__).resolve().parent.parent
            / "data"
            / "account_cards"
        ).expanduser().resolve()
        self._lock = threading.Lock()

    def render_profile(
        self,
        profiles: Iterable[AccountProfile],
        *,
        display_name: str = "ACM 选手",
        weekly_changes: Optional[Dict[str, Optional[int]]] = None,
    ) -> Optional[Path]:
        profile_list = list(profiles)
        source = {
            "kind": "profile",
            "display_name": display_name,
            "weekly_changes": weekly_changes or {},
            "profiles": [
                {
                    key: value
                    for key, value in profile.public_dict().items()
                    if key != "fetched_at"
                }
                for profile in profile_list
            ],
        }
        body = self._profile_html(
            profile_list,
            display_name=display_name,
            weekly_changes=weekly_changes or {},
        )
        fallback = self._pillow_profile
        return self._render(body, source, fallback, profile_list, display_name, weekly_changes or {})

    def render_ranking(
        self,
        rows: List[Dict[str, Any]],
        *,
        title: str,
        subtitle: str,
        metric_label: str = "Rating",
        note: str = "",
    ) -> Optional[Path]:
        source = {
            "kind": "ranking",
            "title": title,
            "subtitle": subtitle,
            "metric_label": metric_label,
            "note": note,
            "rows": rows,
        }
        body = self._ranking_html(
            rows,
            title=title,
            subtitle=subtitle,
            metric_label=metric_label,
            note=note,
        )
        return self._render(
            body,
            source,
            self._pillow_ranking,
            rows,
            title,
            subtitle,
            metric_label,
            note,
        )

    def render_overview_ranking(
        self,
        sections: Dict[str, List[Dict[str, Any]]],
        *,
        title: str,
        subtitle: str,
        metric_label: str = "Rating",
        note: str = "",
    ) -> Optional[Path]:
        source = {
            "kind": "overview",
            "title": title,
            "subtitle": subtitle,
            "metric_label": metric_label,
            "note": note,
            "sections": sections,
        }
        body = self._overview_html(
            sections,
            title=title,
            subtitle=subtitle,
            metric_label=metric_label,
            note=note,
        )
        return self._render(
            body,
            source,
            self._pillow_overview,
            sections,
            title,
            subtitle,
            metric_label,
            note,
        )

    def _render(
        self,
        body: str,
        source: Dict[str, Any],
        fallback,
        *fallback_args,
    ) -> Optional[Path]:
        digest = hashlib.sha256(
            f"{CARD_FORMAT_VERSION}\0{json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}".encode(
                "utf-8"
            )
        ).hexdigest()[:24]
        html_path = self.cache_dir / f"account-card-{digest}.html"
        image_path = self.cache_dir / f"account-card-{digest}.png"
        html_tmp = self.cache_dir / f".account-card-{digest}.html.tmp"
        # Chromium 根据文件扩展名判断截图格式；临时文件也必须以 .png 结尾，
        # 否则会报 Unsupported screenshot image file type: .tmp。
        image_tmp = self.cache_dir / f".account-card-{digest}.tmp.png"

        with self._lock:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                if image_path.is_file() and image_path.stat().st_size > 0:
                    return image_path
                html_tmp.write_text(body, encoding="utf-8")
                os.replace(html_tmp, html_path)
            except (OSError, UnicodeError):
                return None

            height = self._estimate_height(source)
            for kind, executable in AdaptiveOutputRenderer._find_renderers():
                image_tmp.unlink(missing_ok=True)
                if kind == "pillow":
                    success = fallback(*fallback_args, image_tmp)
                else:
                    success = AdaptiveOutputRenderer._run_external_renderer(
                        kind, executable, html_path, image_tmp, height
                    )
                if success:
                    try:
                        os.replace(image_tmp, image_path)
                    except OSError:
                        return None
                    return image_path

            image_tmp.unlink(missing_ok=True)
            if fallback(*fallback_args, image_tmp):
                try:
                    os.replace(image_tmp, image_path)
                except OSError:
                    return None
                return image_path
        return None

    @staticmethod
    def _estimate_height(source: Dict[str, Any]) -> int:
        if source.get("kind") == "ranking":
            return AccountCardRenderer._ranking_height(
                source.get("rows") or [],
                note=str(source.get("note") or ""),
            )
        if source.get("kind") == "overview":
            sections = source.get("sections") or {}
            return AccountCardRenderer._overview_height(
                sections,
                note=str(source.get("note") or ""),
            )
        return AccountCardRenderer._profile_height(
            source.get("profiles") or []
        )

    @staticmethod
    def _text_width(value: object) -> int:
        """估算中英文混排文本宽度，用于预测 HTML 卡片的换行高度。"""
        text = str(value or "")
        return sum(
            2 if char.isascii() is False else 1
            for char in text
        )

    @classmethod
    def _profile_card_height(
        cls,
        profile: object,
        *,
        compact: bool = False,
    ) -> int:
        """估算单个平台卡片高度，和 CSS 的主要换行点保持一致。"""
        if isinstance(profile, AccountProfile):
            recent_contests = profile.recent_contests
            values = {
                "handle": profile.handle,
                "rank": profile.rank_text or profile.color or "",
                "school": profile.school,
                "organization": profile.organization,
                "country": profile.country,
                "solved_count": profile.solved_count,
            }
        elif isinstance(profile, dict):
            recent_contests = profile.get("recent_contests") or []
            values = {
                "handle": profile.get("handle", ""),
                "rank": profile.get("rank_text") or profile.get("color") or "",
                "school": profile.get("school", ""),
                "organization": profile.get("organization", ""),
                "country": profile.get("country", ""),
                "solved_count": profile.get("solved_count"),
            }
        else:
            return PROFILE_CARD_BASE_HEIGHT

        height = 222 if compact else PROFILE_CARD_BASE_HEIGHT
        if values["solved_count"] is not None:
            height += 28

        # 单平台卡片使用整行宽度；这些阈值对应压缩后的 CSS 卡片宽度。
        handle_width = cls._text_width(values["handle"])
        if handle_width > 42:
            height += min(60, ((handle_width - 1) // 42) * 28)

        rank_width = cls._text_width(values["rank"])
        if rank_width > 24:
            height += min(28, ((rank_width - 1) // 24) * 24)

        extras = " · ".join(
            str(values[key] or "").strip()
            for key in ("school", "organization", "country")
            if str(values[key] or "").strip()
        )
        if extras:
            height += min(48, max(0, (cls._text_width(extras) - 1) // 84) * 24)

        latest = (
            recent_contests[0]
            if recent_contests and isinstance(recent_contests[0], dict)
            else {}
        )
        if latest.get("name"):
            height += 30
            latest_width = cls._text_width(latest.get("name"))
            if latest_width > 84:
                height += min(30, ((latest_width - 1) // 84) * 24)
        return height

    @classmethod
    def _profile_height(cls, profiles: Iterable[object]) -> int:
        """按卡片数量和内容估算截图高度，避免固定模板留下大块空白。"""
        profile_list = list(profiles)
        if not profile_list:
            grid_height = 96
        else:
            row_heights = []
            for offset in range(0, len(profile_list), 2):
                row = profile_list[offset : offset + 2]
                row_heights.append(
                    max(
                        cls._profile_card_height(
                            item,
                            compact=len(profile_list) == 1,
                        )
                        for item in row
                    )
                )
            grid_height = sum(row_heights) + max(0, len(row_heights) - 1) * PROFILE_GRID_GAP
        return max(
            PROFILE_MIN_RENDER_HEIGHT,
            min(MAX_CARD_HEIGHT, PROFILE_PAGE_OVERHEAD + grid_height),
        )

    @staticmethod
    def _ranking_text_width(value: object) -> int:
        """估算排行昵称/账号在固定列中的混排宽度。"""
        text = str(value or "")
        return sum(2 if not char.isascii() else 1 for char in text)

    @classmethod
    def _ranking_row_height(cls, row: object) -> int:
        """估算排行行高，避免长昵称换行后挤压下一行。"""
        if not isinstance(row, dict):
            return RANKING_ROW_HEIGHT
        display_name = cls._ranking_text_width(
            row.get("display_name") or row.get("qq_name") or "未知用户"
        )
        handle = cls._ranking_text_width(row.get("handle") or "未绑定")
        # 当前 HTML 中 rank-user 可用宽度约 616px；对应字体下分别按
        # 55/82 个中英文混排单位估算换行。
        name_lines = max(1, (display_name + 54) // 55)
        handle_lines = max(1, (handle + 81) // 82)
        user_height = name_lines * 30 + 5 + handle_lines * 21
        return max(RANKING_ROW_HEIGHT, user_height + 40 + 1)

    @classmethod
    def _ranking_height(
        cls,
        rows: Iterable[object],
        *,
        note: str = "",
    ) -> int:
        """按排行行数、长文本和备注估算截图高度。"""
        row_list = list(rows)
        rows_height = sum(cls._ranking_row_height(row) for row in row_list)
        list_height = RANKING_LIST_OVERHEAD + rows_height
        note_height = 0
        if note:
            note_width = cls._ranking_text_width(note)
            note_lines = max(1, (note_width + 127) // 128)
            note_height = (
                RANKING_NOTE_MARGIN
                + RANKING_NOTE_PADDING
                + note_lines * RANKING_NOTE_LINE_HEIGHT
            )
        height = (
            RANKING_PAGE_START
            + list_height
            + note_height
            + RANKING_FOOTER_OVERHEAD
            + RANKING_PAGE_BOTTOM
            + RANKING_HEIGHT_SAFETY
        )
        return max(
            RANKING_MIN_RENDER_HEIGHT,
            min(MAX_CARD_HEIGHT, height),
        )

    @classmethod
    def _overview_height(
        cls,
        sections: Dict[str, List[Dict[str, Any]]],
        *,
        note: str = "",
    ) -> int:
        """按总览分区和行数估算截图高度，避免使用固定大画布。"""
        section_heights = []
        for rows in sections.values():
            count = min(5, len(rows))
            section_heights.append(
                OVERVIEW_EMPTY_SECTION_HEIGHT
                if count == 0
                else OVERVIEW_SECTION_BASE + count * OVERVIEW_ROW_HEIGHT
            )
        if not section_heights:
            grid_height = OVERVIEW_EMPTY_SECTION_HEIGHT
        else:
            grid_height = 0
            for offset in range(0, len(section_heights), 2):
                grid_height += max(section_heights[offset : offset + 2])
            grid_height += max(0, (len(section_heights) + 1) // 2 - 1) * OVERVIEW_GRID_GAP

        note_height = 0
        if note:
            note_width = cls._ranking_text_width(note)
            note_lines = max(1, (note_width + 127) // 128)
            note_height = (
                RANKING_NOTE_MARGIN
                + RANKING_NOTE_PADDING
                + note_lines * RANKING_NOTE_LINE_HEIGHT
            )
        height = (
            OVERVIEW_PAGE_START
            + grid_height
            + note_height
            + RANKING_FOOTER_OVERHEAD
            + RANKING_PAGE_BOTTOM
            + OVERVIEW_HEIGHT_SAFETY
        )
        return max(
            OVERVIEW_MIN_RENDER_HEIGHT,
            min(MAX_CARD_HEIGHT, height),
        )

    @classmethod
    def _profile_html(
        cls,
        profiles: List[AccountProfile],
        *,
        display_name: str,
        weekly_changes: Dict[str, Optional[int]],
    ) -> str:
        cards = []
        for profile in profiles:
            start, end = PLATFORM_COLORS.get(
                profile.platform, ("#55e6ff", "#a855f7")
            )
            delta = (
                weekly_changes.get(profile.platform)
                if profile.platform in weekly_changes
                else profile.recent_delta
            )
            delta_class = "positive" if (delta or 0) > 0 else "negative" if (delta or 0) < 0 else ""
            primary_label, primary_value = _primary_metric(profile)
            detail = [
                (f"当前 {primary_label}", primary_value),
                ("最高 Rating", _format_number(profile.max_rating)),
                ("平台排名", _format_number(profile.rating_rank or profile.rank_text)),
                ("参赛次数", _format_number(profile.contest_count)),
            ]
            if profile.solved_count is not None:
                detail.append(("通过题数", _format_number(profile.solved_count)))
            details_html = "".join(
                f'<div class="stat"><span>{_escape(label)}</span><b>{_escape(value)}</b></div>'
                for label, value in detail
            )
            extra_values = [
                profile.school,
                profile.organization,
                profile.country,
            ]
            if profile.platform == "luogu":
                ccf_level = str(profile.extra.get("ccf_level") or "").strip()
                xcpc_level = str(profile.extra.get("xcpc_level") or "").strip()
                if ccf_level:
                    extra_values.append(f"CCF {ccf_level}")
                if xcpc_level:
                    extra_values.append(f"XCPC {xcpc_level}")
            extras = " · ".join(value for value in extra_values if value)
            latest = profile.recent_contests[0] if profile.recent_contests else {}
            latest_text = ""
            if isinstance(latest, dict) and latest.get("name"):
                latest_text = (
                    f'<div class="recent">最近：{_escape(latest.get("name"))}'
                    f' · {_escape(_format_delta(latest.get("delta")))}</div>'
                )
            cards.append(
                f"""
                <article class="platform-card" style="--accent:{_escape(start)};--accent2:{_escape(end)}">
                  <div class="platform-head">
                    <span class="platform-tag">{_escape(platform_label(profile.platform))}</span>
                    <span class="verified">◆ VERIFIED</span>
                  </div>
                  <div class="profile-main">
                    <div class="handle">{_escape(profile.handle)}</div>
                    <div class="rating-row">
                      <span class="rating">{_escape(primary_value)}</span>
                      <span class="rating-label">{_escape(primary_label)}</span>
                      <span class="rank">{_escape(profile.rank_text or profile.color or "未评级")}</span>
                    </div>
                  </div>
                  <div class="stats">{details_html}</div>
                  <div class="profile-meta">
                    <div class="trend {delta_class}">本次变化：{_escape(_format_delta(delta))}</div>
                    {latest_text}
                    {f'<div class="extra">{_escape(extras)}</div>' if extras else ""}
                  </div>
                </article>
                """
            )
        if not cards:
            cards.append(
                '<article class="empty-card">尚未绑定竞赛平台账号</article>'
            )
        updated = _updated_text(
            max((p.fetched_at for p in profiles), default=None)
        )
        grid_class = (
            "profile-grid profile-single"
            if len(profiles) == 1
            else "profile-grid"
        )
        return cls._document(
            title="ACM 竞赛战绩卡",
            subtitle=f"{display_name} · 账号同步完成",
            body=(
                f'<div class="{grid_class}">{"".join(cards)}</div>'
                f'<div class="card-footer">数据更新时间：{_escape(updated)} · '
                "仅展示平台公开资料</div>"
            ),
            page_class="profile-document",
        )

    @classmethod
    def _ranking_html(
        cls,
        rows: List[Dict[str, Any]],
        *,
        title: str,
        subtitle: str,
        metric_label: str,
        note: str,
    ) -> str:
        rendered = []
        for index, row in enumerate(rows, start=1):
            delta = row.get("delta")
            delta_class = "positive" if (delta or 0) > 0 else "negative" if (delta or 0) < 0 else ""
            rendered.append(
                f"""
                <div class="rank-row">
                  <div class="rank-no">{index:02d}</div>
                  <div class="rank-user">
                    <b>{_escape(row.get("display_name") or row.get("qq_name") or "未知用户")}</b>
                    <span>{_escape(row.get("handle") or "未绑定")}</span>
                  </div>
                  <div class="rank-value">
                    <strong>{_escape(_format_number(row.get("display_value", row.get("value"))))}</strong>
                    <small>{_escape(row.get("metric_label") or metric_label)}</small>
                  </div>
                  <div class="rank-delta {delta_class}">{_escape(_format_delta(delta))}</div>
                </div>
                """
            )
        ranking_body = "".join(rendered) or (
            '<div class="empty-card">当前还没有可排行的成员</div>'
        )
        body = f'<div class="ranking-list">{ranking_body}</div>'
        if note:
            body += f'<div class="rank-note">{_escape(note)}</div>'
        body += (
            '<div class="card-footer">只展示已绑定且加入本群排行的成员 · '
            f"生成时间：{_escape(_updated_text())}</div>"
        )
        return cls._document(
            title=title,
            subtitle=subtitle,
            body=body,
        )

    @classmethod
    def _overview_html(
        cls,
        sections: Dict[str, List[Dict[str, Any]]],
        *,
        title: str,
        subtitle: str,
        metric_label: str,
        note: str,
    ) -> str:
        blocks = []
        for platform, rows in sections.items():
            start, end = PLATFORM_COLORS.get(
                platform, ("#55e6ff", "#a855f7")
            )
            rendered = []
            for index, row in enumerate(rows[:5], start=1):
                delta = row.get("delta")
                delta_class = (
                    "positive"
                    if (delta or 0) > 0
                    else "negative"
                    if (delta or 0) < 0
                    else ""
                )
                rendered.append(
                    f"""
                    <div class="mini-row">
                      <span class="mini-no">{index:02d}</span>
                      <span class="mini-user"><b>{_escape(row.get("display_name") or row.get("qq_name") or "未知用户")}</b><small>{_escape(row.get("handle") or "")}</small></span>
                      <strong class="mini-value">{_escape(_format_number(row.get("display_value", row.get("value"))))}</strong>
                      <span class="mini-delta {delta_class}">{_escape(_format_delta(delta))}</span>
                    </div>
                    """
                )
            if not rendered:
                rendered.append('<div class="mini-empty">暂无数据</div>')
            blocks.append(
                f"""
                <section class="mini-section" style="--accent:{_escape(start)};--accent2:{_escape(end)}">
                  <h2>{_escape(platform_label(platform))}</h2>
                  {''.join(rendered)}
                </section>
                """
            )
        overview_body = "".join(blocks) or (
            '<div class="empty-card">暂无可排行成员</div>'
        )
        body = f'<div class="overview-grid">{overview_body}</div>'
        if note:
            body += f'<div class="rank-note">{_escape(note)}</div>'
        body += (
            '<div class="card-footer">只展示已绑定且加入本群排行的成员 · '
            f"生成时间：{_escape(_updated_text())}</div>"
        )
        return cls._document(title=title, subtitle=subtitle, body=body)

    @staticmethod
    def _document(
        *,
        title: str,
        subtitle: str,
        body: str,
        page_class: str = "",
    ) -> str:
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{_escape(title)}</title>
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{ margin:0; padding:0; min-height:100%; }}
    body {{
      color:#e7f8ff;
      font-family:"Noto Sans CJK SC","Microsoft YaHei",Arial,sans-serif;
      background:
        radial-gradient(circle at 12% 8%, rgba(48,220,255,.22), transparent 29%),
        radial-gradient(circle at 88% 16%, rgba(212,70,255,.2), transparent 28%),
        linear-gradient(135deg,#090d24 0%,#11143a 52%,#180d32 100%);
    }}
    .page {{ width:{CARD_WIDTH}px; margin:0 auto; padding:58px 70px 62px; position:relative; overflow:hidden; }}
    .page:before {{ content:""; position:absolute; inset:0; opacity:.2; pointer-events:none;
      background-image:linear-gradient(rgba(101,229,255,.15) 1px,transparent 1px),
        linear-gradient(90deg,rgba(101,229,255,.15) 1px,transparent 1px);
      background-size:42px 42px; mask-image:linear-gradient(to bottom,black,transparent 86%); }}
    .orb {{ position:absolute; border-radius:50%; filter:blur(3px); opacity:.55; }}
    .orb.one {{ width:210px; height:210px; right:-80px; top:160px; background:#b33cff; box-shadow:0 0 80px #a42cff; }}
    .orb.two {{ width:150px; height:150px; left:-60px; bottom:120px; background:#18d8ff; box-shadow:0 0 70px #18d8ff; }}
    .header {{ position:relative; margin-bottom:30px; }}
    .eyebrow {{ color:#63eaff; letter-spacing:4px; font-size:15px; font-weight:700; }}
    h1 {{ margin:8px 0 5px; font-size:44px; letter-spacing:2px; color:#fff; text-shadow:0 0 20px rgba(86,231,255,.55); }}
    .subtitle {{ color:#a8c8e5; font-size:20px; }}
    .profile-grid {{ position:relative; display:grid; grid-template-columns:1fr 1fr; gap:24px; }}
    .platform-card, .ranking-list, .empty-card {{ position:relative; background:rgba(10,17,45,.82); border:1px solid rgba(121,226,255,.3);
      border-radius:22px; box-shadow:0 18px 42px rgba(0,0,0,.28), inset 0 0 28px rgba(74,157,255,.06); }}
    .platform-card {{ padding:26px 30px 24px; overflow:hidden; }}
    .platform-card:before {{ content:""; position:absolute; left:0; top:0; bottom:0; width:6px; background:linear-gradient(var(--accent),var(--accent2)); box-shadow:0 0 20px var(--accent); }}
    .platform-head {{ display:flex; justify-content:space-between; align-items:center; }}
    .platform-tag {{ color:var(--accent); font-size:20px; font-weight:800; letter-spacing:1px; }}
    .verified {{ color:#7f9bb7; font-size:12px; letter-spacing:1px; }}
    .handle {{ margin-top:13px; font-size:28px; font-weight:800; color:#fff; overflow-wrap:anywhere; }}
    .rating-row {{ display:flex; align-items:baseline; gap:16px; margin:13px 0 19px; }}
    .rating {{ font-size:48px; line-height:1; font-weight:900; color:var(--accent); text-shadow:0 0 18px color-mix(in srgb,var(--accent),transparent 45%); }}
    .rating-label {{ color:#8faac3; font-size:14px; letter-spacing:1px; }}
    .rank {{ color:#c8d8e9; font-size:18px; }}
    .stats {{ display:grid; grid-template-columns:1fr 1fr; gap:10px 18px; }}
    .stat {{ display:flex; justify-content:space-between; gap:10px; color:#7895b2; font-size:15px; border-bottom:1px dashed rgba(142,203,231,.18); padding-bottom:7px; }}
    .stat b {{ color:#e8f8ff; font-size:17px; text-align:right; }}
    .trend {{ margin-top:16px; color:#a8c8e5; font-size:17px; }}
    .trend.positive, .rank-delta.positive {{ color:#61f0ad; }}
    .trend.negative, .rank-delta.negative {{ color:#ff7899; }}
    .extra {{ margin-top:13px; color:#8faac3; font-size:15px; overflow-wrap:anywhere; }}
    .recent {{ margin-top:13px; color:#b7d5eb; font-size:15px; overflow-wrap:anywhere; }}
    .profile-main {{ position:relative; }}
    .profile-meta {{ position:relative; display:flex; flex-wrap:wrap; align-items:center; gap:8px 18px; margin-top:11px; }}
    .profile-meta > .trend,
    .profile-meta > .recent,
    .profile-meta > .extra {{ flex:1 1 210px; min-width:0; margin-top:0; }}
    .ranking-list {{ position:relative; overflow:hidden; padding:10px 24px; }}
    .rank-row {{ display:grid; grid-template-columns:80px 1fr 150px 100px; align-items:center; gap:16px; padding:20px 8px; border-bottom:1px solid rgba(142,203,231,.15); }}
    .rank-row:last-child {{ border-bottom:0; }}
    .rank-no {{ color:#62eaff; font-size:28px; font-weight:900; }}
    .rank-user b {{ display:block; color:#fff; font-size:21px; }}
    .rank-user span {{ display:block; color:#8ea9c2; margin-top:5px; font-size:15px; overflow-wrap:anywhere; }}
    .rank-value {{ text-align:right; }}
    .rank-value strong {{ display:block; color:#fff; font-size:27px; }}
    .rank-value small {{ color:#7e9bb7; font-size:13px; }}
    .rank-delta {{ text-align:right; font-size:19px; font-weight:800; }}
    .overview-grid {{ position:relative; display:grid; grid-template-columns:1fr 1fr; gap:24px; }}
    .mini-section {{ position:relative; overflow:hidden; padding:20px 22px 12px; background:rgba(10,17,45,.82); border:1px solid rgba(121,226,255,.3); border-radius:20px; box-shadow:0 18px 42px rgba(0,0,0,.28), inset 0 0 28px rgba(74,157,255,.06); }}
    .mini-section:before {{ content:""; position:absolute; left:0; top:0; bottom:0; width:5px; background:linear-gradient(var(--accent),var(--accent2)); box-shadow:0 0 18px var(--accent); }}
    .mini-section h2 {{ margin:0 0 8px; color:var(--accent); font-size:22px; }}
    .mini-row {{ display:grid; grid-template-columns:48px 1fr 90px 72px; align-items:center; gap:8px; min-height:65px; border-bottom:1px solid rgba(142,203,231,.13); }}
    .mini-row:last-child {{ border-bottom:0; }}
    .mini-no {{ color:#63eaff; font-size:18px; font-weight:800; }}
    .mini-user b {{ display:block; color:#fff; font-size:16px; overflow-wrap:anywhere; }}
    .mini-user small {{ display:block; color:#8ea9c2; margin-top:3px; font-size:12px; overflow-wrap:anywhere; }}
    .mini-value {{ color:#fff; font-size:20px; text-align:right; }}
    .mini-delta {{ color:#a8c8e5; font-size:14px; text-align:right; }}
    .mini-delta.positive {{ color:#61f0ad; }}
    .mini-delta.negative {{ color:#ff7899; }}
    .mini-empty {{ color:#9ab4ce; padding:20px 0; }}
    .rank-note {{ position:relative; margin-top:18px; padding:14px 18px; color:#b1cae2; background:rgba(35,56,105,.38); border-radius:12px; font-size:16px; }}
    .empty-card {{ padding:48px; color:#9ab4ce; font-size:20px; text-align:center; }}
    .card-footer {{ position:relative; margin-top:24px; color:#718ba8; font-size:14px; letter-spacing:.4px; }}
    .profile-document.page {{ padding:40px 70px 36px; }}
    .profile-document .header {{ margin-bottom:20px; }}
    .profile-document h1 {{ margin:6px 0 4px; font-size:38px; }}
    .profile-document .subtitle {{ font-size:18px; }}
    .profile-document .profile-grid {{ gap:18px; align-items:start; }}
    .profile-document .profile-grid.profile-single {{ grid-template-columns:1fr; }}
    .profile-document .platform-card {{ padding:20px 24px 18px; border-radius:18px; }}
    .profile-document .profile-grid.profile-single .platform-card {{
      display:grid;
      grid-template-columns:minmax(230px,.9fr) minmax(0,1.1fr);
      column-gap:26px;
      row-gap:8px;
    }}
    .profile-document .profile-grid.profile-single .platform-head,
    .profile-document .profile-grid.profile-single .profile-meta {{
      grid-column:1 / -1;
    }}
    .profile-document .profile-grid.profile-single .profile-main {{
      grid-column:1;
      align-self:center;
    }}
    .profile-document .profile-grid.profile-single .stats {{
      grid-column:2;
      align-self:center;
    }}
    .profile-document .profile-grid.profile-single .profile-meta {{
      margin-top:2px;
    }}
    .profile-document .platform-tag {{ font-size:18px; }}
    .profile-document .verified {{ font-size:11px; }}
    .profile-document .handle {{ margin-top:8px; font-size:25px; line-height:1.2; }}
    .profile-document .rating-row {{ gap:12px; margin:10px 0 14px; }}
    .profile-document .rating {{ font-size:42px; }}
    .profile-document .rating-label {{ font-size:13px; }}
    .profile-document .rank {{ font-size:16px; }}
    .profile-document .stats {{ gap:7px 14px; }}
    .profile-document .stat {{ font-size:14px; padding-bottom:5px; }}
    .profile-document .stat b {{ font-size:16px; }}
    .profile-document .trend {{ margin-top:11px; font-size:15px; }}
    .profile-document .recent,
    .profile-document .extra {{ margin-top:9px; font-size:14px; }}
    .profile-document .card-footer {{ margin-top:18px; font-size:13px; }}
    .profile-document .empty-card {{ padding:32px; font-size:18px; }}
  </style>
</head>
<body>
  <main class="page {_escape(page_class)}">
    <span class="orb one"></span><span class="orb two"></span>
    <header class="header">
      <div class="eyebrow">ACM // NEURAL CONTEST NETWORK</div>
      <h1>{_escape(title)}</h1>
      <div class="subtitle">{_escape(subtitle)}</div>
    </header>
    {body}
  </main>
</body>
</html>
"""

    @staticmethod
    def _find_font(size: int):
        try:
            from PIL import ImageFont
        except ImportError:
            return None

        path = AdaptiveOutputRenderer._find_cjk_font()
        if not path:
            return None
        try:
            return ImageFont.truetype(path, size)
        except (OSError, ValueError):
            return None

    @classmethod
    def _pillow_profile(
        cls,
        profiles: List[AccountProfile],
        display_name: str,
        weekly_changes: Dict[str, Optional[int]],
        image_path: Path,
    ) -> bool:
        try:
            from PIL import Image as PILImage
            from PIL import ImageDraw
        except ImportError:
            return False
        title_font = cls._find_font(42)
        subtitle_font = cls._find_font(20)
        platform_font = cls._find_font(22)
        handle_font = cls._find_font(28)
        rating_font = cls._find_font(48)
        body_font = cls._find_font(17)
        if not all((title_font, subtitle_font, platform_font, handle_font, rating_font, body_font)):
            return False
        single = len(profiles) == 1
        card_gap = 18
        card_width = CARD_WIDTH - 140 if single else (CARD_WIDTH - 140 - card_gap) // 2
        card_rows = []
        for offset in range(0, len(profiles), 2):
            row_profiles = profiles[offset : offset + 2]
            card_rows.append(
                max(
                    270 if single else 300,
                    max(
                        cls._profile_card_height(profile, compact=single)
                        for profile in row_profiles
                    ),
                )
            )
        grid_height = sum(card_rows) + max(0, len(card_rows) - 1) * card_gap
        height = max(
            cls._profile_height(profiles),
            205 + grid_height + 60,
        )
        image = PILImage.new(
            "RGB",
            (CARD_WIDTH, min(MAX_CARD_HEIGHT, height)),
            "#0d1230",
        )
        draw = ImageDraw.Draw(image)
        draw.text((70, 54), "ACM // NEURAL CONTEST NETWORK", font=body_font, fill="#63eaff")
        draw.text((70, 86), "ACM 竞赛战绩卡", font=title_font, fill="#ffffff")
        draw.text((70, 140), f"{display_name} · 账号同步完成", font=subtitle_font, fill="#a8c8e5")
        start_y = 205
        card_w = card_width
        row_y = start_y
        for index, profile in enumerate(profiles):
            col = index % 2
            row = index // 2
            if col == 0 and row > 0:
                row_y += card_rows[row - 1] + card_gap
            x = 70 + col * (card_w + card_gap)
            y = row_y
            card_h = card_rows[row]
            accent = PLATFORM_COLORS.get(profile.platform, ("#55e6ff", "#a855f7"))[0]
            draw.rounded_rectangle(
                (x, y, x + card_w, y + card_h),
                radius=18,
                fill="#111b42",
                outline=accent,
                width=2,
            )
            draw.text(
                (x + 25, y + 20),
                platform_label(profile.platform),
                font=platform_font,
                fill=accent,
            )
            draw.text(
                (x + 25, y + 56),
                profile.handle,
                font=handle_font,
                fill="#ffffff",
            )
            primary_label, primary_value = _primary_metric(profile)
            draw.text(
                (x + 25, y + 100),
                primary_value,
                font=rating_font,
                fill=accent,
            )
            draw.text(
                (x + (310 if single else 270), y + 120),
                primary_label,
                font=body_font,
                fill="#8faac3",
            )
            draw.text(
                (x + (420 if single else 370), y + 120),
                profile.rank_text or profile.color or "未评级",
                font=body_font,
                fill="#c8d8e9",
            )
            details = [
                f"最高 Rating：{_format_number(profile.max_rating)}",
                f"平台排名：{_format_number(profile.rating_rank)}",
                f"参赛次数：{_format_number(profile.contest_count)}",
                f"本次变化：{_format_delta(weekly_changes.get(profile.platform, profile.recent_delta))}",
            ]
            if profile.solved_count is not None:
                details.append(f"通过题数：{_format_number(profile.solved_count)}")
            columns = 4 if single else 2
            detail_width = (card_w - 50) / columns
            detail_top = y + 164
            for line_index, value in enumerate(details):
                detail_col = line_index % columns
                detail_row = line_index // columns
                draw.text(
                    (
                        int(x + 25 + detail_col * detail_width),
                        int(detail_top + detail_row * 27),
                    ),
                    value,
                    font=body_font,
                    fill="#a8d8e5",
                )
            detail_rows = (len(details) + columns - 1) // columns
            meta_y = int(detail_top + detail_rows * 27 + 7)
            if profile.recent_contests and isinstance(profile.recent_contests[0], dict) and profile.recent_contests[0].get("name"):
                recent = profile.recent_contests[0]
                draw.text(
                    (x + 25, meta_y),
                    f"最近：{recent.get('name')} {_format_delta(recent.get('delta'))}",
                    font=body_font,
                    fill="#b7d5eb",
                )
                meta_y += 23
            extra_values = [
                profile.school,
                profile.organization,
                profile.country,
            ]
            if profile.platform == "luogu":
                ccf_level = str(profile.extra.get("ccf_level") or "").strip()
                xcpc_level = str(profile.extra.get("xcpc_level") or "").strip()
                if ccf_level:
                    extra_values.append(f"CCF {ccf_level}")
                if xcpc_level:
                    extra_values.append(f"XCPC {xcpc_level}")
            extras = " · ".join(value for value in extra_values if value)
            if extras:
                draw.text(
                    (x + 25, meta_y),
                    extras,
                    font=body_font,
                    fill="#8faac3",
                )
        draw.text(
            (70, image.height - 42),
            f"生成时间：{_updated_text()} · 仅展示平台公开资料",
            font=body_font,
            fill="#718ba8",
        )
        try:
            image.save(image_path, format="PNG")
        except OSError:
            return False
        return image_path.is_file() and image_path.stat().st_size > 0

    @classmethod
    def _pillow_ranking(
        cls,
        rows: List[Dict[str, Any]],
        title: str,
        subtitle: str,
        metric_label: str,
        note: str,
        image_path: Path,
    ) -> bool:
        try:
            from PIL import Image as PILImage
            from PIL import ImageDraw
        except ImportError:
            return False
        title_font = cls._find_font(40)
        subtitle_font = cls._find_font(19)
        body_font = cls._find_font(19)
        value_font = cls._find_font(28)
        if not all((title_font, subtitle_font, body_font, value_font)):
            return False
        height = max(MIN_CARD_HEIGHT, min(MAX_CARD_HEIGHT, 450 + len(rows) * 82))
        image = PILImage.new("RGB", (CARD_WIDTH, height), "#0d1230")
        draw = ImageDraw.Draw(image)
        draw.text((70, 54), "ACM // GROUP RANKING MATRIX", font=body_font, fill="#63eaff")
        draw.text((70, 88), title, font=title_font, fill="#ffffff")
        draw.text((70, 140), subtitle, font=subtitle_font, fill="#a8c8e5")
        y = 205
        for index, row in enumerate(rows, start=1):
            draw.rounded_rectangle((70, y, CARD_WIDTH - 70, y + 64), radius=12, fill="#111b42", outline="#294a75", width=1)
            draw.text((95, y + 16), f"{index:02d}", font=value_font, fill="#63eaff")
            draw.text((185, y + 11), str(row.get("display_name") or row.get("qq_name") or "未知用户"), font=body_font, fill="#ffffff")
            draw.text((185, y + 38), str(row.get("handle") or "未绑定"), font=subtitle_font, fill="#8faac3")
            draw.text((850, y + 14), _format_number(row.get("display_value", row.get("value"))), font=value_font, fill="#ffffff")
            draw.text((1040, y + 21), _format_delta(row.get("delta")), font=body_font, fill="#61f0ad" if (row.get("delta") or 0) >= 0 else "#ff7899")
            y += 82
        if note:
            draw.text((70, min(y, image.height - 100)), note, font=subtitle_font, fill="#b1cae2")
        draw.text((70, image.height - 42), f"生成时间：{_updated_text()} · 只展示已加入群排行成员", font=subtitle_font, fill="#718ba8")
        try:
            image.save(image_path, format="PNG")
        except OSError:
            return False
        return image_path.is_file() and image_path.stat().st_size > 0

    @classmethod
    def _pillow_overview(
        cls,
        sections: Dict[str, List[Dict[str, Any]]],
        title: str,
        subtitle: str,
        metric_label: str,
        note: str,
        image_path: Path,
    ) -> bool:
        try:
            from PIL import Image as PILImage
            from PIL import ImageDraw
        except ImportError:
            return False
        title_font = cls._find_font(40)
        subtitle_font = cls._find_font(19)
        body_font = cls._find_font(18)
        value_font = cls._find_font(25)
        if not all((title_font, subtitle_font, body_font, value_font)):
            return False
        section_count = max(1, len(sections))
        rows = sum(max(1, min(5, len(value))) for value in sections.values())
        height = max(MIN_CARD_HEIGHT, min(MAX_CARD_HEIGHT, 300 + rows * 70 + section_count * 70))
        image = PILImage.new("RGB", (CARD_WIDTH, height), "#0d1230")
        draw = ImageDraw.Draw(image)
        draw.text((70, 54), "ACM // GROUP RANKING MATRIX", font=body_font, fill="#63eaff")
        draw.text((70, 88), title, font=title_font, fill="#ffffff")
        draw.text((70, 140), subtitle, font=subtitle_font, fill="#a8c8e5")
        columns = 2
        section_w = (CARD_WIDTH - 140 - 24) // columns
        y_base = 205
        for index, (platform, rows_data) in enumerate(sections.items()):
            col = index % columns
            row_index = index // columns
            x = 70 + col * (section_w + 24)
            y = y_base + row_index * (max(1, min(5, len(rows_data))) * 70 + 90)
            accent = PLATFORM_COLORS.get(platform, ("#55e6ff", "#a855f7"))[0]
            section_h = max(78, min(5, len(rows_data)) * 70 + 55)
            draw.rounded_rectangle(
                (x, y, x + section_w, y + section_h),
                radius=16,
                fill="#111b42",
                outline=accent,
                width=2,
            )
            draw.text((x + 20, y + 14), platform_label(platform), font=value_font, fill=accent)
            row_y = y + 55
            for rank, item in enumerate(rows_data[:5], start=1):
                draw.text((x + 20, row_y), f"{rank:02d}", font=body_font, fill="#63eaff")
                draw.text((x + 75, row_y), str(item.get("display_name") or item.get("qq_name") or "未知用户"), font=body_font, fill="#ffffff")
                draw.text((x + section_w - 145, row_y), _format_number(item.get("display_value", item.get("value"))), font=value_font, fill="#ffffff")
                row_y += 70
        draw.text((70, image.height - 42), f"生成时间：{_updated_text()} · {metric_label}", font=subtitle_font, fill="#718ba8")
        try:
            image.save(image_path, format="PNG")
        except OSError:
            return False
        return image_path.is_file() and image_path.stat().st_size > 0
