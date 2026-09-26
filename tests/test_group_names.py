"""群名补全 + 「保存群配置」回归测试。

历史 bug：后台保存群配置时把整行回传，_build_groups_payload 又传了一次
group_id（GroupConfig(group_id=gid, **item)）→ TypeError → 保存永远失败。
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.group_names import event_group_name, fetch_group_name
from test_main_accounts import _load_main_module


class FakeKV:
    def __init__(self):
        self.data = {}
        self.puts = 0

    async def get(self, key, default=None):
        return self.data.get(key, default)

    async def put(self, key, value):
        self.puts += 1
        self.data[key] = value


class FakeBot:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if self.error is not None:
            raise self.error
        return self.payload


class FakeHttp:
    def __init__(self, payload=None):
        self.payload = payload
        self.routes = []

    async def request(self, route, **kwargs):
        self.routes.append(route)
        return self.payload


class FakeInst:
    def __init__(self, pid, name, bot=None, http=None):
        self._meta = types.SimpleNamespace(id=pid, name=name)
        self.bot = bot
        self.client = (
            types.SimpleNamespace(api=types.SimpleNamespace(_http=http)) if http else None
        )

    def meta(self):
        return self._meta


def _context(insts):
    return types.SimpleNamespace(
        platform_manager=types.SimpleNamespace(platform_insts=list(insts)),
        get_platform_inst=lambda pid: next(
            (i for i in insts if i.meta().id == pid), None
        ),
    )


def _bot(main_module, kv, insts=()):
    bot = main_module.AcmerGroupBot.__new__(main_module.AcmerGroupBot)
    bot.get_kv_data = kv.get
    bot.put_kv_data = kv.put
    bot.context = _context(insts)
    bot._groups_cache = None
    return bot


def test_build_groups_payload_accepts_frontend_row():
    """前端整行回传（含 group_id）必须能保存，且群名原样保留。"""
    m = _load_main_module()
    raw = m.AcmerGroupBot._build_groups_payload([
        {"group_id": "100", "platform_id": "p1", "name": "集训群", "enabled": False},
    ])
    assert len(raw) == 1
    cfg = next(iter(raw.values()))
    assert cfg["group_id"] == "100"
    assert cfg["name"] == "集训群"
    assert cfg["enabled"] is False


def test_remember_group_persists_name_once():
    m = _load_main_module()
    kv = FakeKV()
    bot = _bot(m, kv)
    key = m.platform_compat.scoped_key("p1", "100")

    async def scenario():
        await bot.remember_group("100", platform_id="p1", umo="umo1", name="集训群")
        assert kv.data["groups"][key]["name"] == "集训群"
        puts = kv.puts
        # 同 umo 同群名 → 命中内存缓存，不写 KV
        bot._groups_cache = (bot._groups_cache[0], bot._groups_cache[1])
        await bot.remember_group("100", platform_id="p1", umo="umo1", name="集训群")
        assert kv.puts == puts
        # 群名变化 → 写回
        await bot.remember_group("100", platform_id="p1", umo="umo1", name="改名了")
        assert kv.data["groups"][key]["name"] == "改名了"
        assert kv.puts > puts

    asyncio.run(scenario())


def test_remember_group_without_name_keeps_existing():
    m = _load_main_module()
    kv = FakeKV()
    bot = _bot(m, kv)
    key = m.platform_compat.scoped_key("p1", "100")

    async def scenario():
        await bot.remember_group("100", platform_id="p1", umo="umo1", name="集训群")
        kv.puts = 0
        bot._groups_cache = None
        await bot.remember_group("100", platform_id="p1", umo="umo2")
        assert kv.data["groups"][key]["name"] == "集训群"
        assert kv.data["groups"][key]["umo"] == "umo2"

    asyncio.run(scenario())


def test_event_group_name():
    event = types.SimpleNamespace(
        message_obj=types.SimpleNamespace(group=types.SimpleNamespace(group_name=" 集训群 "))
    )
    assert event_group_name(event) == "集训群"
    assert event_group_name(types.SimpleNamespace(message_obj=None)) == ""


def test_fetch_group_name_onebot_and_official():
    m = _load_main_module()
    bot_call = FakeBot({"group_name": "集训群"})
    http = FakeHttp({"group_name": "官方群"})
    context = _context([
        FakeInst("aiocqhttp", "aiocqhttp", bot=bot_call),
        FakeInst("qq_official", "qq_official", http=http),
    ])

    async def scenario():
        assert await fetch_group_name(context, "aiocqhttp", "100", "onebot") == "集训群"
        assert bot_call.calls[0][1]["group_id"] == 100
        assert await fetch_group_name(context, "qq_official", "C8D6", "official") == "官方群"
        assert http.routes[0].parameters.get("group_openid") == "C8D6"
        assert await fetch_group_name(context, "nope", "1", "onebot") == ""

    asyncio.run(scenario())


def test_refresh_group_names_fills_only_missing():
    m = _load_main_module()
    kv = FakeKV()
    http = FakeHttp({"group_name": "官方群"})
    bot_call = FakeBot({"group_name": "集训群"})
    insts = [
        FakeInst("qq_official", "qq_official", http=http),
        FakeInst("aiocqhttp", "aiocqhttp", bot=bot_call),
    ]
    bot = _bot(m, kv, insts)
    kv.data["groups"] = {
        m.platform_compat.scoped_key("qq_official", "C8D6"): {
            "group_id": "C8D6", "platform_id": "qq_official", "umo": "u1",
            "activated": True, "enabled": True, "morning_push_time": "08:00",
            "push_platforms": ["codeforces"], "reminder_enabled": True,
        },
        m.platform_compat.scoped_key("aiocqhttp", "100"): {
            "group_id": "100", "platform_id": "aiocqhttp", "name": "已有名字",
            "umo": "u2", "activated": True, "enabled": True,
            "morning_push_time": "08:00", "push_platforms": ["codeforces"],
            "reminder_enabled": True,
        },
    }

    async def scenario():
        result = await bot.refresh_group_names()
        assert result["pending"] == 1 and result["updated"] == 1 and result["failed"] == 0
        raw = kv.data["groups"]
        assert raw[m.platform_compat.scoped_key("qq_official", "C8D6")]["name"] == "官方群"
        assert raw[m.platform_compat.scoped_key("aiocqhttp", "100")]["name"] == "已有名字"

        again = await bot.refresh_group_names()
        assert again["pending"] == 0

    asyncio.run(scenario())
