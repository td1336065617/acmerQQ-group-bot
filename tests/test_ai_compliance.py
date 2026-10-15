"""ACMer 合规用例：配置契约（读写两处 + 页面）、错误形态、版本声明。"""

from __future__ import annotations

import ast
import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_common

AI_KEYS = ("acmer_ai_tools_enabled", "acmer_ai_throttle_limit", "acmer_ai_throttle_window")


def _function_source(name: str) -> str:
    """取 main.py 里某个函数的源码文本（用于断言"写回路径"也加了键）。"""
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError("找不到函数：" + name)


def test_config_contract_read_write_and_page() -> None:
    """三个 AI 键必须在 读取路径 + 写回路径 + 后台页面 三处都齐全。"""
    read_source = _function_source("get_settings")
    write_source = _function_source("_normalize_settings_payload")
    page = (ROOT / "pages" / "settings" / "index.html").read_text(encoding="utf-8")
    for key in AI_KEYS:
        assert key in read_source, "读取路径缺 " + key
        assert key in write_source, "写回路径缺 " + key + "（保存配置会把它清掉）"
        assert key in page, "后台页面缺控件 " + key


def test_error_codes_are_declared() -> None:
    for code in ("disabled", "throttled", "invalid_argument", "capability_unavailable", "not_found", "platform_unsupported"):
        assert code in ai_common.ERROR_CODES


def test_astrbot_version_declared() -> None:
    text = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    match = re.search(r"astrbot_version:\s*\"?([^\"\n]+)", text)
    assert match and match.group(1).strip().startswith(">=4")


def test_readme_has_no_mandatory_install_claim() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for bad in ("必须同时安装", "必须先安装其它插件"):
        assert bad not in readme


def test_throttle_rejects_and_reports_retry_after() -> None:
    throttle = ai_common.ToolThrottle(limit=1, window=60)
    assert throttle.check("t", "u", now=10) == 0
    wait = throttle.check("t", "u", now=11)
    assert wait > 0
    payload = ai_common.fail("throttled", "慢点", retry_after=wait)
    import json

    assert json.loads(payload)["error"]["retry_after"] == wait


def test_parse_int_rejects_out_of_range() -> None:
    assert ai_common.parse_int("7", 1, 30) == 7
    assert ai_common.parse_int("99", 1, 30) is None
    assert ai_common.parse_int("0", 1, 30) is None
    assert ai_common.parse_int("abc", 1, 30) is None


def test_version_matches_metadata() -> None:
    metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    from_meta = ""
    for line in metadata.splitlines():
        if line.startswith("version:"):
            from_meta = line.split(":", 1)[1].strip()
            break
    assert from_meta, "metadata.yaml 缺 version"
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert from_meta in changelog, "CHANGELOG 缺当前版本段：" + from_meta
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    version_line = ""
    for line in source.splitlines():
        if line.startswith("VERSION = "):
            version_line = line
            break
    assert version_line, "main.py 缺 VERSION 常量"
    declared = version_line.split("=", 1)[1].strip().strip(chr(34))
    assert declared == from_meta, (declared, from_meta)
