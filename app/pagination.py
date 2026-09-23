"""差异明细分页 cursor 的签发与校验。

cursor 为不透明字符串（base64url(JSON payload).base64url(HMAC-SHA256)），
内部绑定：

* ``scope`` —— 参与核对的任务集合（有序 job_ids）与筛选层级（level）的摘要；
* ``pos``   —— 上一页最后一条明细的固定排序键（keyset 分页定位点）；
* ``exp``   —— 过期时间戳。

篡改签名、过期、或把 cursor 拿到另一组任务 / 另一个 level 上复用，
``decode_cursor`` 都会抛出 :class:`InvalidCursor`，由接口层映射为 HTTP 400。
签名密钥为进程启动时随机生成，因此服务重启后所有旧 cursor 也会失效。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Optional

# 拒绝异常超长 cursor，避免无谓的解码/验签开销
MAX_CURSOR_CHARS = 2048
_VERSION = "cc1"


class InvalidCursor(Exception):
    """cursor 无法解码、签名不符、过期或绑定条件不匹配。"""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError) as exc:
        raise InvalidCursor("cursor 不是合法的 base64url 编码") from exc


def scope_hash(job_ids: list[str], level: str) -> str:
    """任务集合（保持请求顺序）+ 筛选层级的绑定摘要。"""
    digest = hashlib.sha256()
    digest.update(str(len(job_ids)).encode("ascii"))
    digest.update(b"|")
    for job_id in job_ids:
        digest.update(job_id.encode("utf-8"))
        digest.update(b"|")
    digest.update(level.encode("ascii"))
    return digest.hexdigest()


class CursorSigner:
    def __init__(self, key: Optional[bytes] = None) -> None:
        self._key = key or secrets.token_bytes(32)

    def encode(self, scope: str, level: str, position: list[Any], ttl_seconds: int,
               now: Optional[float] = None) -> str:
        payload = {
            "v": _VERSION,
            "lvl": level,
            "sc": scope,
            "pos": position,
            "exp": (time.time() if now is None else now) + max(ttl_seconds, 0),
        }
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return f"{_b64encode(body)}.{_b64encode(hmac.new(self._key, body, hashlib.sha256).digest())}"

    def _verified_payload(
        self, token: str, scope: str, now: Optional[float] = None
    ) -> dict[str, Any]:
        if not isinstance(token, str) or not token or len(token) > MAX_CURSOR_CHARS:
            raise InvalidCursor("cursor 为空或超出长度上限")
        parts = token.split(".")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise InvalidCursor("cursor 结构非法")
        body, sig = _b64decode(parts[0]), _b64decode(parts[1])
        expected = hmac.new(self._key, body, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            raise InvalidCursor("cursor 签名校验失败（已篡改或来自旧进程）")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise InvalidCursor("cursor 载荷无法解析") from exc
        if not isinstance(payload, dict) or payload.get("v") != _VERSION:
            raise InvalidCursor("cursor 版本不受支持")
        if not isinstance(payload.get("sc"), str) or not hmac.compare_digest(payload["sc"], scope):
            raise InvalidCursor("cursor 与当前任务集合或筛选层级不匹配")
        position = payload.get("pos")
        if not isinstance(position, list) or not position or not all(
            isinstance(part, (str, int, float, bool)) for part in position
        ):
            raise InvalidCursor("cursor 缺少合法的分页定位点")
        try:
            expires_at = float(payload["exp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidCursor("cursor 缺少合法的过期时间") from exc
        if expires_at <= (time.time() if now is None else now):
            raise InvalidCursor("cursor 已过期，请重新从第一页开始获取")
        return payload

    def decode_with_level(
        self,
        token: str,
        job_ids: list[str],
        expected_level: Optional[str] = None,
        now: Optional[float] = None,
    ) -> tuple[str, list[Any]]:
        """校验 cursor 并返回 (level, 定位点)。

        ``expected_level`` 给出时必须与 cursor 绑定的层级一致，否则视为跨条件复用。
        cursor 内的 scope 本身含 level，因此这里先做**不验签**的结构解析取出层级，
        随后立即按 ``scope_hash(job_ids, level)`` 完成完整验签/过期/绑定校验。
        """
        level = self.peek_level(token)
        if expected_level is not None and level != expected_level:
            raise InvalidCursor("cursor 与请求的 level 不匹配，不可跨层级复用")
        payload = self._verified_payload(
            token, scope_hash(job_ids, level), now=now
        )
        return level, payload["pos"]

    @staticmethod
    def peek_level(token: str) -> str:
        """仅做结构解析取出 cursor 绑定的层级（不验签，调用方必须再完整校验）。"""
        if not isinstance(token, str) or not token or len(token) > MAX_CURSOR_CHARS:
            raise InvalidCursor("cursor 为空或超出长度上限")
        parts = token.split(".")
        if len(parts) != 2 or not parts[0]:
            raise InvalidCursor("cursor 结构非法")
        try:
            payload = json.loads(_b64decode(parts[0]).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise InvalidCursor("cursor 载荷无法解析") from exc
        # scope 形如 sha256hex，无法反查 level；因此签发时冗余带上 level 字段
        level = payload.get("lvl") if isinstance(payload, dict) else None
        if level not in ("patient", "study", "series", "sop"):
            raise InvalidCursor("cursor 缺少合法的层级信息")
        return level


# 默认签名密钥只在进程内存活；多副本部署时 cursor 不跨副本，符合其短生命周期定位
_signer = CursorSigner()


def _cursor_ttl_seconds() -> int:
    # 延迟读取，便于测试通过环境变量/reload 配置调整过期时长
    from .config import settings
    return settings.cursor_ttl_seconds


def encode_cursor(scope: str, level: str, position: list[Any],
                  ttl_seconds: Optional[int] = None) -> str:
    return _signer.encode(
        scope, level, position,
        _cursor_ttl_seconds() if ttl_seconds is None else ttl_seconds,
    )


def decode_cursor(token: str, job_ids: list[str],
                  expected_level: Optional[str] = None) -> tuple[str, list[Any]]:
    """校验 cursor，返回 (level, 定位点)。

    expected_level 给出时必须与 cursor 层级一致；任务集合（有序 job_ids）
    或层级不匹配、签名错误、过期都会抛 InvalidCursor。
    """
    return _signer.decode_with_level(token, job_ids, expected_level=expected_level)
