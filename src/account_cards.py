"""账号战绩卡与群排行卡：樱粉珍珠、冰蓝花瓣主题的 HTML/PNG 渲染。"""
from __future__ import annotations

import hashlib
import html
import io
import base64
import json
import mimetypes
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from .account_models import AccountProfile, platform_label
from .models import CN_TZ
from .output_renderer import AdaptiveOutputRenderer

CARD_FORMAT_VERSION = 8
CARD_WIDTH = 1200
MIN_CARD_HEIGHT = 760
MAX_CARD_HEIGHT = 5200
PROFILE_MIN_RENDER_HEIGHT = 500
PROFILE_PAGE_OVERHEAD = 245
PROFILE_CARD_BASE_HEIGHT = 270
PROFILE_CARD_COMPACT_HEIGHT = 308
PROFILE_CARD_MULTI_HEIGHT = 351
PROFILE_GRID_GAP = 18
RANKING_PAGE_START = 215
RANKING_LIST_OVERHEAD = 21
RANKING_HEADER_HEIGHT = 42
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
OVERVIEW_HEADER_HEIGHT = 28
OVERVIEW_ROW_HEIGHT = 65
OVERVIEW_EMPTY_SECTION_HEIGHT = 134
OVERVIEW_GRID_GAP = 24
OVERVIEW_HEIGHT_SAFETY = 16
OVERVIEW_MIN_RENDER_HEIGHT = 520

