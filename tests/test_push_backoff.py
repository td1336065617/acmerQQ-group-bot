"""推送失败退避与告警限流的回归测试（BUG-042）。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_main_accounts import _load_main_module

from src.models import GroupConfig


def _build_bot(main_module, *, send_ok=True, scene_ready=True):
    main_module.platform_compat.channel_of_platform_id = lambda ctx, pid: "onebot"
    bot = main_module.AcmerGroupBot.__new__(main_module.AcmerGroupBot)
    bot.context = None
    bot._kv = {}
    bot._sent = []

    async def get_kv_data(key, default=None):
        return bot._kv.get(key, default)

    async def put_kv_data(key, value):
        bot._kv[key] = value

    async def get_settings():
        return {"at_all_enabled": False}

    def _group_scene_ready(group_id, platform_id=None):
        return scene_ready

    async def _post_to_group(group_id, text, **kwargs):
        bot._sent.append((group_id, text))
        return send_ok

    bot.get_kv_data = get_kv_data
    bot.put_kv_data = put_kv_data
    bot.get_settings = get_settings
    bot._group_scene_ready = _group_scene_ready
    bot._post_to_group = _post_to_group
    return bot


GROUP = GroupConfig(group_id="g1", push_platforms=["codeforces"])


def test_failures_enter_cooldown_after_three_attempts():
    async def scenario():
        main_module = _load_main_module()
        bot = _build_bot(main_module, send_ok=False)
        for _ in range(main_module.PUSH_FAIL_MAX_ATTEMPTS):
            assert await bot.send_notification(GROUP, "hi") is False
        assert len(bot._sent) == main_module.PUSH_FAIL_MAX_ATTEMPTS
        # 冷却期内不再重试（连发送动作都不做）
        assert await bot.send_notification(GROUP, "hi") is False
        assert len(bot._sent) == main_module.PUSH_FAIL_MAX_ATTEMPTS
        assert bot._kv["pushfail_group_g1"]["n"] == main_module.PUSH_FAIL_MAX_ATTEMPTS

    asyncio.run(scenario())


def test_success_clears_failure_counter():
    async def scenario():
        main_module = _load_main_module()
        bot = _build_bot(main_module, send_ok=False)
        await bot.send_notification(GROUP, "hi")
        assert bot._kv["pushfail_group_g1"]["n"] == 1
        async def _post_ok(*args, **kwargs):
            return True

        bot._post_to_group = _post_ok
        assert await bot.send_notification(GROUP, "hi") is True
        assert bot._kv["pushfail_group_g1"] == {}

    asyncio.run(scenario())


def test_scene_not_ready_is_throttled_and_not_counted():
    async def scenario():
        main_module = _load_main_module()
        bot = _build_bot(main_module, scene_ready=False)
        for _ in range(5):
            assert await bot.send_notification(GROUP, "hi") is False
        # 会话未就绪属于「等群内消息」，不进失败退避计数
        assert "pushfail_group_g1" not in bot._kv
        assert bot._sent == []

    asyncio.run(scenario())


def test_push_attempt_gate_limits_count_and_spacing():
    """BUG-026 护栏：同一天最多 3 次尝试，且两次间隔不少于 10 分钟。"""

    async def scenario():
        main_module = _load_main_module()
        bot = _build_bot(main_module)
        assert await bot.push_attempt_allowed("morning", "k") is True
        await bot.note_push_attempt("morning", "k")
        # 刚记过一次 -> 10 分钟内不再尝试
        assert await bot.push_attempt_allowed("morning", "k") is False
        # 次数用尽 -> 即使间隔够了也不再尝试
        bot._kv["pushattempt_morning_k"] = {
            "n": main_module.AcmerGroupBot.PUSH_ATTEMPT_MAX,
            "ts": 0.0,
        }
        assert await bot.push_attempt_allowed("morning", "k") is False
        await bot.clear_push_attempts("morning", "k")
        assert await bot.push_attempt_allowed("morning", "k") is True

    asyncio.run(scenario())
