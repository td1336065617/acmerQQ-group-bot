"""账号绑定、群排行成员和 Rating 快照的持久化管理。"""
from __future__ import annotations

import hashlib
import re
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple

from .account_models import AccountProfile

ACCOUNTS_KEY = "linked_accounts"
PENDING_BINDINGS_KEY = "pending_account_bindings"
GROUP_RANK_KEY = "group_rank_members"
RATING_SNAPSHOTS_KEY = "account_rating_snapshots"

BINDING_TTL = 10 * 60
TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
TOKEN_RE = re.compile(r"ACM-[A-Z0-9]{8}", re.I)


def create_binding_token() -> str:
    return "ACM-" + "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(8))


def token_hash(token: str) -> str:
    return hashlib.sha256(str(token).strip().upper().encode("utf-8")).hexdigest()


class AccountRegistry:
    """通过插件 KV 存储账号关系，不保存平台登录凭据。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin

    async def _get(self, key: str, default):
        value = await self.plugin.get_kv_data(key, default)
        return value if value is not None else default

    async def _put(self, key: str, value) -> None:
        await self.plugin.put_kv_data(key, value)

    async def get_user_accounts(self, user_id: str) -> Dict[str, Dict[str, Any]]:
        data = await self._get(ACCOUNTS_KEY, {})
        if not isinstance(data, dict):
            return {}
        value = data.get(str(user_id), {})
        return dict(value) if isinstance(value, dict) else {}

    async def get_all_accounts(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        data = await self._get(ACCOUNTS_KEY, {})
        if not isinstance(data, dict):
            return {}
        return {
            str(user_id): dict(accounts)
            for user_id, accounts in data.items()
            if isinstance(accounts, dict)
        }

    async def save_binding(
        self,
        user_id: str,
        platform: str,
        profile: AccountProfile,
        *,
        group_id: Optional[str] = None,
        qq_name: str = "",
    ) -> None:
        accounts = await self.get_all_accounts()
        user_key = str(user_id)
        for other_user, user_accounts in accounts.items():
            if other_user == user_key:
                continue
            existing = user_accounts.get(platform)
            if not isinstance(existing, dict):
                continue
            existing_id = str(
                existing.get("platform_user_id") or existing.get("handle") or ""
            ).casefold()
            profile_id = str(
                profile.platform_user_id or profile.handle or ""
            ).casefold()
            if existing_id and existing_id == profile_id:
                raise ValueError("这个平台账号已经绑定到其他 QQ 用户")

        user_accounts = accounts.setdefault(user_key, {})
        user_accounts[platform] = {
            "platform": platform,
            "handle": profile.handle,
            "platform_user_id": profile.platform_user_id,
            "display_name": profile.display_name or profile.handle,
            "profile_url": profile.profile_url,
            "verified_at": time.time(),
            "qq_name": str(qq_name or "").strip(),
        }
        await self._put(ACCOUNTS_KEY, accounts)
        if group_id:
            # 新绑定按约定自动加入当前群排行；此前退出排行的状态不阻止重新绑定。
            await self.set_group_member(str(group_id), user_key, True)
        await self.clear_pending(user_key, platform)

    async def set_user_display_name(self, user_id: str, qq_name: str) -> bool:
        name = str(qq_name or "").strip()
        if not name:
            return False
        accounts = await self.get_all_accounts()
        user_accounts = accounts.get(str(user_id))
        if not isinstance(user_accounts, dict):
            return False
        changed = False
        for item in user_accounts.values():
            if isinstance(item, dict) and item.get("qq_name") != name:
                item["qq_name"] = name
                changed = True
        if changed:
            await self._put(ACCOUNTS_KEY, accounts)
        return changed

    async def remove_binding(self, user_id: str, platform: str) -> bool:
        accounts = await self.get_all_accounts()
        user_key = str(user_id)
        user_accounts = accounts.get(user_key)
        if not isinstance(user_accounts, dict) or platform not in user_accounts:
            return False
        user_accounts.pop(platform, None)
        if user_accounts:
            accounts[user_key] = user_accounts
        else:
            accounts.pop(user_key, None)
        await self._put(ACCOUNTS_KEY, accounts)
        await self.clear_pending(user_id, platform)
        return True

    async def create_pending(
        self,
        user_id: str,
        platform: str,
        profile: AccountProfile,
        *,
        group_id: Optional[str] = None,
    ) -> str:
        token = create_binding_token()
        pending = await self._get(PENDING_BINDINGS_KEY, {})
        if not isinstance(pending, dict):
            pending = {}
        pending[f"{user_id}:{platform}"] = {
            "platform": platform,
            "handle": profile.handle,
            "platform_user_id": profile.platform_user_id,
            "token_hash": token_hash(token),
            "created_at": time.time(),
            "expires_at": time.time() + BINDING_TTL,
            "group_id": str(group_id) if group_id else "",
        }
        await self._put(PENDING_BINDINGS_KEY, pending)
        return token

    async def get_pending(
        self, user_id: str, platform: str
    ) -> Optional[Dict[str, Any]]:
        pending = await self._get(PENDING_BINDINGS_KEY, {})
        if not isinstance(pending, dict):
            return None
        item = pending.get(f"{user_id}:{platform}")
        if not isinstance(item, dict):
            return None
        try:
            expires_at = float(item.get("expires_at", 0) or 0)
        except (TypeError, ValueError):
            await self.clear_pending(user_id, platform)
            return None
        if expires_at < time.time():
            await self.clear_pending(user_id, platform)
            return None
        return dict(item)

    async def clear_pending(self, user_id: str, platform: str) -> None:
        pending = await self._get(PENDING_BINDINGS_KEY, {})
        if not isinstance(pending, dict):
            return
        pending.pop(f"{user_id}:{platform}", None)
        await self._put(PENDING_BINDINGS_KEY, pending)

    @staticmethod
    def token_matches(value: str, expected_hash: str) -> bool:
        """校验验证码是否追加在公开资料字段中。"""
        return any(
            token_hash(candidate) == expected_hash
            for candidate in TOKEN_RE.findall(str(value or ""))
        )

    async def set_group_member(
        self,
        group_id: str,
        user_id: str,
        enabled: bool,
        *,
        preserve_opt_out: bool = False,
    ) -> None:
        data = await self._get(GROUP_RANK_KEY, {})
        if not isinstance(data, dict):
            data = {}
        group = data.setdefault(str(group_id), {})
        if not isinstance(group, dict):
            group = {}
            data[str(group_id)] = group
        key = str(user_id)
        existing = group.get(key)
        if (
            preserve_opt_out
            and isinstance(existing, dict)
            and not bool(existing.get("enabled", False))
        ):
            return
        group[key] = {
            "enabled": bool(enabled),
            "updated_at": time.time(),
        }
        await self._put(GROUP_RANK_KEY, data)

    async def get_group_member_ids(self, group_id: str) -> List[str]:
        data = await self._get(GROUP_RANK_KEY, {})
        if not isinstance(data, dict):
            return []
        group = data.get(str(group_id), {})
        if not isinstance(group, dict):
            return []
        return [
            str(user_id)
            for user_id, item in group.items()
            if isinstance(item, dict) and bool(item.get("enabled", False))
        ]

    async def record_rating(
        self, user_id: str, platform: str, rating: Optional[int]
    ) -> None:
        await self.record_ratings([(user_id, platform, rating)])

    async def record_ratings(
        self,
        entries: List[Tuple[str, str, Optional[int]]],
    ) -> None:
        """批量写入 Rating 快照，避免大群排行逐成员读写 KV。"""
        normalized: Dict[Tuple[str, str], int] = {}
        for user_id, platform, rating in entries:
            if rating is None:
                continue
            try:
                normalized[(str(user_id), str(platform))] = int(rating)
            except (TypeError, ValueError):
                continue
        if not normalized:
            return

        data = await self._get(RATING_SNAPSHOTS_KEY, {})
        if not isinstance(data, dict):
            data = {}
        now = time.time()
        changed = False
        for (user_id, platform), value in normalized.items():
            user = data.setdefault(user_id, {})
            if not isinstance(user, dict):
                user = {}
                data[user_id] = user
            history = user.setdefault(platform, [])
            if not isinstance(history, list):
                history = []
                user[platform] = history
            if history and isinstance(history[-1], dict):
                last_value = history[-1].get("rating")
                try:
                    last_time = float(history[-1].get("timestamp", 0) or 0)
                except (TypeError, ValueError):
                    last_time = 0.0
                if last_value == value and now - last_time < 15 * 60:
                    continue
            history.append({"timestamp": now, "rating": value})
            user[platform] = history[-90:]
            changed = True
        if changed:
            await self._put(RATING_SNAPSHOTS_KEY, data)

    async def weekly_delta(
        self,
        user_id: str,
        platform: str,
        *,
        days: int = 7,
        now: Optional[float] = None,
    ) -> Optional[int]:
        return (
            await self.get_weekly_deltas(
                [(str(user_id), str(platform))],
                days=days,
                now=now,
            )
        ).get((str(user_id), str(platform)))

    async def get_weekly_deltas(
        self,
        requests: List[Tuple[str, str]],
        *,
        days: int = 7,
        now: Optional[float] = None,
    ) -> Dict[Tuple[str, str], Optional[int]]:
        """批量计算多个用户/平台的近期 Rating 变化。"""
        data = await self._get(RATING_SNAPSHOTS_KEY, {})
        if not isinstance(data, dict):
            return {
                (str(user_id), str(platform)): None
                for user_id, platform in requests
            }
        current_time = time.time() if now is None else now
        cutoff = current_time - max(1, int(days)) * 86400
        result: Dict[Tuple[str, str], Optional[int]] = {}
        for user_id, platform in requests:
            key = (str(user_id), str(platform))
            if key in result:
                continue
            user = data.get(key[0], {})
            history = user.get(key[1], []) if isinstance(user, dict) else []
            if not isinstance(history, list) or not history:
                result[key] = None
                continue
            entries = []
            for item in history:
                if not isinstance(item, dict):
                    continue
                try:
                    timestamp = float(item.get("timestamp", 0) or 0)
                    rating = int(item.get("rating"))
                except (TypeError, ValueError):
                    continue
                entries.append((timestamp, rating))
            if not entries:
                result[key] = None
                continue
            entries.sort(key=lambda item: item[0])
            current_rating = entries[-1][1]
            baseline = next(
                (rating for timestamp, rating in reversed(entries)
                 if timestamp <= cutoff),
                None,
            )
            result[key] = (
                current_rating - baseline if baseline is not None else None
            )
        return result

    async def remove_user_from_all_groups(self, user_id: str) -> None:
        data = await self._get(GROUP_RANK_KEY, {})
        if not isinstance(data, dict):
            return
        key = str(user_id)
        changed = False
        for group in data.values():
            if isinstance(group, dict) and isinstance(group.get(key), dict):
                group[key]["enabled"] = False
                group[key]["updated_at"] = time.time()
                changed = True
        if changed:
            await self._put(GROUP_RANK_KEY, data)