PLATFORM_COLORS = {
    "codeforces": ("#df6c9e", "#f4a7c6"),
    "nowcoder": ("#63bfd7", "#b4eaf1"),
    "luogu": ("#c985c7", "#efa8d1"),
    "atcoder": ("#76c9b6", "#b9eddf"),
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


def _profile_field(profile: object, key: str, default=None):
    if isinstance(profile, AccountProfile):
        return getattr(profile, key, default)
    if isinstance(profile, dict):
        return profile.get(key, default)
    return default


def _primary_metric(profile: object) -> tuple[str, str]:
    platform = str(_profile_field(profile, "platform", "") or "")
    rating = _profile_field(profile, "rating")
    rating_rank = _profile_field(profile, "rating_rank")
    if platform == "luogu":
        if rating is not None:
            return "Elo", _format_number(rating)
        if rating_rank is not None:
            return "平台排名", f"#{rating_rank}"
        return "平台排名", "—"
    return "Rating", _format_number(rating)


def _profile_stats(profile: object) -> List[tuple[str, str]]:
    """只生成有实际数据的资料项，避免大量“—”占据卡片空间。"""
    primary_label, _ = _primary_metric(profile)
    stats: List[tuple[str, str]] = []

    rating_rank = _profile_field(profile, "rating_rank")
    rank_value = _format_number(rating_rank) if rating_rank is not None else ""
    # 洛谷没有 Elo 时主指标本身就是平台排名，不重复展示。
    if rank_value and primary_label != "平台排名":
        stats.append(("平台排名", rank_value))

    max_rating = _profile_field(profile, "max_rating")
    if max_rating is not None:
        stats.append(("最高 Rating", _format_number(max_rating)))

    max_rank_text = str(
        _profile_field(profile, "max_rank_text", "") or ""
    ).strip()
    if max_rank_text:
        stats.append(("最高段位", max_rank_text))

    contest_count = _profile_field(profile, "contest_count")
    if contest_count is not None:
        stats.append(("参赛次数", _format_number(contest_count)))

    solved_count = _profile_field(profile, "solved_count")
    if solved_count is not None:
        stats.append(
            (_solved_count_label(profile), _format_number(solved_count))
        )

    contribution = _profile_field(profile, "contribution")
    if contribution is not None:
        stats.append(("贡献", _format_number(contribution)))
    return stats


def _difficulty_items(profile: object) -> List[tuple[str, int]]:
    """读取资料卡使用的难度分布，过滤损坏或空数据。"""
    raw = _profile_field(profile, "difficulty_distribution", []) or []
    if not isinstance(raw, list):
        return []
    items: List[tuple[str, int]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        try:
            count = int(item.get("count"))
        except (TypeError, ValueError):
            continue
        if label and count > 0:
            items.append((label, count))
    return items


def _difficulty_title(profile: object) -> str:
    items = _difficulty_items(profile)
    if not items:
        return ""
    total = sum(count for _, count in items)
    if _difficulty_scan_is_partial(profile):
        extra = _profile_field(profile, "extra", {}) or {}
        scan_limit = extra.get("difficulty_scan_limit")
        return (
            f"CF 做题分布 · 已统计通过 {total} 题"
            f"（最近 {int(scan_limit)} 条提交）"
        )
    return f"CF 做题分布 · 已通过 {total} 题"


def _difficulty_scan_is_partial(profile: object) -> bool:
    extra = _profile_field(profile, "extra", {}) or {}
    if not isinstance(extra, dict):
        return False
    scan_limit = extra.get("difficulty_scan_limit")
    scanned = extra.get("difficulty_scanned_submissions")
    try:
        return bool(
            scan_limit
            and scanned
            and int(scanned) >= int(scan_limit)
        )
    except (TypeError, ValueError):
        return False


def _solved_count_label(profile: object) -> str:
    return "已统计题数" if _difficulty_scan_is_partial(profile) else "通过题数"


def _difficulty_text(profile: object) -> str:
    items = _difficulty_items(profile)
    return " · ".join(f"{label} {count}" for label, count in items)


def _difficulty_lines(
    profile: object,
    *,
    max_units: int,
) -> List[str]:
    title = _difficulty_title(profile)
    if not title:
        return []
    parts = [f"{label} {count}" for label, count in _difficulty_items(profile)]
    lines: List[str] = []
    current = f"{title}："
    for part in parts:
        candidate = f"{current} {part}" if current.endswith("：") else f"{current} · {part}"
        if (
            not current.endswith("：")
            and _text_width_for_layout(candidate) > max_units
        ):
            lines.append(current)
            current = f"  {part}"
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _text_width_for_layout(value: object) -> int:
    text = str(value or "")
    return sum(2 if not char.isascii() else 1 for char in text)


def _profile_extra_text(profile: object) -> str:
    """整理学校、组织和地区信息，去除 CF 学校/组织重复值。"""
    school = str(_profile_field(profile, "school", "") or "").strip()
    organization = str(
        _profile_field(profile, "organization", "") or ""
    ).strip()
    country = str(_profile_field(profile, "country", "") or "").strip()
    city = str(_profile_field(profile, "city", "") or "").strip()
    values = []

    if organization:
        values.append(f"组织：{organization}")
    if school and school.casefold() != organization.casefold():
        values.append(f"学校：{school}")
    location = " · ".join(value for value in (country, city) if value)
    if location:
        values.append(f"地区：{location}")

    if str(_profile_field(profile, "platform", "") or "") == "luogu":
        extra = _profile_field(profile, "extra", {}) or {}
        if isinstance(extra, dict):
            ccf_level = str(extra.get("ccf_level") or "").strip()
            xcpc_level = str(extra.get("xcpc_level") or "").strip()
            if ccf_level:
                values.append(f"CCF {ccf_level}")
            if xcpc_level:
                values.append(f"XCPC {xcpc_level}")
    return " · ".join(values)


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
        self.avatar_cache_dir = self.cache_dir / "avatars"
        self._lock = threading.Lock()

    def render_profile(
        self,
        profiles: Iterable[AccountProfile],
        *,
        display_name: str = "ACM 选手",
        weekly_changes: Optional[Dict[str, Optional[int]]] = None,
        group_ranks: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Optional[Path]:
        profile_list = list(profiles)
        rank_data = group_ranks or {}
        avatar_sources = [
            self._avatar_html_source(profile)
            for profile in profile_list
        ]
        source = {
            "kind": "profile",
            "display_name": display_name,
            "weekly_changes": weekly_changes or {},
            "group_ranks": rank_data,
            "avatar_sources": [
                (
                    hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
                    if value.startswith("data:image/")
                    else value
                )
                for value in avatar_sources
            ],
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
            group_ranks=rank_data,
            avatar_sources=avatar_sources,
        )
        fallback = self._pillow_profile
        return self._render(
            body,
            source,
            fallback,
            profile_list,
            display_name,
            weekly_changes or {},
            rank_data,
            self.avatar_cache_dir,
        )

    def _avatar_html_source(self, profile: AccountProfile) -> str:
        """预加载头像并内嵌为 data URL，避免截图早于远程图片加载。"""
        try:
            normalized = self._normalize_avatar_url(
                profile.avatar_url,
                profile.platform,
            )
            if not normalized:
                return ""
            image = self._load_avatar(
                self.avatar_cache_dir,
                normalized,
                256,
                profile.platform,
            )
            if image is None:
                return self._download_avatar_data_url(normalized)
            try:
                output = io.BytesIO()
                image.save(output, format="PNG")
                encoded = base64.b64encode(output.getvalue()).decode("ascii")
                return f"data:image/png;base64,{encoded}"
            except (OSError, ValueError):
                return self._download_avatar_data_url(normalized)
        except Exception:
            # 头像只是装饰信息，任何解析/下载异常都应回退到占位图。
            return ""

    @staticmethod
    def _download_avatar_data_url(url: str) -> str:
        """无 Pillow 时也预下载头像，避免浏览器截图抢跑。"""
        if url.startswith("data:image/"):
            return url
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 ACM-QQ-Group-Bot "
                        "avatar-renderer"
                    )
                },
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = response.read(5 * 1024 * 1024 + 1)
                content_type = str(
                    response.headers.get("Content-Type") or ""
                ).split(";", 1)[0].strip().lower()
            if len(payload) > 5 * 1024 * 1024:
                return ""
            if not content_type.startswith("image/"):
                content_type = (
                    mimetypes.guess_type(urlparse(url).path)[0]
                    or "image/png"
                )
            encoded = base64.b64encode(payload).decode("ascii")
            return f"data:{content_type};base64,{encoded}"
        except Exception:
            return ""

    def render_ranking(
        self,
        rows: List[Dict[str, Any]],
        *,
        title: str,
        subtitle: str,
        metric_label: str = "Rating",
        note: str = "",
        value_header: Optional[str] = None,
        secondary_label: str = "近7日变化",
        secondary_value_key: str = "delta",
    ) -> Optional[Path]:
        source = {
            "kind": "ranking",
            "title": title,
            "subtitle": subtitle,
            "metric_label": metric_label,
            "note": note,
            "value_header": value_header or f"当前{metric_label}",
            "secondary_label": secondary_label,
            "secondary_value_key": secondary_value_key,
            "rows": rows,
        }
        body = self._ranking_html(
            rows,
            title=title,
            subtitle=subtitle,
            metric_label=metric_label,
            note=note,
            value_header=value_header,
            secondary_label=secondary_label,
            secondary_value_key=secondary_value_key,
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
            value_header,
            secondary_label,
            secondary_value_key,
        )

    def render_overview_ranking(
        self,
        sections: Dict[str, List[Dict[str, Any]]],
        *,
        title: str,
        subtitle: str,
        metric_label: str = "Rating",
        note: str = "",
        secondary_label: str = "近7日变化",
        secondary_value_key: str = "delta",
    ) -> Optional[Path]:
        source = {
            "kind": "overview",
            "title": title,
            "subtitle": subtitle,
            "metric_label": metric_label,
            "note": note,
            "secondary_label": secondary_label,
            "secondary_value_key": secondary_value_key,
            "sections": sections,
        }
        body = self._overview_html(
            sections,
            title=title,
            subtitle=subtitle,
            metric_label=metric_label,
            note=note,
            secondary_label=secondary_label,
            secondary_value_key=secondary_value_key,
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
            secondary_label,
            secondary_value_key,
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

            try:
                height = self._estimate_height(source)
            except Exception:
                height = PROFILE_MIN_RENDER_HEIGHT
            try:
                renderers = AdaptiveOutputRenderer._find_renderers()
            except Exception:
                renderers = []
            for kind, executable in renderers:
                try:
                    image_tmp.unlink(missing_ok=True)
                    if kind == "pillow":
                        success = fallback(*fallback_args, image_tmp)
                    else:
                        success = AdaptiveOutputRenderer._run_external_renderer(
                            kind, executable, html_path, image_tmp, height
                        )
                except Exception:
                    success = False
                if success:
                    try:
                        os.replace(image_tmp, image_path)
                    except OSError:
                        return None
                    return image_path

            try:
                image_tmp.unlink(missing_ok=True)
                fallback_success = fallback(*fallback_args, image_tmp)
            except Exception:
                fallback_success = False
            if fallback_success:
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
            source.get("profiles") or [],
            group_ranks=source.get("group_ranks") or {},
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
        has_group_rank: bool = False,
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

        height = (
            PROFILE_CARD_COMPACT_HEIGHT
            if compact
            else PROFILE_CARD_MULTI_HEIGHT
        )
        stats = _profile_stats(profile)
        stat_rows = max(1, (len(stats) + 1) // 2)
        height += max(0, stat_rows - 2) * 30
        # 多平台卡的排行信息通常会在窄卡片中换到下一行；单平台卡可横向容纳。
        if has_group_rank and not compact:
            height += 33
        difficulty_lines = _difficulty_lines(
            profile,
            max_units=128 if compact else 68,
        )
        if difficulty_lines:
            height += 30 + len(difficulty_lines) * 24

        # 单平台卡片使用整行宽度；这些阈值对应压缩后的 CSS 卡片宽度。
        handle_width = cls._text_width(values["handle"])
        if handle_width > 42:
            height += min(60, ((handle_width - 1) // 42) * 28)

        rank_width = cls._text_width(values["rank"])
        if rank_width > 24:
            height += min(28, ((rank_width - 1) // 24) * 24)

        extras = _profile_extra_text(profile)
        if extras:
            height += min(48, max(0, (cls._text_width(extras) - 1) // 84) * 24)

        latest = (
            recent_contests[0]
            if recent_contests and isinstance(recent_contests[0], dict)
            else {}
        )
        if latest.get("name"):
            latest_width = cls._text_width(latest.get("name"))
            if latest_width > 84:
                height += min(30, ((latest_width - 1) // 84) * 24)
        return height

    @classmethod
    def _profile_height(
        cls,
        profiles: Iterable[object],
        *,
        group_ranks: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> int:
        """按卡片数量和内容估算截图高度，避免固定模板留下大块空白。"""
        profile_list = list(profiles)
        rank_data = group_ranks or {}
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
                            has_group_rank=(
                                (
                                    str(
                                        item.get("platform") or ""
                                    )
                                    if isinstance(item, dict)
                                    else (
                                        item.platform
                                        if isinstance(item, AccountProfile)
                                        else ""
                                    )
                                )
                                in rank_data
                            )
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
    def _normalize_avatar_url(
        value: object,
        platform: str = "",
    ) -> str:
        """规范化公开头像 URL，兼容协议相对地址和平台相对地址。"""
        text = str(value or "").strip()
        if not text:
            return ""
        if text.startswith("//"):
            return "https:" + text
        if text.startswith("/"):
            bases = {
                "codeforces": "https://codeforces.com",
                "nowcoder": "https://ac.nowcoder.com",
                "luogu": "https://www.luogu.com",
                "atcoder": "https://atcoder.jp",
            }
            base = bases.get(platform)
            return f"{base}{text}" if base else ""
        parsed = urlparse(text)
        return text if parsed.scheme in {"http", "https", "data"} else ""

    @staticmethod
    def _load_avatar(
        cache_dir: str | Path,
        url: object,
        size: int,
        platform: str = "",
    ):
        """下载并裁剪头像为正方形；失败时返回 None。"""
        try:
            from PIL import Image, ImageOps
        except ImportError:
            return None
        normalized = AccountCardRenderer._normalize_avatar_url(
            url,
            platform,
        )
        if not normalized or not normalized.startswith(("http://", "https://")):
            return None
        avatar_cache_dir = Path(cache_dir)
        cache_path = avatar_cache_dir / (
            hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
            + ".png"
        )
        try:
            if cache_path.is_file() and cache_path.stat().st_size > 0:
                with Image.open(cache_path) as cached:
                    image = cached.convert("RGB")
            else:
                request = urllib.request.Request(
                    normalized,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 ACM-QQ-Group-Bot "
                            "avatar-renderer"
                        )
                    },
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    payload = response.read(5 * 1024 * 1024 + 1)
                if len(payload) > 5 * 1024 * 1024:
                    return None
                with Image.open(io.BytesIO(payload)) as source:
                    image = source.convert("RGB")
                avatar_cache_dir.mkdir(parents=True, exist_ok=True)
                image.save(cache_path, format="PNG")
            resampling = getattr(Image, "Resampling", Image).LANCZOS
            return ImageOps.fit(
                image,
                (int(size), int(size)),
                method=resampling,
                centering=(0.5, 0.5),
            )
        except Exception:
            return None

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
        list_height = (
            RANKING_LIST_OVERHEAD
            + RANKING_HEADER_HEIGHT
            + rows_height
        )
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
                else (
                    OVERVIEW_SECTION_BASE
                    + OVERVIEW_HEADER_HEIGHT
                    + count * OVERVIEW_ROW_HEIGHT
                )
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
    def _pillow_overview_layout(cls, sections):
        """计算 Pillow 总览的分区位置，避免不同高度分区互相覆盖。"""
        items = list(sections.items())
        section_heights = [
            (
                OVERVIEW_EMPTY_SECTION_HEIGHT
                if not rows
                else max(
                    78,
                    min(5, len(rows)) * 70
                    + 55
                    + OVERVIEW_HEADER_HEIGHT,
                )
            )
            for _, rows in items
        ]
        row_heights = []
        for offset in range(0, len(section_heights), 2):
            row_heights.append(
                max(section_heights[offset : offset + 2])
            )
        row_tops = []
        current_top = 205
        for row_height in row_heights:
            row_tops.append(current_top)
            current_top += row_height + OVERVIEW_GRID_GAP
        return items, section_heights, row_tops

    @classmethod
    def _difficulty_html(cls, profile: object) -> str:
        items = _difficulty_items(profile)
        if not items:
            return ""
        chips = "".join(
            f'<span class="difficulty-chip"><b>{_escape(label)}</b>'
            f'<strong>{_escape(count)}</strong></span>'
            for label, count in items
        )
        return (
            '<div class="difficulty-panel">'
            f'<div class="difficulty-title">{_escape(_difficulty_title(profile))}</div>'
            f'<div class="difficulty-grid">{chips}</div>'
            "</div>"
        )

    @classmethod
    def _profile_html(
        cls,
        profiles: List[AccountProfile],
        *,
        display_name: str,
        weekly_changes: Dict[str, Optional[int]],
        group_ranks: Optional[Dict[str, Dict[str, Any]]] = None,
        avatar_sources: Optional[List[str]] = None,
    ) -> str:
        rank_data = group_ranks or {}
        image_sources = avatar_sources or []
        cards = []
        for index, profile in enumerate(profiles):
            start, end = PLATFORM_COLORS.get(
                profile.platform, ("#df6c9e", "#f4a7c6")
            )
            delta = (
                weekly_changes.get(profile.platform)
                if profile.platform in weekly_changes
                else profile.recent_delta
            )
            delta_class = "positive" if (delta or 0) > 0 else "negative" if (delta or 0) < 0 else ""
            primary_label, primary_value = _primary_metric(profile)
            detail = _profile_stats(profile)
            details_html = "".join(
                f'<div class="stat"><span>{_escape(label)}</span><b>{_escape(value)}</b></div>'
                for label, value in detail
            )
            extras = _profile_extra_text(profile)
            latest = profile.recent_contests[0] if profile.recent_contests else {}
            difficulty_html = cls._difficulty_html(profile)
            latest_text = ""
            if isinstance(latest, dict) and latest.get("name"):
                latest_text = (
                    f'<div class="recent">最近：{_escape(latest.get("name"))}'
                    f' · {_escape(_format_delta(latest.get("delta")))}</div>'
                )
            rank_info = rank_data.get(profile.platform)
            group_rank_text = ""
            if isinstance(rank_info, dict):
                rank = rank_info.get("rank")
                total = rank_info.get("total")
                if rank is not None and total:
                    group_rank_text = (
                        f'<div class="group-rank">本群排行：第 '
                        f'<b>{_escape(rank)}</b> / {_escape(total)} 名</div>'
                    )
                elif rank_info.get("unavailable"):
                    group_rank_text = (
                        '<div class="group-rank muted">本群排行：暂时无法计算</div>'
                    )
                else:
                    group_rank_text = (
                        '<div class="group-rank muted">本群排行：未进入榜单</div>'
                    )
            avatar_url = cls._normalize_avatar_url(
                profile.avatar_url,
                profile.platform,
            )
            if index < len(image_sources) and image_sources[index]:
                avatar_url = image_sources[index]
            avatar_initial = _escape(platform_label(profile.platform)[:1])
            avatar_image = (
                f'<img src="{_escape(avatar_url)}" alt="" '
                'onerror="this.style.display=\'none\'">'
                if avatar_url
                else ""
            )
            cards.append(
                f"""
                <article class="platform-card" style="--accent:{_escape(start)};--accent2:{_escape(end)}">
                  <div class="platform-head">
                    <span class="platform-tag">{_escape(platform_label(profile.platform))}</span>
                    <span class="verified">◆ VERIFIED</span>
                  </div>
                  <div class="profile-main">
                    <div class="identity-line">
                      <div class="avatar-wrap">
                        <span class="avatar-fallback">{avatar_initial}</span>
                        {avatar_image}
                      </div>
                      <div class="identity-copy">
                        <div class="handle">{_escape(profile.handle)}</div>
                      </div>
                    </div>
                    <div class="rating-row">
                      <span class="rating">{_escape(primary_value)}</span>
                      <span class="rating-label">{_escape(primary_label)}</span>
                      <span class="rank">{_escape(profile.rank_text or profile.color or "未评级")}</span>
                    </div>
                  </div>
                  <div class="stats">{details_html}</div>
                  <div class="profile-meta">
                    {difficulty_html}
                    <div class="trend {delta_class}">本次变化：{_escape(_format_delta(delta))}</div>
                    {latest_text}
                    {f'<div class="extra">{_escape(extras)}</div>' if extras else ""}
                    {group_rank_text}
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
        value_header: Optional[str] = None,
        secondary_label: str = "近7日变化",
        secondary_value_key: str = "delta",
    ) -> str:
        rendered = []
        for index, row in enumerate(rows, start=1):
            delta = row.get("delta")
            delta_class = "positive" if (delta or 0) > 0 else "negative" if (delta or 0) < 0 else ""
            secondary_value = row.get(secondary_value_key)
            if secondary_value is None and secondary_value_key != "delta":
                secondary_value = row.get("delta")
            secondary_text = (
                _format_delta(secondary_value)
                if secondary_value_key == "delta"
                else _format_number(secondary_value)
            )
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
                  <div class="rank-delta {delta_class}">{_escape(secondary_text)}</div>
                </div>
                """
            )
        ranking_body = "".join(rendered) or (
            '<div class="empty-card">当前还没有可排行的成员</div>'
        )
        body = (
            '<div class="ranking-list">'
            '<div class="rank-header">'
            "<span>名次</span>"
            "<span>成员 / 账号</span>"
            f"<span>{_escape(value_header or f'当前{metric_label}')}</span>"
            f"<span>{_escape(secondary_label)}</span>"
            "</div>"
            f"{ranking_body}</div>"
        )
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
        secondary_label: str = "近7日变化",
        secondary_value_key: str = "delta",
    ) -> str:
        is_progress = secondary_value_key != "delta"
        blocks = []
        for platform, rows in sections.items():
            start, end = PLATFORM_COLORS.get(
                platform, ("#df6c9e", "#f4a7c6")
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
                current_value = row.get(
                    "current_display_value",
                    row.get("rating"),
                )
                value_text = row.get("display_value", row.get("value"))
                delta_text = (
                    _format_delta(delta)
                    if secondary_value_key == "delta"
                    else _format_number(
                        row.get(
                            secondary_value_key,
                            current_value,
                        )
                    )
                )
                rendered.append(
                    f"""
                    <div class="mini-row">
                      <span class="mini-no">{index:02d}</span>
                      <span class="mini-user"><b>{_escape(row.get("display_name") or row.get("qq_name") or "未知用户")}</b><small>{_escape(row.get("handle") or "")}</small></span>
                      <strong class="mini-value">{_escape(_format_number(value_text))}</strong>
                      <span class="mini-delta {delta_class}">{_escape(delta_text)}</span>
                    </div>
                    """
                )
            if not rendered:
                rendered.append('<div class="mini-empty">暂无数据</div>')
            value_header = (
                metric_label
                if is_progress
                else "当前指标"
            )
            delta_header = secondary_label or "近7日变化"
            blocks.append(
                f"""
                <section class="mini-section" style="--accent:{_escape(start)};--accent2:{_escape(end)}">
                  <h2>{_escape(platform_label(platform))}</h2>
                  <div class="mini-header">
                    <span>名次</span>
                    <span>成员</span>
                    <span>{_escape(value_header)}</span>
                    <span>{_escape(delta_header)}</span>
                  </div>
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
      color:#4c315b;
      font-family:"Noto Sans CJK SC","Microsoft YaHei",Arial,sans-serif;
      background:
        radial-gradient(circle at 8% 12%, rgba(255,190,224,.34), transparent 28%),
        radial-gradient(circle at 92% 18%, rgba(165,225,255,.28), transparent 26%),
        linear-gradient(135deg,#24142f 0%,#382044 48%,#211837 100%);
    }}
    .page {{ width:{CARD_WIDTH}px; margin:0 auto; padding:58px 70px 62px; position:relative; overflow:hidden; }}
    .page:before {{ content:""; position:absolute; inset:0; opacity:.2; pointer-events:none;
      background-image:linear-gradient(120deg,transparent 0 46%,rgba(255,255,255,.18) 47%,transparent 48%),
        linear-gradient(60deg,transparent 0 72%,rgba(255,192,226,.12) 73%,transparent 74%);
      background-size:180px 180px,220px 220px; mask-image:linear-gradient(to bottom,black,transparent 90%); }}
    .page:after {{ content:""; position:absolute; width:360px; height:360px; right:-170px; top:76px;
      border:1px solid rgba(255,214,239,.3); border-radius:50%; box-shadow:0 0 0 18px rgba(255,214,239,.06),
      0 0 0 42px rgba(164,221,255,.05); transform:rotate(28deg); pointer-events:none; }}
    .orb {{ position:absolute; border-radius:50%; filter:blur(4px); opacity:.48; }}
    .orb.one {{ width:220px; height:220px; right:-88px; top:130px; background:#f58cba; box-shadow:0 0 90px #ed78b1; }}
    .orb.two {{ width:170px; height:170px; left:-70px; bottom:100px; background:#8edff4; box-shadow:0 0 80px #71d7ef; }}
    .petal {{ position:absolute; width:34px; height:78px; border-radius:70% 30% 70% 30%; background:linear-gradient(145deg,rgba(255,240,249,.82),rgba(244,128,188,.28)); border:1px solid rgba(255,233,246,.5); transform:rotate(28deg); opacity:.48; pointer-events:none; }}
    .petal.a {{ right:160px; top:92px; }}
    .petal.b {{ right:108px; top:180px; transform:rotate(112deg) scale(.78); }}
    .petal.c {{ left:120px; bottom:140px; transform:rotate(-34deg) scale(.7); }}
    .crystal {{ position:absolute; width:18px; height:18px; border:1px solid rgba(193,237,255,.72); background:rgba(160,227,248,.2); transform:rotate(45deg); box-shadow:0 0 18px rgba(155,225,255,.65); pointer-events:none; }}
    .crystal.a {{ right:250px; top:218px; }}
    .crystal.b {{ left:210px; bottom:92px; transform:rotate(45deg) scale(.65); }}
    .header {{ position:relative; margin-bottom:30px; z-index:1; }}
    .eyebrow {{ color:#ffd2e9; letter-spacing:4px; font-size:15px; font-weight:800; text-shadow:0 0 16px rgba(255,170,216,.45); }}
    h1 {{ margin:8px 0 5px; font-size:44px; letter-spacing:2px; color:#fff8fc; text-shadow:0 3px 20px rgba(255,137,195,.45); }}
    .subtitle {{ color:#f2d8e8; font-size:20px; }}
    .profile-grid {{ position:relative; display:grid; grid-template-columns:1fr 1fr; gap:24px; }}
    .platform-card, .ranking-list, .empty-card {{ position:relative; background:linear-gradient(145deg,rgba(255,252,255,.97),rgba(255,230,244,.92));
      border:1px solid rgba(255,211,235,.9); border-radius:22px; box-shadow:0 18px 42px rgba(20,8,35,.3), inset 0 0 28px rgba(255,255,255,.62); }}
    .platform-card {{ padding:26px 30px 24px; overflow:hidden; }}
    .platform-card:before {{ content:""; position:absolute; left:0; top:0; bottom:0; width:6px; background:linear-gradient(var(--accent),var(--accent2)); box-shadow:0 0 20px var(--accent); }}
    .platform-head {{ display:flex; justify-content:space-between; align-items:center; }}
    .platform-tag {{ color:var(--accent); font-size:20px; font-weight:900; letter-spacing:1px; }}
    .verified {{ color:#ae7d9c; font-size:12px; letter-spacing:1px; }}
    .handle {{ margin-top:13px; font-size:28px; font-weight:900; color:#4b2b5c; overflow-wrap:anywhere; }}
    .rating-row {{ display:flex; align-items:baseline; gap:16px; margin:13px 0 19px; }}
    .rating {{ font-size:48px; line-height:1; font-weight:900; color:var(--accent); text-shadow:0 0 18px color-mix(in srgb,var(--accent),transparent 45%); }}
    .rating-label {{ color:#9d718d; font-size:14px; letter-spacing:1px; }}
    .rank {{ color:#6f4b71; font-size:18px; }}
    .stats {{ display:grid; grid-template-columns:1fr 1fr; gap:10px 18px; }}
    .stat {{ display:flex; justify-content:space-between; gap:10px; color:#a27692; font-size:15px; border-bottom:1px dashed rgba(180,113,157,.26); padding-bottom:7px; }}
    .stat b {{ color:#4f315e; font-size:17px; text-align:right; }}
    .trend {{ margin-top:16px; color:#8d6382; font-size:17px; }}
    .trend.positive, .rank-delta.positive {{ color:#61f0ad; }}
    .trend.negative, .rank-delta.negative {{ color:#ff7899; }}
    .extra {{ margin-top:13px; color:#9a718c; font-size:15px; overflow-wrap:anywhere; }}
    .recent {{ margin-top:13px; color:#80617d; font-size:15px; overflow-wrap:anywhere; }}
    .profile-main {{ position:relative; }}
    .identity-line {{ display:flex; align-items:center; gap:14px; min-width:0; }}
    .identity-copy {{ min-width:0; flex:1; }}
    .avatar-wrap {{ position:relative; flex:0 0 76px; width:76px; height:76px; aspect-ratio:1 / 1; overflow:hidden; border-radius:18px; background:linear-gradient(135deg,var(--accent),#fff1fa 72%); box-shadow:0 0 24px color-mix(in srgb,var(--accent),transparent 58%); }}
    .avatar-wrap:after {{ content:""; position:absolute; inset:0; border:2px solid rgba(255,255,255,.72); border-radius:inherit; pointer-events:none; }}
    .avatar-wrap img {{ position:absolute; inset:0; width:100%; height:100%; object-fit:cover; object-position:center; display:block; transform:scale(1.02); }}
    .avatar-fallback {{ position:absolute; inset:0; display:grid; place-items:center; color:#fff; font-size:32px; font-weight:900; text-shadow:0 2px 8px rgba(91,35,89,.35); }}
    .group-rank {{ flex:1 1 210px; min-width:0; color:#d34f93; font-size:14px; overflow-wrap:anywhere; }}
    .group-rank b {{ color:#6b315f; font-size:17px; }}
    .group-rank.muted {{ color:#a17a95; }}
    .profile-meta {{ position:relative; display:flex; flex-wrap:wrap; align-items:center; gap:8px 18px; margin-top:11px; }}
    .difficulty-panel {{ flex:1 1 100%; min-width:0; padding:10px 12px 11px; border:1px solid rgba(205,145,184,.32); border-radius:12px; background:linear-gradient(105deg,rgba(255,245,251,.76),rgba(228,247,252,.58)); }}
    .difficulty-title {{ color:#8d5f82; font-size:13px; font-weight:800; letter-spacing:.4px; margin-bottom:7px; overflow-wrap:anywhere; }}
    .difficulty-grid {{ display:flex; flex-wrap:wrap; gap:6px; }}
    .difficulty-chip {{ display:inline-flex; align-items:center; gap:7px; padding:5px 8px; border-radius:9px; background:rgba(255,255,255,.76); border:1px solid rgba(224,177,206,.42); color:#8a6a86; font-size:12px; line-height:1.2; }}
    .difficulty-chip b {{ color:#c44786; font-weight:800; }}
    .difficulty-chip strong {{ color:#51315d; font-size:14px; }}
    .profile-meta > .trend,
    .profile-meta > .recent,
    .profile-meta > .extra {{ flex:1 1 210px; min-width:0; margin-top:0; }}
    .ranking-list {{ position:relative; overflow:hidden; padding:10px 24px; }}
    .rank-header {{ display:grid; grid-template-columns:80px 1fr 150px 100px; align-items:center; gap:16px; padding:4px 8px 10px; color:#a17a95; font-size:13px; font-weight:800; letter-spacing:.5px; border-bottom:1px solid rgba(184,113,157,.24); }}
    .rank-header span:nth-child(3),
    .rank-header span:nth-child(4) {{ text-align:right; }}
    .rank-row {{ display:grid; grid-template-columns:80px 1fr 150px 100px; align-items:center; gap:16px; padding:20px 8px; border-bottom:1px solid rgba(184,113,157,.2); }}
    .rank-row:last-child {{ border-bottom:0; }}
    .rank-no {{ color:#d34f93; font-size:28px; font-weight:900; }}
    .rank-user b {{ display:block; color:#4b2b5c; font-size:21px; }}
    .rank-user span {{ display:block; color:#9a718c; margin-top:5px; font-size:15px; overflow-wrap:anywhere; }}
    .rank-value {{ text-align:right; }}
    .rank-value strong {{ display:block; color:#4b2b5c; font-size:27px; }}
    .rank-value small {{ color:#a17a95; font-size:13px; }}
    .rank-delta {{ text-align:right; font-size:19px; font-weight:800; }}
    .overview-grid {{ position:relative; display:grid; grid-template-columns:1fr 1fr; gap:24px; align-items:start; }}
    .mini-section {{ position:relative; overflow:hidden; padding:20px 22px 12px; background:linear-gradient(145deg,rgba(255,252,255,.97),rgba(255,230,244,.92)); border:1px solid rgba(255,211,235,.9); border-radius:20px; box-shadow:0 18px 42px rgba(20,8,35,.3), inset 0 0 28px rgba(255,255,255,.62); }}
    .mini-section:before {{ content:""; position:absolute; left:0; top:0; bottom:0; width:5px; background:linear-gradient(var(--accent),var(--accent2)); box-shadow:0 0 18px var(--accent); }}
    .mini-section h2 {{ margin:0 0 8px; color:var(--accent); font-size:22px; }}
    .mini-header {{ display:grid; grid-template-columns:48px 1fr 90px 72px; align-items:center; gap:8px; min-height:24px; color:#a17a95; font-size:11px; font-weight:800; border-bottom:1px solid rgba(184,113,157,.2); }}
    .mini-header span:nth-child(3),
    .mini-header span:nth-child(4) {{ text-align:right; }}
    .mini-row {{ display:grid; grid-template-columns:48px 1fr 90px 72px; align-items:center; gap:8px; min-height:65px; border-bottom:1px solid rgba(184,113,157,.18); }}
    .mini-row:last-child {{ border-bottom:0; }}
    .mini-no {{ color:#d34f93; font-size:18px; font-weight:800; }}
    .mini-user b {{ display:block; color:#4b2b5c; font-size:16px; overflow-wrap:anywhere; }}
    .mini-user small {{ display:block; color:#9a718c; margin-top:3px; font-size:12px; overflow-wrap:anywhere; }}
    .mini-value {{ color:#4b2b5c; font-size:20px; text-align:right; }}
    .mini-delta {{ color:#9a718c; font-size:14px; text-align:right; }}
    .mini-delta.positive {{ color:#61f0ad; }}
    .mini-delta.negative {{ color:#ff7899; }}
    .mini-empty {{ color:#a17a95; padding:20px 0; }}
    .rank-note {{ position:relative; margin-top:18px; padding:14px 18px; color:#7b5b78; background:linear-gradient(110deg,rgba(255,220,239,.78),rgba(207,239,255,.58)); border:1px solid rgba(255,213,235,.7); border-radius:12px; font-size:16px; }}
    .empty-card {{ padding:48px; color:#a17a95; font-size:20px; text-align:center; }}
    .card-footer {{ position:relative; margin-top:24px; color:#e6bfd7; font-size:14px; letter-spacing:.4px; }}
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
    .profile-document .avatar-wrap {{ flex-basis:90px; width:90px; height:90px; border-radius:21px; }}
    .profile-document .profile-grid.profile-single .avatar-wrap {{ flex-basis:124px; width:124px; height:124px; border-radius:26px; }}
    .profile-document .avatar-fallback {{ font-size:34px; }}
    .profile-document .profile-grid.profile-single .avatar-fallback {{ font-size:48px; }}
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
    <span class="petal a"></span><span class="petal b"></span><span class="petal c"></span>
    <span class="crystal a"></span><span class="crystal b"></span>
    <header class="header">
      <div class="eyebrow">ELYSIAN // PINK PEARL ARCHIVE</div>
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
        group_ranks: Dict[str, Dict[str, Any]],
        avatar_cache_dir: str | Path,
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

        def meta_count(profile: AccountProfile) -> int:
            count = 1  # 本次变化
            if (
                profile.recent_contests
                and isinstance(profile.recent_contests[0], dict)
                and profile.recent_contests[0].get("name")
            ):
                count += 1
            if _profile_extra_text(profile):
                count += 1
            if profile.platform in group_ranks:
                count += 1
            return count

        card_rows = []
        for offset in range(0, len(profiles), 2):
            row_profiles = profiles[offset : offset + 2]
            card_rows.append(
                max(
                    270 if single else 300,
                    max(
                        cls._profile_card_height(
                            profile,
                            compact=single,
                            has_group_rank=profile.platform in group_ranks,
                        )
                        for profile in row_profiles
                    ),
                    max(
                        cls._profile_card_height(
                            profile,
                            compact=single,
                            has_group_rank=profile.platform in group_ranks,
                        )
                        + (
                            max(0, meta_count(profile) - 2) * 23
                            if single
                            else 0
                        )
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
            "#2a193b",
        )
        draw = ImageDraw.Draw(image)
        draw.ellipse(
            (CARD_WIDTH - 260, -90, CARD_WIDTH + 20, 190),
            outline="#f3a9cb",
            width=2,
        )
        draw.ellipse(
            (CARD_WIDTH - 232, -62, CARD_WIDTH - 8, 162),
            outline="#b6e7f0",
            width=2,
        )
        draw.text(
            (70, 54),
            "ELYSIAN // PINK PEARL ARCHIVE",
            font=body_font,
            fill="#ffd5e8",
        )
        draw.text(
            (70, 86),
            "ACM 竞赛战绩卡",
            font=title_font,
            fill="#fff7fb",
        )
        draw.text(
            (70, 140),
            f"{display_name} · 账号同步完成",
            font=subtitle_font,
            fill="#f1d7e7",
        )
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
            accent = PLATFORM_COLORS.get(
                profile.platform,
                ("#df6c9e", "#f4a7c6"),
            )[0]
            draw.rounded_rectangle(
                (x, y, x + card_w, y + card_h),
                radius=18,
                fill="#fff5fb",
                outline=accent,
                width=2,
            )
            draw.text(
                (x + 25, y + 20),
                platform_label(profile.platform),
                font=platform_font,
                fill=accent,
            )
            avatar_size = 96 if single else 64
            avatar_x = x + 25 if single else x + card_w - 25 - avatar_size
            avatar_y = y + 54 if single else y + 18
            avatar = cls._load_avatar(
                avatar_cache_dir,
                profile.avatar_url,
                avatar_size,
                profile.platform,
            )
            if avatar is not None:
                avatar_mask = PILImage.new(
                    "L",
                    (avatar_size, avatar_size),
                    0,
                )
                ImageDraw.Draw(avatar_mask).rounded_rectangle(
                    (0, 0, avatar_size - 1, avatar_size - 1),
                    radius=16,
                    fill=255,
                )
                image.paste(avatar, (avatar_x, avatar_y), avatar_mask)
            else:
                draw.rounded_rectangle(
                    (
                        avatar_x,
                        avatar_y,
                        avatar_x + avatar_size,
                        avatar_y + avatar_size,
                    ),
                    radius=16,
                    fill=accent,
                    outline="#ffffff",
                    width=1,
                )
            initial = platform_label(profile.platform)[:1] or "A"
            if avatar is None:
                draw.text(
                    (
                        avatar_x + avatar_size // 2 - 12,
                        avatar_y + avatar_size // 2 - 18,
                    ),
                    initial,
                    font=platform_font,
                    fill="#ffffff",
                ),
            draw.text(
                (
                    x + 25 + avatar_size + 14
                    if single
                    else x + 25,
                    y + (66 if single else 56),
                ),
                profile.handle,
                font=handle_font,
                fill="#51315d",
            )
            primary_label, primary_value = _primary_metric(profile)
            draw.text(
                (x + 25, y + (160 if single else 100)),
                primary_value,
                font=rating_font,
                fill=accent,
            )
            draw.text(
                (x + (310 if single else 270), y + (180 if single else 120)),
                primary_label,
                font=body_font,
                fill="#a27692",
            )
            draw.text(
                (x + (420 if single else 370), y + (180 if single else 120)),
                profile.rank_text or profile.color or "未评级",
                font=body_font,
                fill="#765477",
            )
            details = [
                f"{label}：{value}"
                for label, value in _profile_stats(profile)
            ]
            columns = 4 if single else 2
            detail_width = (card_w - 50) / columns
            detail_top = y + (220 if single else 164)
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
                    fill="#8d6683",
                )
            detail_rows = (len(details) + columns - 1) // columns
            meta_y = int(detail_top + detail_rows * 27 + 7)
            change_value = weekly_changes.get(
                profile.platform,
                profile.recent_delta,
            )
            draw.text(
                (x + 25, meta_y),
                f"本次变化：{_format_delta(change_value)}",
                font=body_font,
                fill="#61b98b" if (change_value or 0) >= 0 else "#e26c91",
            )
            meta_y += 23
            difficulty_lines = _difficulty_lines(
                profile,
                max_units=128 if single else 68,
            )
            for line in difficulty_lines:
                draw.text(
                    (x + 25, meta_y),
                    line,
                    font=body_font,
                    fill="#8d6683",
                )
                meta_y += 23
            if profile.recent_contests and isinstance(profile.recent_contests[0], dict) and profile.recent_contests[0].get("name"):
                recent = profile.recent_contests[0]
                draw.text(
                    (x + 25, meta_y),
                    f"最近：{recent.get('name')} {_format_delta(recent.get('delta'))}",
                    font=body_font,
                    fill="#80617d",
                )
                meta_y += 23
            extras = _profile_extra_text(profile)
            if extras:
                draw.text(
                    (x + 25, meta_y),
                    extras,
                    font=body_font,
                    fill="#9a718c",
                )
                meta_y += 23
            rank_info = group_ranks.get(profile.platform)
            if isinstance(rank_info, dict):
                rank = rank_info.get("rank")
                total = rank_info.get("total")
                if rank is not None and total:
                    rank_text = f"本群排行：第 {rank} / {total} 名"
                elif rank_info.get("unavailable"):
                    rank_text = "本群排行：暂时无法计算"
                else:
                    rank_text = "本群排行：未进入榜单"
                draw.text(
                    (x + 25, meta_y),
                    rank_text,
                    font=body_font,
                    fill="#d34f93" if rank is not None else "#9a718c",
                )
        draw.text(
            (70, image.height - 42),
            f"生成时间：{_updated_text()} · 仅展示平台公开资料",
            font=body_font,
            fill="#e3b9d2",
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
        value_header: Optional[str],
        secondary_label: str,
        secondary_value_key: str,
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
        height = max(
            MIN_CARD_HEIGHT,
            min(
                MAX_CARD_HEIGHT,
                450 + RANKING_HEADER_HEIGHT + len(rows) * 82,
            ),
        )
        image = PILImage.new("RGB", (CARD_WIDTH, height), "#2a193b")
        draw = ImageDraw.Draw(image)
        draw.ellipse(
            (CARD_WIDTH - 260, -90, CARD_WIDTH + 20, 190),
            outline="#f3a9cb",
            width=2,
        )
        draw.ellipse(
            (CARD_WIDTH - 232, -62, CARD_WIDTH - 8, 162),
            outline="#b6e7f0",
            width=2,
        )
        draw.text(
            (70, 54),
            "ELYSIAN // PINK PEARL ARCHIVE",
            font=body_font,
            fill="#ffd5e8",
        )
        draw.text((70, 88), title, font=title_font, fill="#fff7fb")
        draw.text((70, 140), subtitle, font=subtitle_font, fill="#f1d7e7")
        y = 205
        header_fill = "#a17a95"
        draw.text((95, y), "名次", font=subtitle_font, fill=header_fill)
        draw.text((185, y), "成员 / 账号", font=subtitle_font, fill=header_fill)
        draw.text(
            (850, y),
            value_header or f"当前{metric_label}",
            font=subtitle_font,
            fill=header_fill,
        )
        draw.text(
            (1040, y),
            secondary_label,
            font=subtitle_font,
            fill=header_fill,
        )
        y += RANKING_HEADER_HEIGHT
        for index, row in enumerate(rows, start=1):
            draw.rounded_rectangle(
                (70, y, CARD_WIDTH - 70, y + 64),
                radius=12,
                fill="#fff5fb",
                outline="#efc5dc",
                width=1,
            )
            draw.text((95, y + 16), f"{index:02d}", font=value_font, fill="#d34f93")
            draw.text(
                (185, y + 11),
                str(row.get("display_name") or row.get("qq_name") or "未知用户"),
                font=body_font,
                fill="#51315d",
            )
            draw.text(
                (185, y + 38),
                str(row.get("handle") or "未绑定"),
                font=subtitle_font,
                fill="#9a718c",
            )
            draw.text(
                (850, y + 14),
                _format_number(row.get("display_value", row.get("value"))),
                font=value_font,
                fill="#51315d",
            )
            secondary_value = row.get(secondary_value_key)
            if secondary_value is None and secondary_value_key != "delta":
                secondary_value = row.get("delta")
            secondary_text = (
                _format_delta(secondary_value)
                if secondary_value_key == "delta"
                else _format_number(secondary_value)
            )
            draw.text(
                (1040, y + 21),
                secondary_text,
                font=body_font,
                fill="#61f0ad"
                if (row.get("delta") or 0) >= 0
                else "#ff7899",
            )
            y += 82
        if note:
            draw.text(
                (70, min(y, image.height - 100)),
                note,
                font=subtitle_font,
                fill="#80617d",
            )
        draw.text(
            (70, image.height - 42),
            f"生成时间：{_updated_text()} · 只展示已加入群排行成员",
            font=subtitle_font,
            fill="#e3b9d2",
        )
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
        secondary_label: str,
        secondary_value_key: str,
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
        items, section_heights, row_tops = cls._pillow_overview_layout(
            sections
        )
        height = cls._overview_height(sections, note=note)
        image = PILImage.new(
            "RGB",
            (CARD_WIDTH, min(MAX_CARD_HEIGHT, height)),
            "#2a193b",
        )
        draw = ImageDraw.Draw(image)
        draw.ellipse(
            (CARD_WIDTH - 260, -90, CARD_WIDTH + 20, 190),
            outline="#f3a9cb",
            width=2,
        )
        draw.ellipse(
            (CARD_WIDTH - 232, -62, CARD_WIDTH - 8, 162),
            outline="#b6e7f0",
            width=2,
        )
        draw.text(
            (70, 54),
            "ELYSIAN // PINK PEARL ARCHIVE",
            font=body_font,
            fill="#ffd5e8",
        )
        draw.text((70, 88), title, font=title_font, fill="#fff7fb")
        draw.text((70, 140), subtitle, font=subtitle_font, fill="#f1d7e7")
        columns = 2
        is_progress = secondary_value_key != "delta"
        section_w = (CARD_WIDTH - 140 - 24) // columns
        for index, (platform, rows_data) in enumerate(items):
            col = index % columns
            row_index = index // columns
            x = 70 + col * (section_w + 24)
            y = row_tops[row_index]
            accent = PLATFORM_COLORS.get(
                platform,
                ("#df6c9e", "#f4a7c6"),
            )[0]
            section_h = section_heights[index]
            draw.rounded_rectangle(
                (x, y, x + section_w, y + section_h),
                radius=16,
                fill="#fff5fb",
                outline=accent,
                width=2,
            )
            draw.text((x + 20, y + 14), platform_label(platform), font=value_font, fill=accent)
            header_fill = "#a17a95"
            value_header = (
                metric_label
                if is_progress
                else "当前指标"
            )
            delta_header = secondary_label or "近7日变化"
            draw.text((x + 20, y + 43), "名次", font=subtitle_font, fill=header_fill)
            draw.text((x + 75, y + 43), "成员", font=subtitle_font, fill=header_fill)
            draw.text(
                (x + section_w - 205, y + 43),
                value_header,
                font=subtitle_font,
                fill=header_fill,
            )
            draw.text(
                (x + section_w - 95, y + 43),
                delta_header,
                font=subtitle_font,
                fill=header_fill,
            )
            row_y = y + 78
            for rank, item in enumerate(rows_data[:5], start=1):
                draw.text((x + 20, row_y), f"{rank:02d}", font=body_font, fill="#d34f93")
                draw.text(
                    (x + 75, row_y),
                    str(item.get("display_name") or item.get("qq_name") or "未知用户"),
                    font=body_font,
                    fill="#51315d",
                )
                draw.text(
                    (x + section_w - 205, row_y),
                    _format_number(item.get("display_value", item.get("value"))),
                    font=value_font,
                    fill="#51315d",
                )
                current_value = item.get(
                    "current_display_value",
                    item.get("rating"),
                )
                delta_value = (
                    item.get(secondary_value_key, current_value)
                    if is_progress
                    else _format_delta(item.get("delta"))
                )
                draw.text(
                    (x + section_w - 95, row_y + 3),
                    (
                        _format_number(delta_value)
                        if is_progress
                        else str(delta_value)
                    ),
                    font=subtitle_font,
                    fill="#80617d",
                )
                row_y += 70
        if note:
            draw.text(
                (70, max(205, image.height - 112)),
                note,
                font=subtitle_font,
                fill="#80617d",
            )
        draw.text(
            (70, image.height - 42),
            f"生成时间：{_updated_text()} · {metric_label}",
            font=subtitle_font,
            fill="#e3b9d2",
        )
        try:
            image.save(image_path, format="PNG")
        except OSError:
            return False
        return image_path.is_file() and image_path.stat().st_size > 0
