"""竞赛平台账号、战绩和绑定挑战的数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


ACCOUNT_PLATFORMS = ("codeforces", "nowcoder", "luogu", "atcoder")

ACCOUNT_PLATFORM_LABELS = {
    "codeforces": "Codeforces",
    "nowcoder": "牛客",
    "luogu": "洛谷",
    "atcoder": "AtCoder",
}

ACCOUNT_PLATFORM_ALIASES = {
    "cf": "codeforces",
    "codeforces": "codeforces",
    "牛客": "nowcoder",
    "nk": "nowcoder",
    "nowcoder": "nowcoder",
    "洛谷": "luogu",
    "lg": "luogu",
    "luogu": "luogu",
    "atc": "atcoder",
    "atcoder": "atcoder",
}

VERIFICATION_FIELD_LABELS = {
    "codeforces": "姓氏（Last name）",
    "nowcoder": "个性签名",
    "luogu": "个人介绍",
    "atcoder": "Affiliation（所属）",
}


@dataclass
class AccountProfile:
    """平台公开资料的统一表示。"""

    platform: str
    handle: str
    platform_user_id: str = ""
    display_name: str = ""
    profile_url: str = ""
    verification_value: str = ""
    rating: Optional[int] = None
    rating_rank: Optional[int] = None
    rank_text: str = ""
    max_rating: Optional[int] = None
    max_rank_text: str = ""
    contest_count: Optional[int] = None
    solved_count: Optional[int] = None
    school: str = ""
    organization: str = ""
    country: str = ""
    city: str = ""
    color: str = ""
    contribution: Optional[int] = None
    recent_delta: Optional[int] = None
    rating_history: List[Dict[str, Any]] = field(default_factory=list)
    recent_contests: List[Dict[str, Any]] = field(default_factory=list)
    recent_submissions: List[Dict[str, Any]] = field(default_factory=list)
    avatar_url: str = ""
    source_url: str = ""
    fetched_at: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> Dict[str, Any]:
        """返回可用于卡片/排行的公开字段，不包含验证字段。"""
        return {
            "platform": self.platform,
            "handle": self.handle,
            "platform_user_id": self.platform_user_id,
            "display_name": self.display_name,
            "profile_url": self.profile_url,
            "rating": self.rating,
            "rating_rank": self.rating_rank,
            "rank_text": self.rank_text,
            "max_rating": self.max_rating,
            "max_rank_text": self.max_rank_text,
            "contest_count": self.contest_count,
            "solved_count": self.solved_count,
            "school": self.school,
            "organization": self.organization,
            "country": self.country,
            "city": self.city,
            "color": self.color,
            "contribution": self.contribution,
            "recent_delta": self.recent_delta,
            "rating_history": list(self.rating_history),
            "recent_contests": list(self.recent_contests),
            "recent_submissions": list(self.recent_submissions),
            "avatar_url": self.avatar_url,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at,
            "extra": dict(self.extra),
        }


class AccountFetchError(RuntimeError):
    """公开账号资料获取或解析失败。"""

    def __init__(self, message: str, *, temporary: bool = True) -> None:
        super().__init__(message)
        self.temporary = temporary


def platform_label(platform: str) -> str:
    return ACCOUNT_PLATFORM_LABELS.get(platform, platform)


def normalize_platform(value: object) -> Optional[str]:
    text = str(value or "").strip().casefold()
    return ACCOUNT_PLATFORM_ALIASES.get(text)
