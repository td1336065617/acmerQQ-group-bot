"""后台任务引用与异常处理（BUG-044）。"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_main_accounts import _load_main_module


def test_spawn_keeps_reference_and_swallows_exception(caplog):
    async def scenario():
        main_module = _load_main_module()
        bot = main_module.AcmerGroupBot.__new__(main_module.AcmerGroupBot)
        seen = []

        async def ok():
            seen.append("ok")

        async def boom():
            raise RuntimeError("后台炸了")

        with caplog.at_level(logging.WARNING):
            bot._spawn(ok(), label="ok-task")
            assert isinstance(bot._bg_tasks, set) and len(bot._bg_tasks) == 1   # 持引用
            await asyncio.sleep(0)
            bot._spawn(boom(), label="boom-task")
            await asyncio.sleep(0.05)
        assert seen == ["ok"]
        assert bot._bg_tasks == set()                                        # 完成后自动移除
        assert any("后台任务 boom-task 失败" in r.message for r in caplog.records)

    asyncio.run(scenario())
