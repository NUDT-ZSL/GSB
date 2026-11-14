"""加固后的令牌校验函数（修复实现）。

设计原则（对应被怀疑的三类绕过）：
1. 算法白名单：JWT 仅接受 HS256，缺失/非字符串/其它值（含 none）一律拒绝，
   杜绝“替换 alg 标识蒙混通过”。
2. 签名段必须存在且完整，空签名/剥离签名直接拒绝。
3. 只用给定共享密钥重算 HMAC-SHA256 并常量时间比较，载荷任何字节改动都会失配。
4. 签名通过后才检查 exp，过期令牌拒绝。
5. 自定义 payload.signature 走同一套 HMAC-SHA256 校验逻辑。

输入级问题（超长、格式不支持、编码损坏）抛 TokenGuardError 子类；
令牌可识别但校验不通过时返回 passed=False 的结构化结果，不抛异常。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from .core import (
    MalformedTokenError,
    REASON_MESSAGES,
    b64url_decode,
    b64url_decode_json,
    check_input,
)
from .detector import TOKEN_TYPE_CUSTOM, TOKEN_TYPE_JWT, detect_token_type

ALLOWED_JWT_ALGORITHM = "HS256"


def _reject(reason: str, *, algorithm: str | None = None) -> dict:
    return {
        "passed": False,
        "reason": reason,
        "message": REASON_MESSAGES[reason],
        "algorithm": algorithm,
    }


def _normalize_key(key: str | bytes) -> bytes:
    if isinstance(key, str):
        return key.encode("utf-8")
    if isinstance(key, (bytes, bytearray)):
        return bytes(key)
    raise TypeError("共享密钥必须是 str 或 bytes")


def _check_exp(payload: dict) -> str | None:
    """签名通过后调用；返回过期原因码，未过期/无 exp 返回 None。"""
    if "exp" not in payload:
        return None
    exp = payload["exp"]
    # 只接受数值型时间戳，类型异常视为损坏，避免绕过比较逻辑。
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return "malformed"
    if exp < time.time():
        return "token_expired"
    return None


def _verify_jwt(token: str, key: bytes) -> dict:
    parts = token.split(".")
    header_b64 = parts[0]
    payload_b64 = parts[1]

    # 先解析头部，保证后续任何拒绝结果都能带上令牌声明的算法标识。
    try:
        header = b64url_decode_json(header_b64)
        algorithm = header.get("alg") if isinstance(header, dict) else None
    except MalformedTokenError:
        header = None
        algorithm = None

    # 验收点 3：只有 2 段说明签名被剥离（detector 已确认首段是 JWT 头）。
    if len(parts) == 2:
        return _reject("signature_stripped", algorithm=algorithm)
    signature_b64 = parts[2]

    if header is None:
        return _reject("malformed")

    # 验收点 2：严格白名单，none / 缺失 / 其它算法全部拒绝。
    if not isinstance(algorithm, str) or algorithm != ALLOWED_JWT_ALGORITHM:
        return _reject("alg_tampered", algorithm=algorithm)

    if signature_b64 == "":
        return _reject("signature_stripped", algorithm=algorithm)

    # 验收点 4：用给定密钥对“头部.载荷”原文重算 HMAC-SHA256。
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    expected = hmac.new(key, signing_input, hashlib.sha256).digest()
    try:
        provided = b64url_decode(signature_b64)
    except MalformedTokenError:
        return _reject("malformed", algorithm=algorithm)
    if not hmac.compare_digest(expected, provided):
        return _reject("payload_tampered", algorithm=algorithm)

    # 签名可信后才解析载荷并检查过期（验收点 8）。
    try:
        payload = b64url_decode_json(payload_b64)
    except MalformedTokenError:
        return _reject("malformed", algorithm=algorithm)

    reason = _check_exp(payload)
    if reason is not None:
        return _reject(reason, algorithm=algorithm)

    return {
        "passed": True,
        "reason": None,
        "message": "校验通过：算法白名单、HMAC-SHA256 签名、有效期均合法",
        "algorithm": algorithm,
    }


def _verify_custom(token: str, key: bytes) -> dict:
    payload_b64, signature_hex = token.split(".")

    # 自定义格式同样不允许空签名段。
    if signature_hex == "":
        return _reject("signature_stripped")

    # 签名必须是 HMAC-SHA256 的十六进制输出（32 字节 -> 64 个十六进制字符）。
    try:
        provided = bytes.fromhex(signature_hex)
    except ValueError:
        return _reject("malformed")
    if len(signature_hex) != 64 or len(provided) != 32:
        return _reject("malformed")

    # 验收点 6：对载荷段原文做 HMAC-SHA256，常量时间比较。
    expected_hex = hmac.new(
        key, payload_b64.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_hex, signature_hex.lower()):
        return _reject("signature_invalid")

    try:
        payload = b64url_decode_json(payload_b64)
    except MalformedTokenError:
        return _reject("malformed")

    reason = _check_exp(payload)
    if reason is not None:
        return _reject(reason)

    return {
        "passed": True,
        "reason": None,
        "message": "校验通过：自定义令牌 HMAC-SHA256 签名与有效期均合法",
        "algorithm": "HS256",
    }


def hardened_verify(token: str, key: str | bytes) -> dict:
    """加固校验入口。

    返回::

        {"passed": bool, "reason": str|None, "message": str, "algorithm": str|None}

    超长/空串/不支持的格式抛 TokenGuardError 子类，由调用方按错误处理。
    """
    check_input(token)
    key_bytes = _normalize_key(key)
    token_type = detect_token_type(token)

    if token_type == TOKEN_TYPE_JWT:
        return _verify_jwt(token, key_bytes)
    if token_type == TOKEN_TYPE_CUSTOM:
        return _verify_custom(token, key_bytes)
    # 理论不可达：detect_token_type 已覆盖所有分支。
    raise MalformedTokenError("未知令牌类型")  # pragma: no cover
