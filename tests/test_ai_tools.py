"""ACMer AI 只读工具的行为测试（复用 tests/test_main_accounts.py 的桩）。"""

from __future__ import annotations

import asyncio
import json
import types
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_main_accounts import _load_main_module


class FakeKV:
    def __init__(self, initial: dict | None = None) -> None:
        self.data = dict(initial or {})

    async def get(self, key, default=None):
        return self.data.get(key, default)

    async def put(self, key, value):
        self.data[key] = value


class FakeContest:
    """只需要 contest_start_utc 认得的字段：start_time（datetime）。"""

    def __init__(self, name: str, offset_seconds: float, url: str = "") -> None:
        import datetime

        self.name = name
        self.url = url
        self.duration_minutes = 120
        self.start_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            seconds=offset_seconds
        )

class FakeRow:
    def __init__(self, rank: int, name: str, solved: int) -> None:
        self.rank = rank
        self.display_name = name
        self.handle = name.lower()
        self.solved = solved
        self.total_problems = 10


class FakeProblem:
    problem_id = "p1"
    url = "https://example.com/p1"

    def display(self) -> str:
        return "P1 两数之和"


def run(coro):
    return asyncio.run(coro)


def payload(raw: str) -> dict:
    return json.loads(raw)


def make_bot(settings: dict | None = None):
    module = _load_main_module()
    kv = FakeKV()
    bot = module.AcmerGroupBot.__new__(module.AcmerGroupBot)
    bot.get_kv_data = kv.get
    bot.put_kv_data = kv.put
    base = {
        "acmer_ai_tools_enabled": True,
        "acmer_ai_throttle_limit": 5,
        "acmer_ai_throttle_window": 60,
        "push_platforms": ["codeforces", "luogu"],
        "daily_problem_platform": "nowcoder",
        "daily_problem_enabled": True,
        "recent_contest_days": 7,
    }
    base.update(settings or {})

    async def get_settings():
        return dict(base)

    bot.get_settings = get_settings
    return module, bot, base


class FakeEvent:
    def __init__(self, group_id: str = "1000", sender: str = "u1") -> None:
        self._group = group_id
        self._sender = sender
        self.unified_msg_origin = "aiocqhttp:GroupMessage:%s" % group_id

    def get_group_id(self):
        return self._group

    def get_sender_id(self):
        return self._sender

    def get_session_id(self):
        return self.unified_msg_origin


def test_contests_formats_upcoming(monkeypatch) -> None:
    module, bot, _settings = make_bot()

    contest = FakeContest("Codeforces Round 999", 3600, "https://cf/999")
    monkeypatch.setattr(bot, "_preview_contests", lambda platforms: [("codeforces", [contest], None)])
    data = payload(run(bot.acmer_contests(FakeEvent(), platform="cf", days=3, limit=5)))
    assert data["ok"] is True
    assert "Codeforces Round 999" in data["data"]["text"]


def test_contests_rejects_bad_platform() -> None:
    _module, bot, _settings = make_bot()
    data = payload(run(bot.acmer_contests(FakeEvent(), platform="不存在的平台")))
    assert data["ok"] is False and data["error"]["code"] == "invalid_argument"


def test_contests_rejects_bad_days() -> None:
    _module, bot, _settings = make_bot()
    assert payload(run(bot.acmer_contests(FakeEvent(), days="abc")))["error"]["code"] == "invalid_argument"
    assert payload(run(bot.acmer_contests(FakeEvent(), days="99")))["error"]["code"] == "invalid_argument"


def test_contests_reports_unavailable_when_empty(monkeypatch) -> None:
    _module, bot, _settings = make_bot()
    monkeypatch.setattr(bot, "_preview_contests", lambda platforms: [("codeforces", [], "缓存为空")])
    data = payload(run(bot.acmer_contests(FakeEvent())))
    assert data["ok"] is False and data["error"]["code"] == "capability_unavailable"


def test_daily_problem_uses_same_source(monkeypatch) -> None:
    _module, bot, _settings = make_bot()

    class FakeService:
        def peek_index(self, platform):
            return [object()]

        async def ensure_index(self, platform):
            return [object()]

    async def fake_problem(group_id, platform, day, pool=None):
        return FakeProblem()

    bot.problem_service = FakeService()
    monkeypatch.setattr(bot, "_daily_problem_for_group", fake_problem)
    data = payload(run(bot.acmer_daily_problem(FakeEvent())))
    assert data["ok"] is True and "P1 两数之和" in data["data"]["text"]


