"""后台「立即试跑」的幂等隔离测试：试跑不得写正式推送的幂等键。"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_main_accounts import _load_main_module
from src.models import GroupConfig


class FakeKV:
    def __init__(self):
        self.data = {}

    async def get(self, key, default=None):
        return self.data.get(key, default)

    async def put(self, key, value):
        self.data[key] = value


class _Row:
    rank = 1
    display_name = "小明"
    handle = "a"
    solved = 3
    total_problems = 5
    user_count = 10
    unsolved = ()

    def to_card_row(self):
        return {"handle": self.handle, "display_name": self.display_name}


class _Result:
    contest_name = "Test Round"
    rows = [_Row()]
    note = ""
    extra_note = ""

    def has_content(self):
        return True


def _bot(main_module, kv):
    bot = main_module.AcmerGroupBot.__new__(main_module.AcmerGroupBot)
    bot.get_kv_data = kv.get
    bot.put_kv_data = kv.put
    return bot


def test_settlement_run_does_not_write_idempotency_key():
    m = _load_main_module()
    kv = FakeKV()
    bot = _bot(m, kv)

    async def members(group_id, platform):
        return ["u1"]

    async def collect(platform, contest, member_list):
        return _Result()

    async def render(*args, **kwargs):
        return None

    async def send(group, text):
        return True

    bot._settlement_members = members
    bot.settlement = types.SimpleNamespace(collect=collect)
    bot._render_settlement_card = render
    bot.send_notification = send

    group = GroupConfig(group_id="g1")
    contest = types.SimpleNamespace(contest_id="1001", name="Round 1")
    pushed = asyncio.run(
        bot._push_settlement(
            group,
            "codeforces",
            contest,
            "settle_g1_codeforces_1001",
            min_participants=1,
            show_unsolved=False,
            platform_order=["codeforces"],
            write_key=False,
        )
    )
    assert pushed == 1
    assert "settle_g1_codeforces_1001" not in kv.data
    assert kv.data["push_log"][-1]["kind"] == "settle"


def test_settlement_default_still_writes_key():
    m = _load_main_module()
    kv = FakeKV()
    bot = _bot(m, kv)

    async def members(group_id, platform):
        return ["u1"]

    async def collect(platform, contest, member_list):
        return _Result()

    async def render(*args, **kwargs):
        return None

    async def send(group, text):
        return True

    bot._settlement_members = members
    bot.settlement = types.SimpleNamespace(collect=collect)
    bot._render_settlement_card = render
    bot.send_notification = send

    group = GroupConfig(group_id="g1")
    contest = types.SimpleNamespace(contest_id="1001", name="Round 1")
    pushed = asyncio.run(
        bot._push_settlement(
            group,
            "codeforces",
            contest,
            "settle_g1_codeforces_1001",
            min_participants=1,
            show_unsolved=False,
            platform_order=["codeforces"],
        )
    )
    assert pushed == 1
    assert kv.data["settle_g1_codeforces_1001"] is True


def test_weekly_run_does_not_write_idempotency_key():
    m = _load_main_module()
    kv = FakeKV()
    bot = _bot(m, kv)

    async def build_report(group):
        return {"cards": [], "text": "本周训练周报"}

    async def send(group, text):
        return True

    bot.build_weekly_report = build_report
    bot.send_notification = send

    group = GroupConfig(group_id="g1")
    ok = asyncio.run(
        bot._push_weekly_report_for_group(group, m.datetime.now(m.CN_TZ), write_key=False)
    )
    assert ok is True
    assert not [key for key in kv.data if key.startswith("weekly_")]
    assert kv.data["push_log"][-1]["kind"] == "weekly_report"


def test_weekly_default_still_writes_key():
    m = _load_main_module()
    kv = FakeKV()
    bot = _bot(m, kv)

    async def build_report(group):
        return {"cards": [], "text": "本周训练周报"}

    async def send(group, text):
        return True

    bot.build_weekly_report = build_report
    bot.send_notification = send

    group = GroupConfig(group_id="g1")
    ok = asyncio.run(
        bot._push_weekly_report_for_group(group, m.datetime.now(m.CN_TZ))
    )
    assert ok is True
    assert any(key.startswith("weekly_g1_") for key in kv.data)


def test_signup_send_helper_never_writes_keys():
    m = _load_main_module()
    kv = FakeKV()
    bot = _bot(m, kv)
    sent = []

    async def send(group, text):
        sent.append((group.group_id, text))
        return True

    bot.send_notification = send
    group = GroupConfig(group_id="g1")
    ok = asyncio.run(bot._send_signup_text(group, "报名即将截止"))
    assert ok is True
    assert sent == [("g1", "报名即将截止")]
    assert kv.data == {}
