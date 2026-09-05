"""账号战绩卡与群排行卡：樱粉珍珠、冰蓝花瓣主题的 HTML/PNG 渲染。"""
from __future__ import annotations

import hashlib
import html
import io
import base64
import json
import mimetypes
import os
import shutil
import subprocess
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

CARD_FORMAT_VERSION = 16
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
RANKING_HEADER_HEIGHT = 50
RANKING_ROW_HEIGHT = 112
RANKING_PILLOW_ROW_STEP = 90
RANKING_NOTE_MARGIN = 18
RANKING_NOTE_PADDING = 28
RANKING_NOTE_LINE_HEIGHT = 24
RANKING_FOOTER_OVERHEAD = 44
RANKING_PAGE_BOTTOM = 62
RANKING_HEIGHT_SAFETY = 24
RANKING_MIN_RENDER_HEIGHT = 520
OVERVIEW_PAGE_START = 215
OVERVIEW_SECTION_BASE = 74
OVERVIEW_HEADER_HEIGHT = 36
OVERVIEW_ROW_HEIGHT = 74
OVERVIEW_PILLOW_ROW_STEP = 78
OVERVIEW_EMPTY_SECTION_HEIGHT = 134
OVERVIEW_GRID_GAP = 24
OVERVIEW_HEIGHT_SAFETY = 16
OVERVIEW_MIN_RENDER_HEIGHT = 520
DIFFICULTY_CHART_COLUMNS = 2
DIFFICULTY_CHART_TITLE_HEIGHT = 32
DIFFICULTY_CHART_ROW_HEIGHT = 27
DIFFICULTY_CHART_BOTTOM_PADDING = 16
RATING_CHART_HEIGHT = 150
RATING_CHART_DISPLAY_LIMIT = 8

PLATFORM_COLORS = {
    "codeforces": ("#df6c9e", "#f4a7c6"),
    "nowcoder": ("#63bfd7", "#b4eaf1"),
    "luogu": ("#c985c7", "#efa8d1"),
    "atcoder": ("#76c9b6", "#b9eddf"),
}

