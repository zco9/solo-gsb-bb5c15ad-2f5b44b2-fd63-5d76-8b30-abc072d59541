"""差异分页 cursor 的签发/校验测试（绑定、篡改、过期、跨条件复用）。"""
from __future__ import annotations

import json
import time

import pytest

from app import pagination


@pytest.fixture()
def signer():
    return pagination.CursorSigner(key=b"k" * 32)


def test_encode_then_decode_roundtrip(signer):
    token = signer.encode(pagination.scope_hash(["j1", "j2"], "sop"), "sop",
                          ["1", "1.2.3"], ttl_seconds=60)
    level, pos = signer.decode_with_level(token, ["j1", "j2"])
    assert level == "sop"
    assert pos == ["1", "1.2.3"]


def test_decode_rejects_tampered_signature(signer):
    token = signer.encode(pagination.scope_hash(["j1", "j2"], "sop"), "sop",
                          ["1"], ttl_seconds=60)
    head, _, sig = token.rpartition(".")
    # 用全零签名替换（base64url 合法字符，HMAC 不可能恰好为全零）
    with pytest.raises(pagination.InvalidCursor):
        signer.decode_with_level(f"{head}.{''.join('A' for _ in sig)}", ["j1", "j2"])
    # 在 body 前缀插入字符后，原签名也必然不匹配
    with pytest.raises(pagination.InvalidCursor):
        signer.decode_with_level(f"AAAA{head}.{sig}", ["j1", "j2"])


def test_decode_rejects_tampered_payload(signer):
    token = signer.encode(pagination.scope_hash(["j1", "j2"], "sop"), "sop",
                          ["1"], ttl_seconds=60)
    # 改动 payload body 后签名必然不匹配
    head, _, _sig = token.rpartition(".")
    with pytest.raises(pagination.InvalidCursor):
        signer.decode_with_level(f"{head}x.aaaa", ["j1", "j2"])


def test_expired_cursor_rejected(signer):
    now = 1000.0
    token = signer.encode(pagination.scope_hash(["j1"], "patient"), "patient",
                          ["id:P1"], ttl_seconds=10, now=now)
    assert signer.decode_with_level(token, ["j1"], now=now + 9)[0] == "patient"
    with pytest.raises(pagination.InvalidCursor):
        signer.decode_with_level(token, ["j1"], now=now + 10)
    with pytest.raises(pagination.InvalidCursor):
        signer.decode_with_level(token, ["j1"], now=now + 999)


def test_zero_ttl_cursor_immediately_expired(signer):
    token = signer.encode(pagination.scope_hash(["j1"], "sop"), "sop", ["1"], ttl_seconds=0)
    with pytest.raises(pagination.InvalidCursor):
        signer.decode_with_level(token, ["j1"])


def test_cursor_bound_to_job_set(signer):
    token = signer.encode(pagination.scope_hash(["j1", "j2"], "sop"), "sop",
                          ["1"], ttl_seconds=60)
    # 换成另一组任务 -> scope 不匹配
    with pytest.raises(pagination.InvalidCursor):
        signer.decode_with_level(token, ["j1", "j3"])
    with pytest.raises(pagination.InvalidCursor):
        signer.decode_with_level(token, ["j2", "j1"])
    with pytest.raises(pagination.InvalidCursor):
        signer.decode_with_level(token, ["j1", "j2", "j3"])


def test_cursor_bound_to_level(signer):
    token = signer.encode(pagination.scope_hash(["j1", "j2"], "sop"), "sop",
                          ["1"], ttl_seconds=60)
    # 不带期望层级：可解出 sop
    level, _ = signer.decode_with_level(token, ["j1", "j2"])
    assert level == "sop"
    # 显式声明其他层级 -> 跨层级复用被拒绝
    with pytest.raises(pagination.InvalidCursor):
        signer.decode_with_level(token, ["j1", "j2"], expected_level="study")
    # study cursor 拿到 sop scope 上也必须失败（scope 内含 level）
    study_token = signer.encode(pagination.scope_hash(["j1", "j2"], "study"), "study",
                                ["0", "1.1"], ttl_seconds=60)
    with pytest.raises(pagination.InvalidCursor):
        signer.decode_with_level(study_token, ["j1", "j2"], expected_level="sop")


@pytest.mark.parametrize("bad", [
    "",
    "garbage",
    "a.b.c",
    ".",
    "!!!.###",
    "aaaa.bbbb",  # 合法 base64 但内容不是 JSON/版本不符
])
def test_malformed_cursor_rejected(signer, bad):
    with pytest.raises(pagination.InvalidCursor):
        signer.decode_with_level(bad, ["j1", "j2"])


def test_oversized_cursor_rejected(signer):
    long_token = "a" * (pagination.MAX_CURSOR_CHARS + 1)
    with pytest.raises(pagination.InvalidCursor):
        signer.decode_with_level(long_token, ["j1"])


def test_peek_level_does_not_validate_signature(signer):
    token = signer.encode(pagination.scope_hash(["j1"], "series"), "series",
                          ["0", "x"], ttl_seconds=60)
    assert signer.peek_level(token) == "series"
    # 手工构造“结构正确、携带层级但签名错误”的 token：peek 可取层级，完整校验失败
    import base64
    import json
    fake_body = base64.urlsafe_b64encode(
        json.dumps({"v": "cc1", "lvl": "series", "sc": "x", "pos": ["0"],
                    "exp": 9e18}).encode()
    ).decode().rstrip("=")
    tampered = f"{fake_body}.AAAAAAAAAAAAAAAA"
    assert signer.peek_level(tampered) == "series"
    with pytest.raises(pagination.InvalidCursor):
        signer.decode_with_level(tampered, ["j1"])


def test_module_level_helpers_use_settings_ttl(monkeypatch):
    from app import config
    # settings 是 frozen dataclass，monkeypatch 无法直接改属性
    monkeypatch.setattr(
        type(config.settings), "cursor_ttl_seconds",
        property(lambda self: 0),
    )
    token = pagination.encode_cursor(
        pagination.scope_hash(["j1"], "sop"), "sop", ["1"]
    )
    with pytest.raises(pagination.InvalidCursor):
        pagination.decode_cursor(token, ["j1"])


def test_scope_hash_order_sensitive():
    assert pagination.scope_hash(["j1", "j2"], "sop") != pagination.scope_hash(
        ["j2", "j1"], "sop"
    )
    assert pagination.scope_hash(["j1"], "sop") != pagination.scope_hash(
        ["j1"], "study"
    )
