"""ACMer 隔离用例：单独安装即可用，零跨插件 import。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_zero_cross_plugin_imports() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "check_no_cross_plugin_imports.py"), str(ROOT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ai_module_has_no_astrbot_dependency() -> None:
    """ai_impl / ai_common 必须是纯逻辑：AST 里不出现 astrbot 的 import（便于单独测试）。"""
    import ast

    for name in ("ai_impl.py", "ai_common.py"):
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith("astrbot") for alias in node.names), name
            elif isinstance(node, ast.ImportFrom):
                assert not str(node.module or "").startswith("astrbot"), name

def test_tools_are_registered_in_entry_module() -> None:
    """工具必须定义在入口模块 main.py 的插件类里（框架按入口模块绑定）。"""
    text = (ROOT / "main.py").read_text(encoding="utf-8")
    for tool in ("acmer_contests", "acmer_daily_problem", "acmer_profile", "acmer_group_rank"):
        assert "llm_tool(name=" in text and tool in text, tool