PLATFORM_TEXT_COLORS = {
    "codeforces": "#b33a72",
    "nowcoder": "#16758d",
    "luogu": "#8b4d8d",
    "atcoder": "#1f806d",
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


def current_metric_header(metric_label: object) -> str:
    """把内部指标名转换为排行表头，避免使用含义不明的“当前指标”。"""
    label = str(metric_label or "").strip()
    if not label or label in {"当前指标", "近7日变化"}:
        label = "Rating"
    if label.startswith("当前"):
        return label
    if label in {"Rating", "Elo"}:
        return f"当前 {label}"
    if label == "平台排名":
        return "当前平台排名"
    if label == "Elo / 平台排名":
        return "当前 Elo / 平台排名"
    return f"当前 {label}"


def _resolved_value_header(
    value_header: object,
    rows: Iterable[object],
    *,
    metric_label: str,
    platform: str = "",
) -> str:
    value = str(value_header or "").strip()
    if not value or value == "当前指标":
        return current_metric_header(
            rank_metric_label_for_rows(
                rows,
                platform=platform,
                fallback=metric_label,
            )
        )
    return value


def _resolved_secondary_header(
    secondary_label: object,
    rows: Iterable[object],
    *,
    metric_label: str,
    secondary_value_key: str,
) -> str:
    value = str(secondary_label or "").strip()
    if (
        secondary_value_key != "delta"
        or not value
        or value == "当前指标"
    ):
        return current_metric_header(
            rank_metric_label_for_rows(
                rows,
                fallback=metric_label,
            )
        )
    return value


def rank_metric_label_for_rows(
    rows: Iterable[object],
    *,
    platform: str = "",
    fallback: str = "Rating",
) -> str:
    """从排行行数据中取实际指标名；总览按分区分别解析。"""
    labels = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = str(
            row.get("current_metric_label")
            or row.get("metric_label")
            or ""
        ).strip()
        if platform == "luogu" and label == "Rating":
            # 旧缓存曾把洛谷 Elo 记录成 Rating，展示时按实际含义纠正。
            label = "Elo"
        if label in {"", "当前指标", "近7日变化"}:
            continue
        if label not in labels:
            labels.append(label)
    if len(labels) == 1:
        return labels[0]
    if len(labels) > 1:
        if set(labels) == {"Elo", "平台排名"}:
            return "Elo / 平台排名"
        return "各平台对应指标"
    if platform == "luogu":
        return "Elo / 平台排名"
    return str(fallback or "Rating").strip() or "Rating"


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
    analysis = _profile_field(profile, "analysis", {}) or {}
    if isinstance(analysis, dict):
        submission_count = analysis.get("submission_count")
        if submission_count is not None and not any(
            label == "提交次数" for label, _ in stats
        ):
            stats.append(("提交次数", _format_number(submission_count)))
        challenged_count = analysis.get("challenged_count")
        if challenged_count is not None:
            stats.append(("挑战题数", _format_number(challenged_count)))
        acceptance_rate = analysis.get("acceptance_rate")
        if acceptance_rate is not None:
            try:
                stats.append(("提交通过率", f"{float(acceptance_rate):.1f}%"))
            except (TypeError, ValueError):
                stats.append(("提交通过率", _format_number(acceptance_rate)))
        problem_acceptance_rate = analysis.get("problem_acceptance_rate")
        if problem_acceptance_rate is not None:
            try:
                stats.append(
                    ("题目通过率", f"{float(problem_acceptance_rate):.1f}%")
                )
            except (TypeError, ValueError):
                stats.append(
                    ("题目通过率", _format_number(problem_acceptance_rate))
                )
        active_days_30 = analysis.get("active_days_30")
        if active_days_30 is not None:
            stats.append(("近30天活跃", f"{_format_number(active_days_30)}天"))
    # 同一资料卡最多补充一组摘要分析，避免四个平台都绑定时统计项过密。
    deduplicated: List[tuple[str, str]] = []
    seen_labels = set()
    for label, value in stats:
        if label in seen_labels:
            continue
        seen_labels.add(label)
        deduplicated.append((label, value))
    return deduplicated


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
    analysis = _profile_field(profile, "analysis", {}) or {}
    if isinstance(analysis, dict):
        title = str(analysis.get("difficulty_title") or "").strip()
        if title:
            return f"{title} · {total} 题"
    if _difficulty_scan_is_partial(profile):
        extra = _profile_field(profile, "extra", {}) or {}
        scan_limit = extra.get("difficulty_scan_limit")
        return (
            f"CF 做题分布 · 已统计通过 {total} 题"
            f"（最近 {int(scan_limit)} 条提交）"
        )
    return f"CF 做题分布 · 已通过 {total} 题"


def _rating_history_values(profile: object, limit: int = 8) -> List[int]:
    raw = _profile_field(profile, "rating_history", []) or []
    if not isinstance(raw, list):
        return []
    values = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        try:
            rating = int(item.get("rating"))
        except (TypeError, ValueError):
            continue
        try:
            timestamp = float(item.get("timestamp") or index)
        except (TypeError, ValueError):
            timestamp = float(index)
        values.append((timestamp, index, rating))
    values.sort(key=lambda item: (item[0], item[1]))
    return [rating for _, _, rating in values[-max(2, int(limit)):]]


def _difficulty_chart_height(profile: object) -> int:
    items = _difficulty_items(profile)
    if not items:
        return 0
    return _distribution_chart_height(items)


def _distribution_chart_height(items: List[tuple[str, int]]) -> int:
    if not items:
        return 0
    rows = (
        len(items) + DIFFICULTY_CHART_COLUMNS - 1
    ) // DIFFICULTY_CHART_COLUMNS
    return (
        DIFFICULTY_CHART_TITLE_HEIGHT
        + rows * DIFFICULTY_CHART_ROW_HEIGHT
        + DIFFICULTY_CHART_BOTTOM_PADDING
    )


def _rating_chart_height(profile: object) -> int:
    return (
        RATING_CHART_HEIGHT
        if len(_rating_history_values(profile, RATING_CHART_DISPLAY_LIMIT)) >= 2
        else 0
    )


def _analysis_distribution_items(profile: object) -> List[tuple[str, int]]:
    analysis = _profile_field(profile, "analysis", {}) or {}
    raw = analysis.get("category_distribution") if isinstance(analysis, dict) else []
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


def _analysis_distribution_title(profile: object) -> str:
    analysis = _profile_field(profile, "analysis", {}) or {}
    if not isinstance(analysis, dict):
        return ""
    return str(analysis.get("category_title") or "").strip()


def _analysis_summary_items(
    profile: object,
) -> List[tuple[str, str]]:
    analysis = _profile_field(profile, "analysis", {}) or {}
    if not isinstance(analysis, dict):
        return []
    raw = analysis.get("summary")
    if not isinstance(raw, list):
        return []
    items: List[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        value = str(item.get("value") or "").strip()
        if label and value:
            items.append((label, value))
    return items


def _analysis_language_items(
    profile: object,
) -> List[tuple[str, int]]:
    analysis = _profile_field(profile, "analysis", {}) or {}
    raw = analysis.get("language_distribution") if isinstance(analysis, dict) else []
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


def _analysis_score_items(
    profile: object,
) -> List[tuple[str, int]]:
    analysis = _profile_field(profile, "analysis", {}) or {}
    raw = analysis.get("score_distribution") if isinstance(analysis, dict) else []
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


def _analysis_activity_items(
    profile: object,
) -> List[tuple[str, int]]:
    analysis = _profile_field(profile, "analysis", {}) or {}
    raw = analysis.get("activity_distribution") if isinstance(analysis, dict) else []
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


def _analysis_activity_title(profile: object) -> str:
    analysis = _profile_field(profile, "analysis", {}) or {}
    if not isinstance(analysis, dict):
        return ""
    return str(analysis.get("activity_title") or "").strip()


def _analysis_chart_items(
    profile: object,
) -> tuple[List[tuple[str, int]], str]:
    difficulty = _difficulty_items(profile)
    if difficulty:
        return difficulty, _difficulty_title(profile)
    activity = _analysis_activity_items(profile)
    if activity:
        return (
            activity,
            _analysis_activity_title(profile) or "活跃度分析",
        )
    category = _analysis_distribution_items(profile)
    if category:
        return (
            category,
            _analysis_distribution_title(profile) or "数据分布",
        )
    return [], ""


def _analysis_secondary_chart_items(
    profile: object,
) -> tuple[List[tuple[str, int]], str]:
    # 牛客/AtCoder 的难度图已经是题目级主分析，知识点/系列只在摘要
    # 中展示，避免单个平台卡片堆叠三张图。
    if _difficulty_items(profile):
        return [], ""
    primary, _ = _analysis_chart_items(profile)
    category = _analysis_distribution_items(profile)
    if category and primary != category:
        return (
            category,
            _analysis_distribution_title(profile) or "数据分布",
        )
    language = _analysis_language_items(profile)
    if language and primary != language:
        return language, "语言分布"
    return [], ""


def _analysis_chart_height(profile: object) -> int:
    items, _ = _analysis_chart_items(profile)
    return _distribution_chart_height(items)


def _analysis_secondary_chart_height(profile: object) -> int:
    items, _ = _analysis_secondary_chart_items(profile)
    return _distribution_chart_height(items)


def _analysis_source_text(profile: object) -> str:
    analysis = _profile_field(profile, "analysis", {}) or {}
    if not isinstance(analysis, dict):
        return ""
    source = str(analysis.get("source") or "").strip()
    coverage = str(analysis.get("coverage") or "").strip()
    if source and coverage:
        return f"数据源：{source} · {coverage}"
    if source:
        return f"数据源：{source}"
    return coverage


def _rating_chart_title(profile: object) -> str:
    return (
        "Elo 趋势"
        if _profile_field(profile, "platform", "") == "luogu"
        else "Rating 趋势"
    )


def _analysis_summary_height(profile: object) -> int:
    summary = _analysis_summary_items(profile)
    source_text = _analysis_source_text(profile)
    language_items = _analysis_language_items(profile)
    category_items = _analysis_distribution_items(profile)
    score_items = _analysis_score_items(profile)
    category_summary = (
        bool(category_items)
        and not _analysis_secondary_chart_items(profile)[0]
    )
    if (
        not summary
        and not source_text
        and not language_items
        and not category_summary
        and not score_items
    ):
        return 0
    chip_rows = max(1, (len(summary) + 3) // 4) if summary else 0
    height = 40 + chip_rows * 27
    if language_items or category_summary or score_items:
        height += 25
    if source_text:
        source_lines = max(1, min(3, (_text_width_for_layout(source_text) + 84) // 85))
        height += 10 + source_lines * 17
    return height


def _text_width_for_layout(value: object) -> int:
    text = str(value or "")
    return sum(2 if not char.isascii() else 1 for char in text)


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
            "value_header": _resolved_value_header(
                value_header,
                rows,
                metric_label=metric_label,
            ),
            "secondary_label": _resolved_secondary_header(
                secondary_label,
                rows,
                metric_label=metric_label,
                secondary_value_key=secondary_value_key,
            ),
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
        stat_columns = 4 if compact else 2
        stat_rows = max(
            1,
            (len(stats) + stat_columns - 1) // stat_columns,
        )
        height += max(0, stat_rows - 2) * 30
        # 多平台卡的排行信息通常会在窄卡片中换到下一行；单平台卡可横向容纳。
        if has_group_rank and not compact:
            height += 33
        analysis_height = _analysis_chart_height(profile)
        if analysis_height:
            height += analysis_height + 10
        secondary_chart_height = _analysis_secondary_chart_height(profile)
        if secondary_chart_height:
            height += secondary_chart_height + 10
        rating_height = _rating_chart_height(profile)
        if rating_height:
            height += rating_height + 10
        summary_height = _analysis_summary_height(profile)
        if summary_height:
            height += summary_height + 10

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

    @staticmethod
    def _pillow_text_width(value: object, font) -> float:
        """读取 Pillow 字体的实际绘制宽度。"""
        text = "" if value is None else str(value)
        try:
            return float(font.getlength(text))
        except (AttributeError, TypeError, ValueError):
            try:
                left, _, right, _ = font.getbbox(text)
                return float(max(0, right - left))
            except (AttributeError, TypeError, ValueError):
                return float(len(text))

    @classmethod
    def _fit_rank_pillow_text(
        cls,
        value: object,
        font,
        max_width: float,
        *,
        ellipsis: str = "…",
    ) -> str:
        """把 Pillow 文本限制在列宽内，避免长昵称压住相邻列。"""
        text = "" if value is None else str(value)
        if max_width <= 0:
            return ""
        if cls._pillow_text_width(text, font) <= max_width:
            return text
        if cls._pillow_text_width(ellipsis, font) > max_width:
            return ""
        fitted = ellipsis
        for index in range(1, len(text) + 1):
            candidate = text[:index] + ellipsis
            if cls._pillow_text_width(candidate, font) <= max_width:
                fitted = candidate
            else:
                break
        return fitted

    @classmethod
    def _overview_row_height(cls, row: object) -> int:
        """按总览小卡片中的昵称/账号换行情况估算行高。"""
        if not isinstance(row, dict):
            return OVERVIEW_ROW_HEIGHT
        name_width = cls._ranking_text_width(
            row.get("display_name") or row.get("qq_name") or "未知用户"
        )
        handle_width = cls._ranking_text_width(row.get("handle") or "")
        name_lines = max(1, (name_width + 15) // 16)
        handle_lines = max(1, (handle_width + 19) // 20)
        content_height = name_lines * 25 + 4 + handle_lines * 19
        return max(OVERVIEW_ROW_HEIGHT, content_height + 14)

    @classmethod
    def _ranking_row_height(cls, row: object) -> int:
        """估算排行行高，避免长昵称换行后挤压下一行。"""
        if not isinstance(row, dict):
            return RANKING_ROW_HEIGHT
        display_name = cls._ranking_text_width(
            row.get("display_name") or row.get("qq_name") or "未知用户"
        )
        handle = cls._ranking_text_width(row.get("handle") or "未绑定")
        # 普通排行的成员列会随指标列变宽而收窄，按新的字号估算
        # 44/64 个中英文混排单位，避免昵称或账号换行后覆盖下一行。
        name_lines = max(1, (display_name + 43) // 44)
        handle_lines = max(1, (handle + 63) // 64)
        user_height = name_lines * 34 + 6 + handle_lines * 24
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
                    + sum(
                        cls._overview_row_height(row)
                        for row in rows[:5]
                    )
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
                    min(5, len(rows)) * OVERVIEW_PILLOW_ROW_STEP
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
        items, title = _analysis_chart_items(profile)
        if not items:
            return ""
        maximum = max(count for _, count in items)
        bars = []
        for label, count in items:
            percent = count / maximum * 100 if maximum else 0
            row_class = (
                " difficulty-unknown"
                if label in {"未标分", "未标星", "未标难度", "未建模", "未标注"}
                else ""
            )
            bars.append(
                f'<div class="difficulty-row{row_class}">'
                f'<span class="difficulty-label">{_escape(label)}</span>'
                '<span class="difficulty-track">'
                f'<i class="difficulty-fill" style="width:{percent:.2f}%"></i>'
                "</span>"
                f'<strong class="difficulty-count">{_escape(count)}</strong>'
                "</div>"
            )
        return (
            '<div class="difficulty-panel">'
            f'<div class="difficulty-title">{_escape(title)}</div>'
            f'<div class="difficulty-bars">{"".join(bars)}</div>'
            "</div>"
        )

    @classmethod
    def _secondary_analysis_html(cls, profile: object) -> str:
        items, title = _analysis_secondary_chart_items(profile)
        if not items:
            return ""
        maximum = max(count for _, count in items)
        bars = []
        for label, count in items:
            percent = count / maximum * 100 if maximum else 0
            bars.append(
                '<div class="difficulty-row">'
                f'<span class="difficulty-label">{_escape(label)}</span>'
                '<span class="difficulty-track">'
                f'<i class="difficulty-fill secondary-fill" style="width:{percent:.2f}%"></i>'
                "</span>"
                f'<strong class="difficulty-count">{_escape(count)}</strong>'
                "</div>"
            )
        return (
            '<div class="difficulty-panel analysis-panel">'
            f'<div class="difficulty-title">{_escape(title)}</div>'
            f'<div class="difficulty-bars">{"".join(bars)}</div>'
            "</div>"
        )

    @classmethod
    def _analysis_summary_html(cls, profile: object) -> str:
        summary = _analysis_summary_items(profile)
        source_text = _analysis_source_text(profile)
        language_items = _analysis_language_items(profile)
        category_items = _analysis_distribution_items(profile)
        score_items = _analysis_score_items(profile)
        category_summary = (
            bool(category_items)
            and not _analysis_secondary_chart_items(profile)[0]
        )
        if (
            not summary
            and not source_text
            and not language_items
            and not category_summary
            and not score_items
        ):
            return ""
        chips = "".join(
            f'<span class="analysis-chip"><b>{_escape(label)}</b>'
            f'<strong>{_escape(value)}</strong></span>'
            for label, value in summary
        )
        details = []
        if language_items:
            details.append(
                "常用语言："
                + " · ".join(
                    f"{label} {count}"
                    for label, count in language_items[:3]
                )
            )
        if category_summary:
            details.append(
                "分类/知识点："
                + " · ".join(
                    f"{label} {count}"
                    for label, count in category_items[:3]
                )
            )
        if score_items:
            details.append(
                "资料分项："
                + " · ".join(
                    f"{label} {count}"
                    for label, count in score_items[:3]
                )
            )
        detail_html = (
            f'<div class="analysis-detail">{_escape(" · ".join(details))}</div>'
            if details
            else ""
        )
        source_html = (
            f'<div class="analysis-source">{_escape(source_text)}</div>'
            if source_text
            else ""
        )
        return (
            '<div class="analysis-summary">'
            '<div class="difficulty-title">数据分析摘要</div>'
            f'<div class="analysis-chips">{chips}</div>'
            f"{detail_html}{source_html}"
            "</div>"
        )

    @classmethod
    def _rating_history_html(cls, profile: object) -> str:
        values = _rating_history_values(profile, RATING_CHART_DISPLAY_LIMIT)
        if len(values) < 2:
            return ""

        minimum = min(values)
        maximum = max(values)
        span = maximum - minimum
        padding = max(25, int(span * 0.12))
        lower = minimum - padding
        upper = maximum + padding
        scale = max(1, upper - lower)
        point_coords = []
        for index, value in enumerate(values):
            x = 10 + 80 * index / (len(values) - 1)
            y = 46 - 34 * (value - lower) / scale
            point_coords.append((x, y, value))
        point_text = " ".join(
            f"{x:.2f},{y:.2f}" for x, y, _ in point_coords
        )
        area_text = f"{point_text} 90,48 10,48"
        circles = "".join(
            f'<circle class="rating-chart-dot" cx="{x:.2f}" cy="{y:.2f}" r="1.7">'
            f"<title>第 {index + 1} 场：{value}</title></circle>"
            for index, (x, y, value) in enumerate(point_coords)
        )
        return (
            '<div class="rating-chart">'
            '<div class="rating-chart-head">'
            f'<span class="rating-chart-title">{_escape(_rating_chart_title(profile))}</span>'
            f'<small>最近 {len(values)} 场</small>'
            "</div>"
            '<svg class="rating-chart-svg" viewBox="0 0 100 58" '
            f'preserveAspectRatio="none" role="img" aria-label="{_escape(_rating_chart_title(profile))}">'
            '<line class="rating-chart-gridline" x1="10" y1="12" x2="90" y2="12"></line>'
            '<line class="rating-chart-gridline" x1="10" y1="29" x2="90" y2="29"></line>'
            '<line class="rating-chart-gridline" x1="10" y1="46" x2="90" y2="46"></line>'
            f'<polygon class="rating-chart-area" points="{area_text}"></polygon>'
            f'<polyline class="rating-chart-line" points="{point_text}"></polyline>'
            f"{circles}"
            "</svg>"
            '<div class="rating-chart-scale">'
            f"<span>{_escape(minimum)}</span>"
            f"<b>最新 {_escape(values[-1])}</b>"
            f"<span>{_escape(maximum)}</span>"
            "</div>"
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
            text_accent = PLATFORM_TEXT_COLORS.get(
                profile.platform,
                "#8b4d8d",
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
            secondary_analysis_html = cls._secondary_analysis_html(profile)
            analysis_summary_html = cls._analysis_summary_html(profile)
            rating_history_html = cls._rating_history_html(profile)
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
                <article class="platform-card" style="--accent:{_escape(start)};--accent2:{_escape(end)};--accent-text:{_escape(text_accent)}">
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
                    {secondary_analysis_html}
                    {rating_history_html}
                    {analysis_summary_html}
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
        resolved_header = _resolved_value_header(
            value_header,
            rows,
            metric_label=metric_label,
        )
        body = (
            '<div class="ranking-list">'
            '<div class="rank-header">'
            "<span>名次</span>"
            "<span>成员 / 账号</span>"
            f"<span>{_escape(resolved_header)}</span>"
            f"<span>{_escape(_resolved_secondary_header(secondary_label, rows, metric_label=metric_label, secondary_value_key=secondary_value_key))}</span>"
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
            text_accent = PLATFORM_TEXT_COLORS.get(
                platform,
                "#8b4d8d",
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
            section_metric_label = rank_metric_label_for_rows(
                rows,
                platform=platform,
                fallback=(
                    "Rating"
                    if is_progress
                    else metric_label
                ),
            )
            value_header = (
                metric_label
                if is_progress
                else current_metric_header(section_metric_label)
            )
            delta_header = _resolved_secondary_header(
                secondary_label,
                rows,
                metric_label=section_metric_label,
                secondary_value_key=secondary_value_key,
            ) if is_progress else secondary_label or "近7日变化"
            blocks.append(
                f"""
                <section class="mini-section" style="--accent:{_escape(start)};--accent2:{_escape(end)};--accent-text:{_escape(text_accent)}">
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
      font-family:"Noto Sans CJK SC","Noto Sans CJK TC","Noto Sans","Microsoft YaHei",Arial,sans-serif;
      -webkit-font-smoothing:antialiased;
      text-rendering:optimizeLegibility;
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
    .eyebrow {{ color:#fff0f7; letter-spacing:4px; font-size:15px; font-weight:800; text-shadow:0 0 16px rgba(255,170,216,.55); }}
    h1 {{ margin:8px 0 5px; font-size:44px; letter-spacing:2px; color:#fff8fc; text-shadow:0 3px 20px rgba(255,137,195,.45); }}
    .subtitle {{ color:#ffe7f2; font-size:20px; text-shadow:0 1px 8px rgba(28,12,43,.28); }}
    .profile-grid {{ position:relative; display:grid; grid-template-columns:1fr 1fr; gap:24px; }}
    .platform-card, .ranking-list, .empty-card {{ position:relative; background:linear-gradient(145deg,rgba(255,252,255,.97),rgba(255,230,244,.92));
      border:1px solid rgba(255,211,235,.9); border-radius:22px; box-shadow:0 18px 42px rgba(20,8,35,.3), inset 0 0 28px rgba(255,255,255,.62); }}
    .platform-card {{ padding:26px 30px 24px; overflow:hidden; }}
    .platform-card:before {{ content:""; position:absolute; left:0; top:0; bottom:0; width:6px; background:linear-gradient(var(--accent),var(--accent2)); box-shadow:0 0 20px var(--accent); }}
    .platform-head {{ display:flex; justify-content:space-between; align-items:center; }}
    .platform-tag {{ color:var(--accent-text,var(--accent)); font-size:20px; font-weight:900; letter-spacing:1px; }}
    .verified {{ color:#784b70; font-size:12px; letter-spacing:1px; font-weight:800; }}
    .handle {{ margin-top:13px; font-size:28px; font-weight:900; color:#4b2b5c; overflow-wrap:anywhere; }}
    .rating-row {{ display:flex; align-items:baseline; gap:16px; margin:13px 0 19px; }}
    .rating {{ font-size:48px; line-height:1; font-weight:900; color:var(--accent-text,var(--accent)); text-shadow:0 0 18px color-mix(in srgb,var(--accent),transparent 45%); }}
    .rating-label {{ color:#704966; font-size:14px; letter-spacing:1px; font-weight:700; }}
    .rank {{ color:#5e3b5d; font-size:18px; font-weight:700; }}
    .stats {{ display:grid; grid-template-columns:1fr 1fr; gap:10px 18px; }}
    .stat {{ display:flex; justify-content:space-between; gap:10px; color:#6e4a67; font-size:15px; font-weight:600; border-bottom:1px dashed rgba(180,113,157,.3); padding-bottom:7px; }}
    .stat b {{ color:#452852; font-size:17px; font-weight:800; text-align:right; }}
    .trend {{ margin-top:16px; color:#6b4564; font-size:17px; font-weight:600; }}
    .trend.positive, .rank-delta.positive {{ color:#16835f; }}
    .trend.negative, .rank-delta.negative {{ color:#c03d66; }}
    .extra {{ margin-top:13px; color:#6e4a67; font-size:15px; font-weight:600; overflow-wrap:anywhere; }}
    .recent {{ margin-top:13px; color:#6f4b69; font-size:15px; font-weight:600; overflow-wrap:anywhere; }}
    .profile-main {{ position:relative; }}
    .identity-line {{ display:flex; align-items:center; gap:14px; min-width:0; }}
    .identity-copy {{ min-width:0; flex:1; }}
    .avatar-wrap {{ position:relative; flex:0 0 76px; width:76px; height:76px; aspect-ratio:1 / 1; overflow:hidden; border-radius:18px; background:linear-gradient(135deg,var(--accent),#fff1fa 72%); box-shadow:0 0 24px color-mix(in srgb,var(--accent),transparent 58%); }}
    .avatar-wrap:after {{ content:""; position:absolute; inset:0; border:2px solid rgba(255,255,255,.72); border-radius:inherit; pointer-events:none; }}
    .avatar-wrap img {{ position:absolute; inset:0; width:100%; height:100%; object-fit:cover; object-position:center; display:block; transform:scale(1.02); }}
    .avatar-fallback {{ position:absolute; inset:0; display:grid; place-items:center; color:#fff; font-size:32px; font-weight:900; text-shadow:0 2px 8px rgba(91,35,89,.35); }}
    .group-rank {{ flex:1 1 210px; min-width:0; color:#a92b73; font-size:14px; font-weight:700; overflow-wrap:anywhere; }}
    .group-rank b {{ color:#6b315f; font-size:17px; }}
    .group-rank.muted {{ color:#6e4a67; }}
    .profile-meta {{ position:relative; display:flex; flex-wrap:wrap; align-items:center; gap:8px 18px; margin-top:11px; }}
    .difficulty-panel, .rating-chart {{ flex:1 1 100%; min-width:0; padding:12px 14px 13px; border:1px solid rgba(205,145,184,.4); border-radius:14px; background:linear-gradient(105deg,rgba(255,245,251,.84),rgba(228,247,252,.66)); }}
    .difficulty-title {{ display:flex; align-items:center; color:#6b4564; font-size:14px; line-height:1.35; font-weight:800; letter-spacing:.2px; margin-bottom:9px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
    .difficulty-bars {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:5px 14px; }}
    .difficulty-row {{ display:grid; grid-template-columns:90px minmax(48px,1fr) 42px; align-items:center; gap:8px; min-width:0; min-height:22px; }}
    .difficulty-label {{ color:#6e4a67; font-size:12px; font-weight:600; white-space:nowrap; }}
    .difficulty-track {{ display:block; height:10px; min-width:0; overflow:hidden; border-radius:99px; background:rgba(216,183,207,.34); box-shadow:inset 0 1px 2px rgba(112,70,107,.12); }}
    .difficulty-fill {{ display:block; height:100%; min-width:4px; border-radius:inherit; background:linear-gradient(90deg,var(--accent),var(--accent2)); box-shadow:0 0 8px color-mix(in srgb,var(--accent),transparent 52%); }}
    .difficulty-count {{ color:#452852; font-size:14px; font-weight:800; text-align:right; }}
    .difficulty-unknown .difficulty-label {{ color:#4d7890; }}
    .difficulty-unknown .difficulty-fill {{ background:linear-gradient(90deg,#6ea6b7,#b4eaf1); box-shadow:0 0 8px rgba(110,166,183,.35); }}
    .analysis-panel {{ background:linear-gradient(105deg,rgba(248,246,255,.84),rgba(228,247,252,.66)); }}
    .secondary-fill {{ background:linear-gradient(90deg,#9d7fd1,#c6b5ed); }}
    .analysis-summary {{ flex:1 1 100%; min-width:0; padding:11px 14px 12px; border:1px solid rgba(205,145,184,.34); border-radius:14px; background:linear-gradient(105deg,rgba(255,248,252,.82),rgba(242,239,255,.7)); }}
    .analysis-chips {{ display:flex; flex-wrap:wrap; gap:6px 8px; margin-top:7px; }}
    .analysis-chip {{ display:inline-flex; align-items:center; gap:6px; padding:4px 8px; border-radius:9px; background:rgba(255,255,255,.78); border:1px solid rgba(224,177,206,.42); color:#6e4a67; font-size:12px; line-height:1.2; }}
    .analysis-chip b {{ color:#754b94; font-weight:700; }}
    .analysis-chip strong {{ color:#452852; font-size:13px; font-weight:800; }}
    .analysis-detail {{ margin-top:8px; color:#6e4a67; font-size:12px; font-weight:600; overflow-wrap:anywhere; }}
    .analysis-source {{ margin-top:7px; color:#655669; font-size:11px; line-height:1.45; overflow-wrap:anywhere; }}
    .rating-chart {{ padding-bottom:10px; }}
    .rating-chart-head {{ display:flex; justify-content:space-between; align-items:baseline; gap:12px; margin-bottom:5px; }}
    .rating-chart-title {{ color:#6b4564; font-size:14px; font-weight:800; letter-spacing:.2px; }}
    .rating-chart-head small {{ color:#6f4b69; font-size:12px; font-weight:600; }}
    .rating-chart-svg {{ display:block; width:100%; height:78px; overflow:visible; color:var(--accent); }}
    .rating-chart-gridline {{ stroke:rgba(159,117,148,.22); stroke-width:.7; vector-effect:non-scaling-stroke; }}
    .rating-chart-area {{ fill:color-mix(in srgb,var(--accent),transparent 82%); }}
    .rating-chart-line {{ fill:none; stroke:var(--accent); stroke-width:2.8; stroke-linecap:round; stroke-linejoin:round; vector-effect:non-scaling-stroke; filter:drop-shadow(0 2px 3px color-mix(in srgb,var(--accent),transparent 60%)); }}
    .rating-chart-dot {{ fill:#fffafd; stroke:var(--accent); stroke-width:1.8; vector-effect:non-scaling-stroke; }}
    .rating-chart-scale {{ display:flex; justify-content:space-between; align-items:center; gap:8px; margin-top:2px; color:#6e4a67; font-size:11px; font-weight:600; }}
    .rating-chart-scale b {{ color:#5e3b5d; font-size:12px; }}
    .profile-meta > .trend,
    .profile-meta > .recent,
    .profile-meta > .extra {{ flex:1 1 210px; min-width:0; margin-top:0; }}
    .ranking-list {{ position:relative; overflow:hidden; padding:14px 24px 16px; }}
    .rank-header {{ display:grid; grid-template-columns:72px minmax(0,1fr) 180px 140px; align-items:center; gap:18px; min-height:50px; padding:6px 14px 12px; color:#5e3b5d; font-size:16px; line-height:1.25; font-weight:900; letter-spacing:.2px; border-bottom:1px solid rgba(184,113,157,.34); }}
    .rank-header span {{ min-width:0; white-space:nowrap; }}
    .rank-header span:nth-child(3),
    .rank-header span:nth-child(4) {{ text-align:right; }}
    .rank-row {{ display:grid; grid-template-columns:72px minmax(0,1fr) 180px 140px; align-items:center; gap:18px; min-height:112px; padding:22px 14px; border-bottom:1px solid rgba(184,113,157,.24); }}
    .rank-row:last-child {{ border-bottom:0; }}
    .rank-no {{ color:#d34f93; font-size:30px; line-height:1; font-weight:900; }}
    .rank-user {{ min-width:0; }}
    .rank-user b {{ display:block; color:#4b2b5c; font-size:22px; line-height:1.3; font-weight:800; letter-spacing:.25px; overflow-wrap:anywhere; word-break:break-word; }}
    .rank-user span {{ display:block; color:#6f4b69; margin-top:7px; font-size:16px; line-height:1.35; letter-spacing:.3px; overflow-wrap:anywhere; word-break:break-word; }}
    .rank-value {{ min-width:0; text-align:right; white-space:nowrap; }}
    .rank-value strong {{ display:block; color:#4b2b5c; font-size:31px; line-height:1.05; font-weight:900; letter-spacing:.25px; }}
    .rank-value small {{ color:#6f4b69; font-size:14px; line-height:1.3; font-weight:700; letter-spacing:.25px; }}
    .rank-delta {{ min-width:0; color:#6e4a67; text-align:right; font-size:21px; line-height:1.2; font-weight:800; letter-spacing:.3px; white-space:nowrap; }}
    .overview-grid {{ position:relative; display:grid; grid-template-columns:1fr 1fr; gap:24px; align-items:start; }}
    .mini-section {{ position:relative; overflow:hidden; padding:20px 22px 12px; background:linear-gradient(145deg,rgba(255,252,255,.97),rgba(255,230,244,.92)); border:1px solid rgba(255,211,235,.9); border-radius:20px; box-shadow:0 18px 42px rgba(20,8,35,.3), inset 0 0 28px rgba(255,255,255,.62); }}
    .mini-section:before {{ content:""; position:absolute; left:0; top:0; bottom:0; width:5px; background:linear-gradient(var(--accent),var(--accent2)); box-shadow:0 0 18px var(--accent); }}
    .mini-section h2 {{ margin:0 0 8px; color:var(--accent-text,var(--accent)); font-size:23px; line-height:1.2; }}
    .mini-header {{ display:grid; grid-template-columns:50px minmax(0,1fr) 155px 110px; align-items:center; gap:10px; min-height:36px; padding-bottom:6px; color:#5e3b5d; font-size:13px; line-height:1.25; font-weight:900; letter-spacing:.1px; border-bottom:1px solid rgba(184,113,157,.3); }}
    .mini-header span {{ min-width:0; }}
    .mini-header span:nth-child(3),
    .mini-header span:nth-child(4) {{ text-align:right; }}
    .mini-row {{ display:grid; grid-template-columns:50px minmax(0,1fr) 155px 110px; align-items:center; gap:10px; min-height:74px; border-bottom:1px solid rgba(184,113,157,.22); }}
    .mini-row:last-child {{ border-bottom:0; }}
    .mini-no {{ color:#d34f93; font-size:20px; line-height:1.1; font-weight:800; }}
    .mini-user {{ min-width:0; }}
    .mini-user b {{ display:block; color:#4b2b5c; font-size:17px; line-height:1.35; font-weight:800; letter-spacing:.25px; overflow-wrap:anywhere; word-break:break-word; }}
    .mini-user small {{ display:block; color:#6f4b69; margin-top:4px; font-size:13px; line-height:1.3; letter-spacing:.25px; overflow-wrap:anywhere; word-break:break-word; }}
    .mini-value {{ color:#4b2b5c; font-size:22px; line-height:1.1; font-weight:800; letter-spacing:.3px; text-align:right; white-space:nowrap; }}
    .mini-delta {{ color:#6e4a67; font-size:15px; line-height:1.25; font-weight:700; letter-spacing:.25px; text-align:right; white-space:nowrap; }}
    .mini-delta.positive {{ color:#16835f; }}
    .mini-delta.negative {{ color:#c03d66; }}
    .mini-empty {{ color:#6e4a67; padding:20px 0; }}
    .rank-note {{ position:relative; margin-top:18px; padding:14px 18px; color:#5b3a58; background:linear-gradient(110deg,rgba(255,220,239,.86),rgba(207,239,255,.72)); border:1px solid rgba(255,213,235,.82); border-radius:12px; font-size:16px; }}
    .empty-card {{ padding:48px; color:#6e4a67; font-size:20px; text-align:center; }}
    .card-footer {{ position:relative; margin-top:24px; color:#f0c8df; font-size:14px; letter-spacing:.4px; text-shadow:0 1px 7px rgba(28,12,43,.35); }}
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
      grid-template-columns:repeat(4,minmax(0,1fr));
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
    .profile-document .difficulty-row {{ grid-template-columns:96px minmax(80px,1fr) 46px; }}
    .profile-document .difficulty-label {{ font-size:13px; }}
    .profile-document .difficulty-count {{ font-size:15px; }}
    .profile-document .rating-chart-svg {{ height:84px; }}
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
    def _find_font(size: int, *, bold: bool = False):
        try:
            from PIL import ImageFont
        except ImportError:
            return None

        font_path, font_index = AccountCardRenderer._find_cjk_font_spec(
            bold=bold
        )
        if not font_path:
            return None
        try:
            return ImageFont.truetype(
                font_path,
                size,
                index=font_index,
            )
        except (OSError, ValueError):
            return None

    @staticmethod
    def _find_cjk_font_spec(*, bold: bool = False) -> tuple[Optional[str], int]:
        """优先选择真正的简体中文 CJK 字体及其 TTC 字体索引。"""
        configured = os.environ.get("ACMER_QQ_BOT_FONT")
        if configured and Path(configured).is_file():
            try:
                index = int(os.environ.get("ACMER_QQ_BOT_FONT_INDEX", "0"))
            except ValueError:
                index = 0
            return configured, max(0, index)

        fc_match = shutil.which("fc-match")
        if fc_match:
            style = "Bold" if bold else "Regular"
            try:
                result = subprocess.run(
                    [
                        fc_match,
                        "-f",
                        "%{file}|%{index}",
                        f"Noto Sans CJK SC:style={style}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                path_text, _, index_text = result.stdout.strip().partition("|")
                if result.returncode == 0 and Path(path_text).is_file():
                    try:
                        index = int(index_text or "0")
                    except ValueError:
                        index = 0
                    return path_text, max(0, index)
            except (OSError, subprocess.SubprocessError):
                pass

        if bold:
            candidates = [
                (
                    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
                    2,
                ),
                (
                    "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc",
                    2,
                ),
                (
                    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                    2,
                ),
                (
                    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
                    0,
                ),
            ]
        else:
            candidates = [
                (
                    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                    2,
                ),
                (
                    "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc",
                    2,
                ),
                (
                    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
                    0,
                ),
                (
                    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
                    0,
                ),
            ]
        for path, index in candidates:
            if Path(path).is_file():
                return path, index
        return None, 0

    @staticmethod
    def _fit_pillow_text(draw, value: object, font, max_width: int) -> str:
        """在固定宽度的图表标题中截断超长文本，避免压到相邻内容。"""
        text = str(value or "")
        if not text:
            return ""
        try:
            if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
                return text
            suffix = "…"
            while text and draw.textbbox(
                (0, 0),
                text + suffix,
                font=font,
            )[2] > max_width:
                text = text[:-1]
            return (text + suffix) if text else suffix
        except (AttributeError, TypeError):
            return text

    @classmethod
    def _pillow_distribution_chart(
        cls,
        draw,
        profile: object,
        items: List[tuple[str, int]],
        title: str,
        x: int,
        y: int,
        width: int,
        *,
        title_font,
        label_font,
        value_font,
    ) -> int:
        height = _distribution_chart_height(items)
        if not items or not height:
            return 0

        maximum = max(count for _, count in items)
        draw.rounded_rectangle(
            (x, y, x + width, y + height),
            radius=14,
            fill="#fff8fc",
            outline="#e0bfd7",
            width=1,
        )
        title = cls._fit_pillow_text(
            draw,
            title,
            title_font,
            max(80, width - 24),
        )
        draw.text((x + 12, y + 7), title, font=title_font, fill="#6b4564")

        columns = DIFFICULTY_CHART_COLUMNS if len(items) > 1 else 1
        column_gap = 14
        column_width = max(
            100,
            (width - 24 - column_gap * (columns - 1)) // columns,
        )
        label_width = 78 if width < 700 else 96
        value_width = 42 if width < 700 else 48
        bar_width = max(
            28,
            column_width - label_width - value_width - 16,
        )
        content_top = y + DIFFICULTY_CHART_TITLE_HEIGHT - 1
        for index, (label, count) in enumerate(items):
            column = index % columns
            row = index // columns
            cell_x = x + 12 + column * (column_width + column_gap)
            row_y = content_top + row * DIFFICULTY_CHART_ROW_HEIGHT
            unknown = label in {
                "未标分",
                "未标星",
                "未标难度",
                "未建模",
                "未标注",
            }
            label_color = "#4d7890" if unknown else "#6e4a67"
            draw.text(
                (cell_x, row_y + 3),
                label,
                font=label_font,
                fill=label_color,
            )
            bar_x = cell_x + label_width
            bar_y = row_y + 9
            draw.rounded_rectangle(
                (bar_x, bar_y, bar_x + bar_width, bar_y + 9),
                radius=5,
                fill="#ead9e5",
            )
            fill_width = max(
                4,
                round(bar_width * count / maximum),
            ) if maximum else 0
            fill_color = (
                "#83b8c5"
                if unknown
                else PLATFORM_COLORS.get(
                    str(_profile_field(profile, "platform", "") or ""),
                    ("#d34f93", "#f4a7c6"),
                )[0]
            )
            draw.rounded_rectangle(
                (bar_x, bar_y, bar_x + min(bar_width, fill_width), bar_y + 9),
                radius=5,
                fill=fill_color,
            )
            count_box = draw.textbbox((0, 0), str(count), font=value_font)
            draw.text(
                (
                    cell_x + column_width - value_width
                    + max(0, value_width - (count_box[2] - count_box[0])),
                    row_y + 2,
                ),
                str(count),
                font=value_font,
                fill="#452852",
            )
        return height

    @classmethod
    def _pillow_difficulty_chart(
        cls,
        draw,
        profile: object,
        x: int,
        y: int,
        width: int,
        *,
        title_font,
        label_font,
        value_font,
    ) -> int:
        items, title = _analysis_chart_items(profile)
        return cls._pillow_distribution_chart(
            draw,
            profile,
            items,
            title,
            x,
            y,
            width,
            title_font=title_font,
            label_font=label_font,
            value_font=value_font,
        )

    @classmethod
    def _pillow_secondary_chart(
        cls,
        draw,
        profile: object,
        x: int,
        y: int,
        width: int,
        *,
        title_font,
        label_font,
        value_font,
    ) -> int:
        items, title = _analysis_secondary_chart_items(profile)
        return cls._pillow_distribution_chart(
            draw,
            profile,
            items,
            title,
            x,
            y,
            width,
            title_font=title_font,
            label_font=label_font,
            value_font=value_font,
        )

    @classmethod
    def _pillow_analysis_summary(
        cls,
        draw,
        profile: object,
        x: int,
        y: int,
        width: int,
        *,
        title_font,
        label_font,
    ) -> int:
        summary = _analysis_summary_items(profile)
        source_text = _analysis_source_text(profile)
        language_items = _analysis_language_items(profile)
        category_items = _analysis_distribution_items(profile)
        score_items = _analysis_score_items(profile)
        category_summary = (
            bool(category_items)
            and not _analysis_secondary_chart_items(profile)[0]
        )
        height = _analysis_summary_height(profile)
        if height <= 0:
            return 0

        draw.rounded_rectangle(
            (x, y, x + width, y + height),
            radius=14,
            fill="#fff8fc",
            outline="#e0bfd7",
            width=1,
        )
        draw.text(
            (x + 12, y + 7),
            "数据分析摘要",
            font=title_font,
            fill="#6b4564",
        )
        if summary:
            columns = min(4, len(summary))
            column_gap = 8
            column_width = max(
                80,
                (width - 24 - column_gap * (columns - 1)) // columns,
            )
            for index, (label, value) in enumerate(summary):
                column = index % columns
                row = index // columns
                cell_x = x + 12 + column * (column_width + column_gap)
                cell_y = y + 32 + row * 27
                text = f"{label} {value}"
                text = cls._fit_pillow_text(
                    draw,
                    text,
                    label_font,
                    column_width,
                )
                draw.text(
                    (cell_x, cell_y),
                    text,
                    font=label_font,
                    fill="#5e3b5d",
                )

        detail_y = y + 32 + (
            max(1, (len(summary) + 3) // 4) * 27
            if summary
            else 0
        )
        detail_parts = []
        if language_items:
            detail_parts.append(
                "常用语言：" + " · ".join(
                    f"{label} {count}"
                    for label, count in language_items[:3]
                )
            )
        if category_summary:
            detail_parts.append(
                "分类/知识点：" + " · ".join(
                    f"{label} {count}"
                    for label, count in category_items[:3]
                )
            )
        if score_items:
            detail_parts.append(
                "资料分项：" + " · ".join(
                    f"{label} {count}"
                    for label, count in score_items[:3]
                )
            )
        if detail_parts:
            draw.text(
                (x + 12, detail_y),
                cls._fit_pillow_text(
                    draw,
                    " · ".join(detail_parts),
                    label_font,
                    width - 24,
                ),
                font=label_font,
                fill="#6e4a67",
            )
            detail_y += 25

        if source_text:
            source_lines = []
            current = ""
            for char in source_text:
                candidate = current + char
                if (
                    current
                    and draw.textbbox(
                        (0, 0),
                        candidate,
                        font=label_font,
                    )[2]
                    > width - 24
                ):
                    source_lines.append(current)
                    current = char
                else:
                    current = candidate
            if current:
                source_lines.append(current)
            for line in source_lines[:3]:
                draw.text(
                    (x + 12, detail_y),
                    line,
                    font=label_font,
                    fill="#655669",
                )
                detail_y += 17
        return height

    @classmethod
    def _pillow_rating_chart(
        cls,
        draw,
        profile: object,
        x: int,
        y: int,
        width: int,
        *,
        title_font,
        label_font,
    ) -> int:
        values = _rating_history_values(profile, RATING_CHART_DISPLAY_LIMIT)
        height = _rating_chart_height(profile)
        if len(values) < 2 or not height:
            return 0

        minimum = min(values)
        maximum = max(values)
        span = maximum - minimum
        padding = max(25, int(span * 0.12))
        lower = minimum - padding
        upper = maximum + padding
        scale = max(1, upper - lower)
        accent = PLATFORM_COLORS.get(
            str(_profile_field(profile, "platform", "") or ""),
            ("#d34f93", "#f4a7c6"),
        )[0]

        draw.rounded_rectangle(
            (x, y, x + width, y + height),
            radius=14,
            fill="#fff8fc",
            outline="#e0bfd7",
            width=1,
        )
        draw.text(
            (x + 12, y + 8),
            _rating_chart_title(profile),
            font=title_font,
            fill="#6b4564",
        )
        recent_text = f"最近 {len(values)} 场"
        recent_box = draw.textbbox((0, 0), recent_text, font=label_font)
        draw.text(
            (
                x + width - 12 - (recent_box[2] - recent_box[0]),
                y + 10,
            ),
            recent_text,
            font=label_font,
            fill="#6f4b69",
        )

        left = x + 18
        right = x + width - 18
        top = y + 37
        bottom = y + 95
        chart_width = max(40, right - left)
        chart_height = bottom - top
        points = []
        for index, value in enumerate(values):
            point_x = left + chart_width * index / (len(values) - 1)
            point_y = bottom - chart_height * (value - lower) / scale
            points.append((round(point_x), round(point_y)))

        for ratio in (0, 0.5, 1):
            grid_y = round(bottom - chart_height * ratio)
            draw.line(
                (left, grid_y, right, grid_y),
                fill="#e7d9e4",
                width=1,
            )
        draw.polygon(
            points + [(right, bottom), (left, bottom)],
            fill="#f8e3ef",
        )
        draw.line(points, fill=accent, width=3)
        for point_x, point_y in points:
            draw.ellipse(
                (
                    point_x - 4,
                    point_y - 4,
                    point_x + 4,
                    point_y + 4,
                ),
                fill="#fffafd",
                outline=accent,
                width=2,
            )

        minimum_text = str(minimum)
        latest_text = f"最新 {values[-1]}"
        maximum_text = str(maximum)
        draw.text((left, y + 105), minimum_text, font=label_font, fill="#6e4a67")
        latest_box = draw.textbbox((0, 0), latest_text, font=label_font)
        draw.text(
            (
                x + (width - (latest_box[2] - latest_box[0])) // 2,
                y + 105,
            ),
            latest_text,
            font=label_font,
            fill="#5e3b5d",
        )
        maximum_box = draw.textbbox((0, 0), maximum_text, font=label_font)
        draw.text(
            (
                x + width - 18 - (maximum_box[2] - maximum_box[0]),
                y + 105,
            ),
            maximum_text,
            font=label_font,
            fill="#6e4a67",
        )
        return height

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
        title_font = cls._find_font(42, bold=True)
        subtitle_font = cls._find_font(20)
        platform_font = cls._find_font(22, bold=True)
        handle_font = cls._find_font(28, bold=True)
        rating_font = cls._find_font(48, bold=True)
        body_font = cls._find_font(17)
        chart_title_font = cls._find_font(15, bold=True)
        chart_label_font = cls._find_font(13)
        chart_value_font = cls._find_font(14, bold=True)
        if not all(
            (
                title_font,
                subtitle_font,
                platform_font,
                handle_font,
                rating_font,
                body_font,
                chart_title_font,
                chart_label_font,
                chart_value_font,
            )
        ):
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
            fill="#ffe7f2",
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
            fill="#ffe7f2",
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
            text_accent = PLATFORM_TEXT_COLORS.get(
                profile.platform,
                "#8b4d8d",
            )
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
                fill=text_accent,
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
                fill=text_accent,
            )
            draw.text(
                (x + (310 if single else 270), y + (180 if single else 120)),
                primary_label,
                font=body_font,
                fill="#704966",
            )
            draw.text(
                (x + (420 if single else 370), y + (180 if single else 120)),
                profile.rank_text or profile.color or "未评级",
                font=body_font,
                fill="#5e3b5d",
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
                    fill="#6e4a67",
                )
            detail_rows = (len(details) + columns - 1) // columns
            meta_y = int(detail_top + detail_rows * 27 + 7)
            chart_width = card_w - 50
            difficulty_height = cls._pillow_difficulty_chart(
                draw,
                profile,
                x + 25,
                meta_y,
                chart_width,
                title_font=chart_title_font,
                label_font=chart_label_font,
                value_font=chart_value_font,
            )
            if difficulty_height:
                meta_y += difficulty_height + 10
            secondary_height = cls._pillow_secondary_chart(
                draw,
                profile,
                x + 25,
                meta_y,
                chart_width,
                title_font=chart_title_font,
                label_font=chart_label_font,
                value_font=chart_value_font,
            )
            if secondary_height:
                meta_y += secondary_height + 10
            rating_height = cls._pillow_rating_chart(
                draw,
                profile,
                x + 25,
                meta_y,
                chart_width,
                title_font=chart_title_font,
                label_font=chart_label_font,
            )
            if rating_height:
                meta_y += rating_height + 10
            summary_height = cls._pillow_analysis_summary(
                draw,
                profile,
                x + 25,
                meta_y,
                chart_width,
                title_font=chart_title_font,
                label_font=chart_label_font,
            )
            if summary_height:
                meta_y += summary_height + 10
            change_value = weekly_changes.get(
                profile.platform,
                profile.recent_delta,
            )
            draw.text(
                (x + 25, meta_y),
                f"本次变化：{_format_delta(change_value)}",
                font=body_font,
                fill="#16835f" if (change_value or 0) >= 0 else "#c03d66",
            )
            meta_y += 23
            if profile.recent_contests and isinstance(profile.recent_contests[0], dict) and profile.recent_contests[0].get("name"):
                recent = profile.recent_contests[0]
                draw.text(
                    (x + 25, meta_y),
                    f"最近：{recent.get('name')} {_format_delta(recent.get('delta'))}",
                    font=body_font,
                    fill="#6e4a67",
                )
                meta_y += 23
            extras = _profile_extra_text(profile)
            if extras:
                draw.text(
                    (x + 25, meta_y),
                    extras,
                    font=body_font,
                    fill="#6e4a67",
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
                    fill="#a92b73" if rank is not None else "#6e4a67",
                )
        draw.text(
            (70, image.height - 42),
            f"生成时间：{_updated_text()} · 仅展示平台公开资料",
            font=body_font,
            fill="#f7dceb",
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
        title_font = cls._find_font(40, bold=True)
        subtitle_font = cls._find_font(20)
        body_font = cls._find_font(21)
        value_font = cls._find_font(30, bold=True)
        if not all((title_font, subtitle_font, body_font, value_font)):
            return False
        height = max(
            MIN_CARD_HEIGHT,
            min(
                MAX_CARD_HEIGHT,
                450
                + RANKING_HEADER_HEIGHT
                + len(rows) * RANKING_PILLOW_ROW_STEP,
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
            fill="#ffe7f2",
        )
        draw.text((70, 88), title, font=title_font, fill="#fff7fb")
        draw.text((70, 140), subtitle, font=subtitle_font, fill="#ffe7f2")
        y = 205
        header_fill = "#f0c8df"
        delta_right = CARD_WIDTH - 95
        value_right = delta_right - 135
        draw.text((95, y), "名次", font=subtitle_font, fill=header_fill)
        draw.text((185, y), "成员 / 账号", font=subtitle_font, fill=header_fill)
        resolved_header = _resolved_value_header(
            value_header,
            rows,
            metric_label=metric_label,
        )
        header_font = cls._find_font(18) or subtitle_font
        user_max_width = 600
        draw.text(
            (value_right, y),
            cls._fit_rank_pillow_text(
                resolved_header,
                header_font,
                170,
            ),
            font=header_font,
            fill=header_fill,
            anchor="rt",
        )
        secondary_header = _resolved_secondary_header(
            secondary_label,
            rows,
            metric_label=metric_label,
            secondary_value_key=secondary_value_key,
        )
        draw.text(
            (delta_right, y),
            cls._fit_rank_pillow_text(
                secondary_header,
                header_font,
                125,
            ),
            font=header_font,
            fill=header_fill,
            anchor="rt",
        )
        y += RANKING_HEADER_HEIGHT
        for index, row in enumerate(rows, start=1):
            draw.rounded_rectangle(
                (70, y, CARD_WIDTH - 70, y + 72),
                radius=12,
                fill="#fff5fb",
                outline="#efc5dc",
                width=1,
            )
            draw.text((95, y + 15), f"{index:02d}", font=value_font, fill="#d34f93")
            draw.text(
                (185, y + 11),
                cls._fit_rank_pillow_text(
                    row.get("display_name")
                    or row.get("qq_name")
                    or "未知用户",
                    body_font,
                    user_max_width,
                ),
                font=body_font,
                fill="#51315d",
            )
            draw.text(
                (185, y + 43),
                cls._fit_rank_pillow_text(
                    row.get("handle") or "未绑定",
                    subtitle_font,
                    user_max_width,
                ),
                font=subtitle_font,
                fill="#6e4a67",
            )
            draw.text(
                (value_right, y + 13),
                cls._fit_rank_pillow_text(
                    _format_number(row.get("display_value", row.get("value"))),
                    value_font,
                    170,
                ),
                font=value_font,
                fill="#51315d",
                anchor="rt",
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
                (delta_right, y + 22),
                cls._fit_rank_pillow_text(
                    secondary_text,
                    body_font,
                    125,
                ),
                font=body_font,
                fill="#16835f"
                if (row.get("delta") or 0) >= 0
                else "#c03d66",
                anchor="rt",
            )
            y += RANKING_PILLOW_ROW_STEP
        if note:
            draw.text(
                (70, min(y, image.height - 100)),
                note,
                font=subtitle_font,
                fill="#f0c8df",
            )
        draw.text(
            (70, image.height - 42),
            f"生成时间：{_updated_text()} · 只展示已加入群排行成员",
            font=subtitle_font,
            fill="#f7dceb",
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
        title_font = cls._find_font(40, bold=True)
        subtitle_font = cls._find_font(20)
        body_font = cls._find_font(20)
        value_font = cls._find_font(28, bold=True)
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
            fill="#ffe7f2",
        )
        draw.text((70, 88), title, font=title_font, fill="#fff7fb")
        draw.text((70, 140), subtitle, font=subtitle_font, fill="#ffe7f2")
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
            header_fill = "#5e3b5d"
            section_metric_label = rank_metric_label_for_rows(
                rows_data,
                platform=platform,
                fallback=(
                    "Rating"
                    if is_progress
                    else metric_label
                ),
            )
            value_header = (
                metric_label
                if is_progress
                else current_metric_header(section_metric_label)
            )
            delta_header = _resolved_secondary_header(
                secondary_label,
                rows_data,
                metric_label=section_metric_label,
                secondary_value_key=secondary_value_key,
            ) if is_progress else secondary_label or "近7日变化"
            section_header_y = y + 52
            draw.text(
                (x + 20, section_header_y),
                "名次",
                font=subtitle_font,
                fill=header_fill,
            )
            draw.text(
                (x + 75, section_header_y),
                "成员",
                font=subtitle_font,
                fill=header_fill,
            )
            value_right = x + section_w - 122
            delta_right = x + section_w - 22
            header_font = cls._find_font(17) or subtitle_font
            value_header = cls._fit_rank_pillow_text(
                value_header,
                header_font,
                155,
            )
            delta_header = cls._fit_rank_pillow_text(
                delta_header,
                header_font,
                110,
            )
            draw.text(
                (value_right, section_header_y),
                value_header,
                font=header_font,
                fill=header_fill,
                anchor="rt",
            )
            draw.text(
                (delta_right, section_header_y),
                delta_header,
                font=header_font,
                fill=header_fill,
                anchor="rt",
            )
            value_left = value_right - 155
            user_max_width = max(100, value_left - (x + 75) - 14)
            row_y = y + 90
            for rank, item in enumerate(rows_data[:5], start=1):
                draw.text((x + 20, row_y), f"{rank:02d}", font=body_font, fill="#d34f93")
                draw.text(
                    (x + 75, row_y),
                    cls._fit_rank_pillow_text(
                        item.get("display_name")
                        or item.get("qq_name")
                        or "未知用户",
                        body_font,
                        user_max_width,
                    ),
                    font=body_font,
                    fill="#51315d",
                )
                value_text = _format_number(
                    item.get("display_value", item.get("value"))
                )
                value_font_for_item = value_font
                if cls._pillow_text_width(value_text, value_font) > 155:
                    value_font_for_item = (
                        cls._find_font(22, bold=True) or value_font
                    )
                draw.text(
                    (value_right, row_y),
                    cls._fit_rank_pillow_text(
                        value_text,
                        value_font_for_item,
                        155,
                    ),
                    font=value_font_for_item,
                    fill="#51315d",
                    anchor="rt",
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
                delta_font = subtitle_font
                if cls._pillow_text_width(delta_value, delta_font) > 110:
                    delta_font = cls._find_font(17) or subtitle_font
                draw.text(
                    (delta_right, row_y + 3),
                    cls._fit_rank_pillow_text(
                        (
                            _format_number(delta_value)
                            if is_progress
                            else str(delta_value)
                        ),
                        delta_font,
                        110,
                    ),
                    font=delta_font,
                    fill="#6e4a67",
                    anchor="rt",
                )
                row_y += OVERVIEW_PILLOW_ROW_STEP
        if note:
            draw.text(
                (70, max(205, image.height - 112)),
                note,
                font=subtitle_font,
                fill="#f0c8df",
            )
        draw.text(
            (70, image.height - 42),
            f"生成时间：{_updated_text()} · {metric_label}",
            font=subtitle_font,
            fill="#f7dceb",
        )
        try:
            image.save(image_path, format="PNG")
        except OSError:
            return False
        return image_path.is_file() and image_path.stat().st_size > 0