def test_daily_problem_handles_missing_pool(monkeypatch) -> None:
    _module, bot, _settings = make_bot()

    class FakeService:
        def peek_index(self, platform):
            return []

        async def ensure_index(self, platform):
            return []

    async def fake_problem(group_id, platform, day, pool=None):
        return None

    bot.problem_service = FakeService()
    monkeypatch.setattr(bot, "_daily_problem_for_group", fake_problem)
    data = payload(run(bot.acmer_daily_problem(FakeEvent())))
    assert data["ok"] is False and data["error"]["code"] == "capability_unavailable"


def test_group_rank_is_read_only(monkeypatch) -> None:
    _module, bot, _settings = make_bot()
    calls: list = []

    async def fake_rows(group_id, platform, *, progress=False, record_metrics=True):
        calls.append((group_id, platform, record_metrics))
        # 真实结构是 dict（main.py 里 rows.append({...})）
        return [
            {
                "user_id": "u1",
                "display_name": "小明",
                "handle": "xm",
                "value": 1500,
                "display_value": "1500",
                "metric_label": "Rating",
                "delta": 20,
            }
        ], []

    monkeypatch.setattr(bot, "_collect_rank_rows", fake_rows)
    data = payload(run(bot.acmer_group_rank(FakeEvent(), platform="lg")))
    assert data["ok"] is True and "小明" in data["data"]["text"]
    assert calls and all(item[2] is False for item in calls), "工具不得写快照"


def test_group_rank_requires_group() -> None:
    _module, bot, _settings = make_bot()
    data = payload(run(bot.acmer_group_rank(FakeEvent(group_id=""))))
    assert data["ok"] is False and data["error"]["code"] == "platform_unsupported"


def test_profile_query_is_read_only(monkeypatch) -> None:
    _module, bot, _settings = make_bot()
    seen: dict = {}

    async def fake_profiles(user_id, *, detail=True, force=False, record_metrics=True):
        seen["record_metrics"] = record_metrics
        # 真实契约：三元组 (accounts, profiles, errors)，profiles 是 profile 对象列表
        account = types.SimpleNamespace(platform="codeforces", handle="xm")
        profile = types.SimpleNamespace(platform="codeforces", handle="xm")
        return {"codeforces": account}, [profile], []

    def fake_metric(profile):
        return {"Rating": 1500}

    monkeypatch.setattr(bot, "_load_bound_profiles", fake_profiles)
    monkeypatch.setattr(bot, "_profile_metric", staticmethod(fake_metric))
    data = payload(run(bot.acmer_profile(FakeEvent(sender="u9"))))
    assert data["ok"] is True
    assert "1500" in data["data"]["text"]
    assert seen["record_metrics"] is False, "查战绩不得写快照"


def test_profile_not_found_for_unknown_member(monkeypatch) -> None:
    _module, bot, _settings = make_bot()

    async def no_members(group_id, platform):
        return []

    monkeypatch.setattr(bot, "_group_member_bindings", no_members)
    monkeypatch.setattr(bot, "_event_display_name", staticmethod(lambda event: "提问的人"))
    data = payload(run(bot.acmer_profile(FakeEvent(), user="查无此人")))
    assert data["ok"] is False and data["error"]["code"] == "not_found"


def test_tools_disabled_by_config() -> None:
    _module, bot, _settings = make_bot({"acmer_ai_tools_enabled": False})
    data = payload(run(bot.acmer_contests(FakeEvent())))
    assert data["ok"] is False and data["error"]["code"] == "disabled"


def test_throttle_blocks_after_limit() -> None:
    _module, bot, _settings = make_bot({"acmer_ai_throttle_limit": 2, "acmer_ai_throttle_window": 60})

    async def fake_rows(group_id, platform, *, progress=False, record_metrics=True):
        # 真实结构是 dict（main.py 里 rows.append({...})）
        return [
            {
                "user_id": "u1",
                "display_name": "小明",
                "handle": "xm",
                "value": 1500,
                "display_value": "1500",
                "metric_label": "Rating",
                "delta": 20,
            }
        ], []

    bot._collect_rank_rows = fake_rows
    event = FakeEvent()
    first = payload(run(bot.acmer_group_rank(event, platform="lg")))
    second = payload(run(bot.acmer_group_rank(event, platform="lg")))
    third = payload(run(bot.acmer_group_rank(event, platform="lg")))
    assert first["ok"] is True and second["ok"] is True
    assert third["ok"] is False and third["error"]["code"] == "throttled"


