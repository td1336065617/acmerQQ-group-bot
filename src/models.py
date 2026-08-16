"""数据模型定义。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from pydantic import BaseModel, Field

# 展示/计算统一使用北京时间（UTC+8）
CN_TZ = timezone(timedelta(hours=8))

PLATFORM_LABELS = {
    "nowcoder": "牛客",
    "codeforces": "Codeforces",
    "atcoder": "AtCoder",
    "luogu": "洛谷",
}
DEFAULT_PLATFORMS = ["nowcoder", "codeforces", "atcoder", "luogu"]


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


class GroupConfig(BaseModel):
    """每个群的推送配置。"""

    group_id: str
    enabled: bool = True
    morning_push_time: str = "08:00"
    push_platforms: List[str] = Field(default_factory=lambda: list(DEFAULT_PLATFORMS))
    reminder_enabled: bool = True
