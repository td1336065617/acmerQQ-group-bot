"""推送被拒分类与退避的单元测试（S1，台账 M1.2-M1.5）。

覆盖：四类错误识别、类型名优先、反向样本（含关键词但不归类）、归一化与脱敏、
退避序列、键格式与 scoped_key 等价（锁定两处格式不漂移）、分类器绝不抛异常。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import platform_compat
from src.push_health import (
    K_MUTED,
    K_NA,
    K_PERMISSION,
    K_RATE,
    K_TRANSIENT,
    K_UNKNOWN,
    classify,
    health_key,
    is_denied_kind,
    next_suspend_minutes,
    normalize_text,
    redact,
)


def test_classify_permission_by_text():
    result = classify(Exception("接口返回：主动消息失败, 无权限"))
    assert result["kind"] == K_PERMISSION
    assert result["matched_by"] == "text"
    assert result["pattern"] == "主动消息失败"  # 长模式优先


def test_classify_permission_by_type_name():
    class ForbiddenError(Exception):
        pass

    result = classify(ForbiddenError("some payload"))
    assert result["kind"] == K_PERMISSION
    assert result["matched_by"] == "type"
    assert result["pattern"] == "ForbiddenError"


def test_classify_muted():
    result = classify(Exception("机器人被禁言，无法发言"))
    assert result["kind"] == K_MUTED


def test_classify_rate_limited():
    result = classify(Exception("请求太频繁，请稍后再试"))
    assert result["kind"] == K_RATE


def test_classify_transient():
    result = classify(Exception("connection timeout while sending"))
    assert result["kind"] == K_TRANSIENT


def test_classify_unknown_and_na():
    # 反向样本：含「权限」二字但不在模式表里，不得归入权限类
    assert classify("跳过权限检查成功")["kind"] == K_UNKNOWN
    # OneBot 通道不适用本设计
    assert classify("主动消息失败, 无权限", "aiocqhttp")["kind"] == K_NA


def test_classify_never_raises():
    for bad in (None, object(), 42, b"raw-bytes"):
        result = classify(bad, "official")
        assert result["kind"] in (K_UNKNOWN, K_PERMISSION), result
    assert classify(None, "aiocqhttp")["kind"] == K_NA


def test_classify_extracts_http_code():
    result = classify(Exception("code=403 forbidden"))
    assert result["code"] == 403
    assert result["kind"] == K_PERMISSION


def test_normalize_fullwidth_case_and_spacing():
    assert normalize_text("ForbiddenError") == "forbiddenerror"
    assert normalize_text("主动　消息失败") == "主动 消息失败"  # 全角空格
    # 带散空格的中文也要能被 classify 命中（分类器双轨匹配）
    assert classify("主动 消息 失败 ，无 权限")["kind"] == K_PERMISSION


def test_redact_strips_ids_and_query():
    text = "send failed id=0123456789abcdef0123456789abcdef https://x.qq.com/g/msg?token=zzz&type=1"
    out = redact(text, 200)
    assert "0123456789abcdef0123456789abcdef" not in out
    assert "token=zzz" not in out
    assert "<id>" in out
    assert len(redact("x" * 999, 200)) <= 200


def test_backoff_sequence():
    assert [next_suspend_minutes(n) for n in (1, 2, 3, 4, 9)] == [30, 360, 720, 1440, 1440]
    assert next_suspend_minutes(0) == 30  # 退化为首次档位


def test_health_key_matches_scoped_key():
    assert health_key("爱莉希雅", "G1") == "push_health_" + platform_compat.scoped_key("爱莉希雅", "G1")
    assert health_key("爱莉希雅", "G1") == "push_health_爱莉希雅:G1"
    assert health_key("", "G1") == "push_health_G1"  # 平台为空的退化形


def test_is_denied_kind():
    assert is_denied_kind(K_PERMISSION) and is_denied_kind(K_MUTED)
    for kind in (K_RATE, K_TRANSIENT, K_UNKNOWN, K_NA):
        assert not is_denied_kind(kind)