def test_group_rank_paginates_and_boards(monkeypatch) -> None:
    """page 分页 + board 三个值都要能跑（AC-04）。"""
    _module, bot, _settings = make_bot()
    seen: list = []

    async def fake_rows(group_id, platform, *, progress=False, record_metrics=True):
        seen.append(progress)
        rows = [FakeRow(index, "m" + str(index), index) for index in range(1, 26)]
        return rows, []

    monkeypatch.setattr(bot, "_collect_rank_rows", fake_rows)
    first = payload(run(bot.acmer_group_rank(FakeEvent(), platform="lg", board="total", page=1)))
    assert first["ok"] is True
    assert "11 | " not in first["data"]["text"]
    second = payload(run(bot.acmer_group_rank(FakeEvent(group_id="g2"), platform="lg", board="total", page=2)))
    assert second["ok"] is True and "11 | " in second["data"]["text"]
    progress = payload(run(bot.acmer_group_rank(FakeEvent(group_id="g3"), platform="lg", board="progress")))
    assert progress["ok"] is True and progress["data"]["board"] == "progress"
    regress = payload(run(bot.acmer_group_rank(FakeEvent(group_id="g4"), platform="lg", board="regress")))
    assert regress["ok"] in (True, False)   # 退步榜可能无数据，但不能抛异常
    bad = payload(run(bot.acmer_group_rank(FakeEvent(group_id="g5"), board="????")))
    assert bad["error"]["code"] == "invalid_argument"
    assert True in seen, "progress 榜单要传 progress=True"


def test_rank_lines_accepts_real_dict_rows() -> None:
    """真实排行行是 dict：必须能渲染出名次与成绩（旧实现只认对象属性）。"""
    import ai_impl

    rows = [
        {"display_name": "A", "display_value": "1500", "metric_label": "Rating", "delta": 20},
        {"display_name": "B", "display_value": "1400", "metric_label": "Rating", "delta": -30},
    ]
    lines = ai_impl.rank_lines(rows, 10)
    assert lines[0].startswith("1 | A")
    assert "Rating 1500" in lines[0]
    assert "+20" in lines[0]
    assert "-30" in lines[1]


def test_profile_lines_accepts_plain_profile_objects() -> None:
    """真实 _load_bound_profiles 只回 profile 对象（平台从对象上取）。"""
    import ai_impl
    import types

    profile = types.SimpleNamespace(platform="codeforces")
    lines = ai_impl.profile_lines([profile], lambda item: {"Rating": 1500})
    assert lines and "Codeforces" in lines[0] and "1500" in lines[0]


def test_select_upcoming_skips_finished_contests() -> None:
    """已结束的历史比赛不得被当成近期比赛返回。"""
    from datetime import datetime, timedelta, timezone

    import ai_impl

    now = datetime.now(timezone.utc)
    old = now - timedelta(days=30)
    soon = now + timedelta(days=1)
    entries = [
        (
            "codeforces",
            [
                {"start": old, "end": old + timedelta(hours=2)},
                {"start": soon, "end": soon + timedelta(hours=2)},
            ],
            None,
        )
    ]
    picked = ai_impl.select_upcoming(
        entries,
        start_of=lambda item: item["start"],
        end_of=lambda item: item["end"],
        now=now.timestamp(),
        days=7,
        limit=10,
    )
    assert len(picked) == 1, picked
    assert picked[0][1]["start"] == soon


def test_profile_finds_member_by_qq_name(monkeypatch) -> None:
    """生产实案：绑定记录里 display_name 是 OJ 用户名、QQ 昵称在 qq_name。

    模型把群昵称「道」当 user 传进来时，必须找到这个人（此前只匹配 display_name，回"没找到"）。
    """
    _module, bot, _settings = make_bot()

    async def fake_members(group_id, platform):
        return [("u7", "td1336065617", "645160704", "道")]

    async def fake_profiles(user_id, *, detail=True, force=False, record_metrics=True):
        assert user_id == "u7", user_id
        account = types.SimpleNamespace(platform="codeforces", handle="td1336065617")
        profile = types.SimpleNamespace(platform="codeforces", handle="td1336065617")
        return {"codeforces": account}, [profile], []

    monkeypatch.setattr(bot, "_group_member_bindings", fake_members)
    monkeypatch.setattr(bot, "_load_bound_profiles", fake_profiles)
    monkeypatch.setattr(bot, "_profile_metric", staticmethod(lambda profile: {"Rating": 1500}))
    monkeypatch.setattr(bot, "_event_display_name", staticmethod(lambda event: "提问的人"))
    data = payload(run(bot.acmer_profile(FakeEvent(), user="道")))
    assert data["ok"] is True, data
    assert data["data"]["user"] == "道"
    assert "1500" in data["data"]["text"]


