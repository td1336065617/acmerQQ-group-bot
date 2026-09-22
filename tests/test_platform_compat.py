"""platform_compat 与群配置迁移测试。"""
from __future__ import annotations

from astrbot.api.message_components import At, Plain

import platform_compat as pc


class _Meta:
    def __init__(self, name, pid):
        self.name = name
        self.id = pid


class _Inst:
    def __init__(self, name, pid, scene=None):
        self._meta = _Meta(name, pid)
        self._session_scene = scene or {}

    def meta(self):
        return self._meta


class _PM:
    def __init__(self, insts):
        self.platform_insts = insts


class _Ctx:
    def __init__(self, insts):
        self.platform_manager = _PM(insts)


class _Event:
    def __init__(self, name, pid="p1"):
        self._name = name
        self._pid = pid

    def get_platform_name(self):
        return self._name

    def get_platform_id(self):
        return self._pid


def test_resolve_channel():
    assert pc.resolve_channel(_Event("qq_official")) == "official"
    assert pc.resolve_channel(_Event("qq_official_webhook")) == "official"
    assert pc.resolve_channel(_Event("aiocqhttp")) == "onebot"
    assert pc.resolve_channel(_Event("telegram")) == ""


def test_channel_of_platform_id():
    ctx = _Ctx([_Inst("qq_official", "爱莉希雅"), _Inst("aiocqhttp", "onebot-1")])
    assert pc.channel_of_platform_id(ctx, "爱莉希雅") == "official"
    assert pc.channel_of_platform_id(ctx, "onebot-1") == "onebot"
    assert pc.channel_of_platform_id(ctx, "unknown") == ""
    assert pc.channel_of_platform_id(ctx, "") == ""


def test_resolve_platform_id_prefers_event():
    ctx = _Ctx([_Inst("qq_official", "爱莉希雅")])
    assert pc.resolve_platform_id(ctx, event=_Event("aiocqhttp", "onebot-1")) == "onebot-1"


def test_resolve_platform_id_from_umo():
    ctx = _Ctx([])
    assert pc.resolve_platform_id(ctx, umo="onebot-1:GroupMessage:12345") == "onebot-1"


def test_resolve_platform_id_fallback():
    ctx = _Ctx([_Inst("aiocqhttp", "onebot-1"), _Inst("qq_official", "爱莉希雅")])
    assert pc.resolve_platform_id(ctx) == "爱莉希雅"
    ctx2 = _Ctx([_Inst("aiocqhttp", "onebot-1")])
    assert pc.resolve_platform_id(ctx2) == "onebot-1"
    assert pc.resolve_platform_id(_Ctx([])) == ""


def test_scene_ready_official():
    ctx = _Ctx([_Inst("qq_official", "爱莉希雅", {"g1": "group"})])
    assert pc.scene_ready(ctx, "g1", "爱莉希雅") is True
    assert pc.scene_ready(ctx, "g2", "爱莉希雅") is False


def test_scene_ready_onebot():
    ctx = _Ctx([_Inst("aiocqhttp", "onebot-1")])
    assert pc.scene_ready(ctx, "g1", "onebot-1") is True


def test_at_all_prefix():
    onebot = pc.at_all_prefix("onebot")
    assert isinstance(onebot[0], At) and str(onebot[0].qq) == "all"
    official = pc.at_all_prefix("official")
    assert isinstance(official[0], Plain)


def test_scoped_key():
    assert pc.scoped_key("p1", "g1") == "p1:g1"
    assert pc.scoped_key("", "g1") == "g1"
    assert pc.split_scoped("p1:g1") == ("p1", "g1")
    assert pc.split_scoped("g1") == ("", "g1")


def test_admin_entry_matches():
    assert pc.admin_entry_matches("u1", "p1", "u1") is True
    assert pc.admin_entry_matches("p1:u1", "p1", "u1") is True
    assert pc.admin_entry_matches("p2:u1", "p1", "u1") is False


def test_migrate_groups_legacy():
    raw = {"123": {"group_id": "123", "platform_id": "爱莉希雅", "activated": True}}
    out, changed = pc.migrate_groups(raw, "爱莉希雅")
    assert changed is True
    assert out == {"爱莉希雅:123": raw["123"]}


def test_migrate_groups_without_platform_id():
    raw = {"123": {"group_id": "123"}}
    out, changed = pc.migrate_groups(raw, "default-p")
    assert changed is True
    assert "default-p:123" in out


def test_migrate_groups_idempotent():
    raw = {"爱莉希雅:123": {"group_id": "123", "platform_id": "爱莉希雅"}}
    out, changed = pc.migrate_groups(raw, "爱莉希雅")
    assert changed is False
    assert out == raw
