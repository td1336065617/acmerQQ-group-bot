"""后台 Web UI 新增接口的回归测试。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_main_accounts import _load_main_module
from src.account_models import AccountFetchError, AccountProfile
from src.models import GroupConfig


FULL_SETTINGS = {
    "morning_push_time": "08:00",
    "push_platforms": ["codeforces", "nowcoder"],
    "reminder_enabled": True,
    "at_all_enabled": False,
    "max_plain_text_chars": 1800,
    "max_plain_text_lines": 36,
    "recent_contest_days": 7,
    "nowcoder_scope": "all",
    "settle_push_enabled": True,
    "settle_delay_minutes": 10,
    "settle_min_participants": 1,
    "settle_show_unsolved": True,
    "daily_problem_enabled": True,
    "daily_problem_platform": "nowcoder",
    "daily_problem_count": 1,
    "recommend_enabled": True,
    "weekly_report_enabled": True,
    "weekly_report_weekday": 1,
    "weekly_report_time": "20:00",
}


class FakeRequest:
    def __init__(self, body=None, args=None):
        self.body = body
        self.args = args or {}

    async def json(self, default=None):
        return self.body if self.body is not None else default


class FakeKV:
    def __init__(self):
        self.data = {}

    async def get(self, key, default=None):
        return self.data.get(key, default)

    async def put(self, key, value):
        self.data[key] = value


class FakeRegistry:
    def __init__(self, accounts=None, conflict=False):
        self.accounts = accounts or {}
        self.store_enabled = False
        self.conflict = conflict
        self.saved = []
        self.removed = []

    async def get_all_accounts(self):
        return self.accounts

    async def get_user_accounts(self, user_id):
        return self.accounts.get(user_id, {})

    async def save_binding(self, user_id, platform, profile, **kwargs):
        if self.conflict:
            raise ValueError("这个平台账号已经绑定到其他 QQ 用户")
        self.saved.append((user_id, platform, profile.handle, kwargs))
        self.accounts.setdefault(user_id, {})[platform] = {
            "handle": profile.handle,
            "platform_user_id": profile.platform_user_id,
            "qq_name": kwargs.get("qq_name") or "",
            "verified_at": 1.0,
        }

    async def remove_binding(self, user_id, platform):
        per_user = self.accounts.get(user_id) or {}
        if platform in per_user:
            del per_user[platform]
            self.removed.append((user_id, platform))
            return True
        return False


class FakeFetcher:
    def __init__(self, profile=None, error=None):
        self.profile = profile
        self.error = error
        self.calls = []

    @staticmethod
    def invalid_identifier_message(platform):
        return f"{platform} 账号格式不正确"

    async def get_profile(self, platform, identifier, **kwargs):
        self.calls.append((platform, identifier, kwargs))
        if self.error is not None:
            raise self.error
        return self.profile


class FakeRank:
    def __init__(self):
        self.calls = []

    async def read(self, group_id, platform, **kwargs):
        self.calls.append((group_id, platform, kwargs))
        return [{"handle": "a", "value": 1}], ["warn"]


def _bot(
    main_module,
    *,
    kv=None,
    registry=None,
    fetcher=None,
    rank=None,
    groups=None,
    admins=None,
    settings=None,
):
    bot = main_module.AcmerGroupBot.__new__(main_module.AcmerGroupBot)
    store = kv or FakeKV()
    bot.get_kv_data = store.get
    bot.put_kv_data = store.put
    bot._kv_store = store
    bot.account_registry = registry or FakeRegistry()
    bot.account_fetcher = fetcher or FakeFetcher()
    bot.rank_service = rank or FakeRank()

    async def get_groups():
        return list(groups or [GroupConfig(group_id="g1")])

    async def get_settings():
        return dict(settings or FULL_SETTINGS)

    async def get_admins():
        return list(admins if admins is not None else ["admin-1"])

    bot.get_groups = get_groups
    bot.get_settings = get_settings
    bot._get_admins = get_admins
    bot._default_platform_id = lambda: "aiocqhttp"
    bot._invalidate_all_rank_cache = lambda: None
    invalidated = []
    bot._invalidate_all_rank_cache = lambda: invalidated.append(True)
    bot.invalidated = invalidated
    return bot


def _profile(handle="jiangly", platform="codeforces"):
    return AccountProfile(
        platform=platform, handle=handle, platform_user_id=handle
    )


def test_overview_summarises_state():
    m = _load_main_module()
    bot = _bot(
        m,
        groups=[GroupConfig(group_id="g1"), GroupConfig(group_id="g2", enabled=False)],
        admins=["a", "b"],
    )
    result = asyncio.run(bot._web_overview())
    assert result["status"] == "success"
    data = result["data"]
    assert data["group_count"] == 2
    assert data["enabled_group_count"] == 1
    assert data["admin_count"] == 2
    assert data["store_backend"] == "kv"
    assert data["platform_id"] == "aiocqhttp"
    assert any(item["kind"] == "morning" for item in data["next_pushes"])


def test_push_log_returns_newest_first_and_filters_group():
    m = _load_main_module()
    bot = _bot(m)
    bot._kv_store.data["push_log"] = [
        {"ts": 1, "group_id": "g1", "kind": "morning", "ok": True, "detail": ""},
        {"ts": 2, "group_id": "g2", "kind": "settle", "ok": False, "detail": "x"},
        {"ts": 3, "group_id": "g1", "kind": "test", "ok": True, "detail": ""},
    ]
    m.request = FakeRequest(args={"group_id": "g1", "limit": "10"})
    result = asyncio.run(bot._web_push_log())
    items = result["data"]["items"]
    assert [item["ts"] for item in items] == [3, 1]
    assert result["data"]["total"] == 2


def test_bindings_list_flattens_filters_and_counts():
    m = _load_main_module()
    registry = FakeRegistry(
        {
            "u1": {
                "codeforces": {"handle": "a", "qq_name": "小明", "verified_at": 2},
                "nowcoder": {"handle": "1", "qq_name": "", "verified_at": 1},
            },
            "u2": {
                "codeforces": {"handle": "b", "qq_name": "小红", "verified_at": 3}
            },
        }
    )
    bot = _bot(m, registry=registry)
    m.request = FakeRequest(args={})
    result = asyncio.run(bot._web_bindings_list())
    data = result["data"]
    assert data["total"] == 3
    assert data["platform_counts"]["codeforces"] == 2
    assert data["items"][0]["handle"] == "b"

    m.request = FakeRequest(args={"platform": "codeforces", "q": "小明"})
    filtered = asyncio.run(bot._web_bindings_list())
    assert filtered["data"]["total"] == 1
    assert filtered["data"]["items"][0]["user_id"] == "u1"


def test_bindings_save_writes_target_and_invalidates_cache():
    m = _load_main_module()
    registry = FakeRegistry()
    fetcher = FakeFetcher(profile=_profile())
    bot = _bot(m, registry=registry, fetcher=fetcher)
    m.request = FakeRequest(
        {
            "action": "save",
            "user_id": "u1",
            "platform": "codeforces",
            "identifier": "jiangly",
            "qq_name": "小明",
            "group_id": "g1",
        }
    )
    result = asyncio.run(bot._web_bindings_write())
    assert result["status"] == "success"
    assert registry.saved == [
        (
            "u1",
            "codeforces",
            "jiangly",
            # verified_at=None 表示这次是真正校验过账号（账号有变化或首次绑定）
            {"group_id": "g1", "qq_name": "小明", "verified_at": None},
        )
    ]
    assert fetcher.calls[0][0] == "codeforces"
    assert bot.invalidated == [True]
    assert result["data"]["replaced"] is None


def test_bindings_save_reports_replaced_handle():
    m = _load_main_module()
    registry = FakeRegistry({"u1": {"codeforces": {"handle": "old"}}})
    bot = _bot(m, registry=registry, fetcher=FakeFetcher(profile=_profile()))
    m.request = FakeRequest(
        {
            "action": "save",
            "user_id": "u1",
            "platform": "codeforces",
            "identifier": "new",
        }
    )
    result = asyncio.run(bot._web_bindings_write())
    assert result["data"]["replaced"] == "old"


def test_bindings_save_conflict_is_rejected_without_cache_reset():
    m = _load_main_module()
    registry = FakeRegistry(conflict=True)
    bot = _bot(m, registry=registry, fetcher=FakeFetcher(profile=_profile()))
    m.request = FakeRequest(
        {
            "action": "save",
            "user_id": "u1",
            "platform": "codeforces",
            "identifier": "jiangly",
        }
    )
    result = asyncio.run(bot._web_bindings_write())
    assert isinstance(result, str)
    assert "已经绑定到其他 QQ 用户" in result
    assert bot.invalidated == []


def test_bindings_save_invalid_identifier_does_not_fetch():
    m = _load_main_module()
    fetcher = FakeFetcher(profile=_profile())
    bot = _bot(m, fetcher=fetcher)
    m.request = FakeRequest(
        {"action": "save", "user_id": "u1", "platform": "codeforces", "identifier": "!!!"}
    )
    result = asyncio.run(bot._web_bindings_write())
    assert isinstance(result, str)
    assert "格式不正确" in result
    assert fetcher.calls == []


def test_bindings_save_fetch_error_is_reported():
    m = _load_main_module()
    error = AccountFetchError("Codeforces 用户名或主页链接格式不正确", temporary=False)
    bot = _bot(m, fetcher=FakeFetcher(error=error))
    m.request = FakeRequest(
        {
            "action": "save",
            "user_id": "u1",
            "platform": "codeforces",
            "identifier": "nobody",
        }
    )
    result = asyncio.run(bot._web_bindings_write())
    assert isinstance(result, str)
    assert "格式不正确" in result


def test_bindings_delete_single_and_batch():
    m = _load_main_module()
    registry = FakeRegistry(
        {"u1": {"codeforces": {"handle": "a"}}, "u2": {"nowcoder": {"handle": "b"}}}
    )
    bot = _bot(m, registry=registry)
    m.request = FakeRequest(
        {
            "action": "delete",
            "items": [
                {"user_id": "u1", "platform": "codeforces"},
                {"user_id": "u2", "platform": "nowcoder"},
                {"user_id": "u3", "platform": "luogu"},
            ],
        }
    )
    result = asyncio.run(bot._web_bindings_write())
    assert result["data"]["removed"] == 2
    assert bot.invalidated == [True]


def test_rank_defaults_to_snapshot_and_refresh_forces():
    m = _load_main_module()
    rank = FakeRank()
    bot = _bot(m, rank=rank)
    m.request = FakeRequest(args={"group_id": "g1", "platform": "codeforces"})
    result = asyncio.run(bot._web_rank())
    assert result["data"]["stale"] is True
    assert rank.calls[-1][2] == {"progress": False, "allow_stale": True, "force": False}

    m.request = FakeRequest(
        args={"group_id": "g1", "platform": "codeforces", "refresh": "1"}
    )
    result = asyncio.run(bot._web_rank())
    assert result["data"]["stale"] is False
    assert rank.calls[-1][2] == {"progress": False, "allow_stale": False, "force": True}


def test_import_preview_does_not_write():
    m = _load_main_module()
    bot = _bot(m)
    m.request = FakeRequest(
        {
            "apply": False,
            "payload": {"settings": {"settle_delay_minutes": 20}},
        }
    )
    result = asyncio.run(bot._web_import())
    assert result["data"]["applied"] is False
    assert result["data"]["diff"]["settings"]["settle_delay_minutes"] == {
        "from": 10,
        "to": 20,
    }
    assert bot._kv_store.data == {}


def test_import_invalid_value_is_rejected_wholesale():
    m = _load_main_module()
    bot = _bot(m)
    m.request = FakeRequest(
        {"apply": True, "payload": {"settings": {"settle_delay_minutes": 999}}}
    )
    result = asyncio.run(bot._web_import())
    assert isinstance(result, str)
    assert "赛后赛果推送延迟" in result
    assert bot._kv_store.data == {}


def test_import_apply_writes_normalized_settings():
    m = _load_main_module()
    bot = _bot(m)
    m.request = FakeRequest(
        {"apply": True, "payload": {"settings": {"settle_delay_minutes": 20}}}
    )
    result = asyncio.run(bot._web_import())
    assert result["data"]["applied"] is True
    written = bot._kv_store.data["settings"]
    assert written["settle_delay_minutes"] == 20
    assert written["morning_push_time"] == "08:00"


def test_run_now_rejects_disabled_group_and_not_ready_scene():
    m = _load_main_module()
    groups = [GroupConfig(group_id="g1", enabled=False)]
    bot = _bot(m, groups=groups)
    m.request = FakeRequest({"group_id": "g1", "kind": "morning"})
    assert "已停用" in asyncio.run(bot._web_run_now())

    groups = [GroupConfig(group_id="g1", enabled=True)]
    bot = _bot(m, groups=groups)
    bot._group_scene_ready = lambda *_a, **_k: False
    m.request = FakeRequest({"group_id": "g1", "kind": "morning"})
    assert "会话未就绪" in asyncio.run(bot._web_run_now())