def test_profile_own_nickname_is_treated_as_self(monkeypatch) -> None:
    """模型把发送者自己的群昵称填进 user 时按"本人"处理，不再去成员表空找一轮。"""
    _module, bot, _settings = make_bot()
    asked: dict = {}

    async def fake_profiles(user_id, *, detail=True, force=False, record_metrics=True):
        asked["user_id"] = user_id
        return {}, [types.SimpleNamespace(platform="codeforces", handle="me")], []

    async def should_not_call(group_id, platform):
        raise AssertionError("已经认出是自己，不该再查成员表")

    monkeypatch.setattr(bot, "_group_member_bindings", should_not_call)
    monkeypatch.setattr(bot, "_load_bound_profiles", fake_profiles)
    monkeypatch.setattr(bot, "_profile_metric", staticmethod(lambda profile: {"Rating": 1600}))
    monkeypatch.setattr(bot, "_event_display_name", staticmethod(lambda event: "道"))
    data = payload(run(bot.acmer_profile(FakeEvent(sender="u1"), user="道")))
    assert data["ok"] is True, data
    assert asked["user_id"] == "u1"
    assert data["data"]["user"] == "本人"


def test_profile_not_found_hints_to_leave_user_empty(monkeypatch) -> None:
    _module, bot, _settings = make_bot()

    async def no_members(group_id, platform):
        return []

    monkeypatch.setattr(bot, "_group_member_bindings", no_members)
    monkeypatch.setattr(bot, "_event_display_name", staticmethod(lambda event: "道"))
    data = payload(run(bot.acmer_profile(FakeEvent(), user="查无此人")))
    assert data["ok"] is False and data["error"]["code"] == "not_found"
    assert "user 留空" in data["error"]["message"], data


def test_profile_other_member_name_containing_own_name_is_not_hijacked(monkeypatch) -> None:
    """发送者叫「道长」、群里有成员叫「道」：查「道」必须查那位成员，不能认成自己。"""
    _module, bot, _settings = make_bot()
    asked: dict = {}

    async def fake_members(group_id, platform):
        return [("u7", "td1336065617", "645160704", "道")]

    async def fake_profiles(user_id, *, detail=True, force=False, record_metrics=True):
        asked["user_id"] = user_id
        return {}, [types.SimpleNamespace(platform="codeforces", handle="x")], []

    monkeypatch.setattr(bot, "_group_member_bindings", fake_members)
    monkeypatch.setattr(bot, "_load_bound_profiles", fake_profiles)
    monkeypatch.setattr(bot, "_profile_metric", staticmethod(lambda profile: {"Rating": 1500}))
    monkeypatch.setattr(bot, "_event_display_name", staticmethod(lambda event: "道长"))
    data = payload(run(bot.acmer_profile(FakeEvent(sender="self1"), user="道")))
    assert data["ok"] is True, data
    assert asked["user_id"] == "u7", asked


def test_profile_exact_match_beats_contains(monkeypatch) -> None:
    """同时存在「道长」与「道」时，精确的「道」优先（包含匹配会把名字挑错）。"""
    _module, bot, _settings = make_bot()
    asked: dict = {}

    async def fake_members(group_id, platform):
        return [("u1", "a", "h1", "道长"), ("u2", "b", "h2", "道")]

    async def fake_profiles(user_id, *, detail=True, force=False, record_metrics=True):
        asked["user_id"] = user_id
        return {}, [types.SimpleNamespace(platform="codeforces", handle="x")], []

    monkeypatch.setattr(bot, "_group_member_bindings", fake_members)
    monkeypatch.setattr(bot, "_load_bound_profiles", fake_profiles)
    monkeypatch.setattr(bot, "_profile_metric", staticmethod(lambda profile: {"Rating": 1}))
    monkeypatch.setattr(bot, "_event_display_name", staticmethod(lambda event: "提问者"))
    data = payload(run(bot.acmer_profile(FakeEvent(), user="道")))
    assert data["ok"] is True, data
    assert asked["user_id"] == "u2", asked


