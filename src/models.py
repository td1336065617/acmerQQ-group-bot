"""数据模型定义。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from pydantic import BaseModel, Field

# 展示/计算统一使用北京时间（UTC+8）
CN_TZ = timezone(timedelta(hours=8))

PLATFORM_LABELS = {
    "nowcoder": "牛客",
    "codeforces": "Codeforces",
    "atcoder": "AtCoder",
    "luogu": "洛谷",
    "offline": "线下赛",
}
DEFAULT_PLATFORMS = ["nowcoder", "codeforces", "atcoder", "luogu"]
OFFLINE_PLATFORM = "offline"
QUERY_PLATFORMS = [*DEFAULT_PLATFORMS, OFFLINE_PLATFORM]


class Contest(BaseModel):
    """统一后的比赛模型（start_time/end_time 均为 UTC 带时区时间）。"""

    platform: str
    name: str
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_minutes: int = 0
    url: str = ""
    contest_id: str = ""

    def start_cn(self) -> datetime:
        return self.start_time.astimezone(CN_TZ)

    def is_upcoming(self, margin_minutes: int = 5) -> bool:
        now = datetime.now(timezone.utc)
        return self.start_time > now - timedelta(minutes=margin_minutes)

    def format_detail(self) -> str:
        label = PLATFORM_LABELS.get(self.platform, self.platform)
        lines = [
            f"📅 {label} 最近比赛",
            f"🏷 {self.name}",
            f"🕐 {self.start_cn():%Y-%m-%d %H:%M}（北京时间）",
        ]
        if self.duration_minutes > 0:
            lines.append(f"⏳ 时长 {self.duration_minutes} 分钟")
        if self.url:
            lines.append(f"🔗 {self.url}")
        return "\n".join(lines)


class OfflineContest(BaseModel):
    """XCPC Link 线下赛程中的一场比赛。"""

    name: str
    start_date: date
    end_date: Optional[date] = None
    venue: str = ""
    organizer: str = ""
    problem_setter: str = ""
    official_url: str = ""
    allocation_plan: str = ""
    source_url: str

    def event_end_date(self) -> date:
        return self.end_date or self.start_date

    def is_upcoming(self) -> bool:
        return self.event_end_date() >= datetime.now(CN_TZ).date()

    def date_text(self) -> str:
        if self.end_date and self.end_date != self.start_date:
            return (
                f"{self.start_date:%Y-%m-%d} 至 {self.end_date:%Y-%m-%d}"
            )
        return f"{self.start_date:%Y-%m-%d}"

    def format_detail(self) -> str:
        lines = [
            "🏟 线下赛",
            f"🏷 {self.name}",
            f"🕐 日期：{self.date_text()}（北京时间）",
        ]
        if self.venue:
            lines.append(f"📍 赛站/地点：{self.venue}")
        if self.organizer:
            lines.append(f"🏫 主办方：{self.organizer}")
        if self.problem_setter:
            lines.append(f"🧩 出题组：{self.problem_setter}")
        if self.allocation_plan:
            lines.append(f"📌 名额/规则：{self.allocation_plan}")
        if self.official_url:
            lines.append(f"🔗 官方通知：{self.official_url}")
        lines.append(f"📚 数据源：XCPC Link（{self.source_url}）")
        return "\n".join(lines)


class GroupConfig(BaseModel):
    """每个群的推送配置。"""

    group_id: str
    platform_id: Optional[str] = None
    activated: bool = False
    enabled: bool = True
    morning_push_time: str = "08:00"
    push_platforms: List[str] = Field(default_factory=lambda: list(DEFAULT_PLATFORMS))
    reminder_enabled: bool = True