def test_profile_blank_own_name_does_not_make_everyone_self(monkeypatch) -> None:
    """适配器给不出昵称（空白）时，不能把任何人当"本人"。"""
    _module, bot, _settings = make_bot()
    asked: dict = {}

    async def fake_members(group_id, platform):
        return [("u7", "td1336065617", "645160704", "道")]

    async def fake_profiles(user_id, *, detail=True, force=False, record_metrics=True):
        asked["user_id"] = user_id
        return {}, [types.SimpleNamespace(platform="codeforces", handle="x")], []

    monkeypatch.setattr(bot, "_group_member_bindings", fake_members)
    monkeypatch.setattr(bot, "_load_bound_profiles", fake_profiles)
    monkeypatch.setattr(bot, "_profile_metric", staticmethod(lambda profile: {"Rating": 1}))
    monkeypatch.setattr(bot, "_event_display_name", staticmethod(lambda event: "   "))
    data = payload(run(bot.acmer_profile(FakeEvent(sender="self1"), user="道")))
    assert data["ok"] is True, data
    assert asked["user_id"] == "u7", asked


def test_profile_falls_back_to_all_bound_accounts(monkeypatch) -> None:
    """F3：已绑定但不在本群排行名册里的成员，也要能按昵称查到（与人类 @某人 对齐）。"""
    _module, bot, _settings = make_bot()
    asked: dict = {}

    async def no_roster(group_id, platform):
        return []

    async def fake_accounts():
        return {
            "u9": {
                "codeforces": {
                    "display_name": "td1336065617",
                    "platform_user_id": "645160704",
                    "qq_name": "道",
                }
            }
        }

    async def fake_profiles(user_id, *, detail=True, force=False, record_metrics=True):
        asked["user_id"] = user_id
        return {}, [types.SimpleNamespace(platform="codeforces", handle="x")], []

    monkeypatch.setattr(bot, "_group_member_bindings", no_roster)
    bot.account_registry = types.SimpleNamespace(get_all_accounts=fake_accounts)
    monkeypatch.setattr(bot.account_registry, "get_all_accounts", fake_accounts)
    monkeypatch.setattr(bot, "_load_bound_profiles", fake_profiles)
    monkeypatch.setattr(bot, "_profile_metric", staticmethod(lambda profile: {"Rating": 1}))
    monkeypatch.setattr(bot, "_event_display_name", staticmethod(lambda event: "提问者"))
    data = payload(run(bot.acmer_profile(FakeEvent(), user="道")))
    assert data["ok"] is True, data
    assert asked["user_id"] == "u9", asked


def test_profile_nickname_lookup_in_private_chat_is_rejected(monkeypatch) -> None:
    """F5：私聊没有"本群"，按昵称查人要明确拒绝并给出替代路径。"""
    _module, bot, _settings = make_bot()
    data = payload(run(bot.acmer_profile(FakeEvent(group_id=""), user="道")))
    assert data["ok"] is False and data["error"]["code"] == "platform_unsupported", data
    assert "username" in str(data["error"].get("hint") or "")


def test_daily_problem_rejects_platform_outside_pool(monkeypatch) -> None:
    """F9：请求的平台不在题目池里时必须报错，不能静默换成默认平台作答。"""
    _module, bot, _settings = make_bot()

    async def fake_settings():
        return {"acmer_ai_tools_enabled": True, "daily_problem_enabled": True, "push_platforms": ["nowcoder"], "daily_problem_platform": "nowcoder"}

    monkeypatch.setattr(bot, "get_settings", fake_settings)
    data = payload(run(bot.acmer_daily_problem(FakeEvent(), platform="atcoder")))
    assert data["ok"] is False and data["error"]["code"] == "invalid_argument", data


def test_group_rank_rejects_platform_not_in_rank(monkeypatch) -> None:
    """F9：offline 不在排行支持的平台里，不能静默给全平台数据。"""
    _module, bot, _settings = make_bot()
    data = payload(run(bot.acmer_group_rank(FakeEvent(), platform="offline")))
    assert data["ok"] is False and data["error"]["code"] == "invalid_argument", data
