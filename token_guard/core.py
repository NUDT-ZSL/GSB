"""公共常量、异常与编码工具。

所有模块仅使用 Python 标准库（hmac/hashlib/base64/json/time）。
"""

from __future__ import annotations

import base64
import json

# 验收点 9：令牌输入长度硬上限（字符数）。
MAX_TOKEN_LENGTH = 1000

# 风险严重等级：数值越大越严重；输出与排序均以此为准。
SEVERITY_CRITICAL = "critical"
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"

SEVERITY_ORDER = {
    SEVERITY_CRITICAL: 4,
    SEVERITY_HIGH: 3,
    SEVERITY_MEDIUM: 2,
    SEVERITY_LOW: 1,
}

# 加固校验拒绝原因 -> 中文说明，analyzer 与 verifier 共用，保证口径一致。
REASON_MESSAGES = {
    "unsupported_token": "不支持的令牌格式（既非 JWT 也非 payload.signature）",
    "token_too_long": f"令牌长度超过 {MAX_TOKEN_LENGTH} 字符上限",
    "malformed": "令牌结构或编码损坏，无法解析",
    "alg_tampered": "算法标识被篡改或不被允许（仅允许 HS256，拒绝 none）",
    "signature_stripped": "签名段缺失，疑似签名剥离",
    "signature_invalid": "签名与载荷不匹配（载荷或签名已被篡改）",
    "payload_tampered": "载荷被改动，签名校验失败",
    "token_expired": "令牌已过期（exp 早于当前时间）",
}


class TokenGuardError(Exception):
    """工具级错误的基类：携带稳定的错误 code，便于程序化处理。"""

    code = "token_guard_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class UnsupportedTokenError(TokenGuardError):
    """无法识别为 JWT 或自定义 payload.signature 格式。"""

    code = "unsupported_token"


class TokenTooLongError(TokenGuardError):
    """输入令牌长度超过 MAX_TOKEN_LENGTH。"""

    code = "token_too_long"


class MalformedTokenError(TokenGuardError):
    """结构看似某类令牌，但 Base64/JSON/签名编码损坏。"""

    code = "malformed"


def b64url_encode(data: bytes) -> str:
    """无填充的 Base64URL 编码（JWT 标准编码方式）。"""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(segment: str) -> bytes:
    """解码无填充 Base64URL 段；字符集非法时抛 MalformedTokenError。"""
    if not segment:
        raise MalformedTokenError("Base64URL 段为空")
    # 只允许 Base64URL 字母表，补齐 "=" 后用 validate 严格校验。
    padded = segment + "=" * (-len(segment) % 4)
    try:
        # urlsafe_b64decode 不支持 validate，故直接用 b64decode + urlsafe 替代表。
        return base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise MalformedTokenError(f"Base64URL 解码失败: {exc}") from exc


def b64url_decode_json(segment: str) -> dict:
    """解码并解析 JSON 对象；结果不是对象时按损坏处理。"""
    raw = b64url_decode(segment)
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise MalformedTokenError(f"JSON 解析失败: {exc}") from exc
    if not isinstance(obj, dict):
        raise MalformedTokenError("编码内容不是 JSON 对象")
    return obj


def check_input(token: object) -> str:
    """输入类型与长度的统一入口检查（验收点 9、验收点 1 的空串场景）。"""
    if not isinstance(token, str):
        raise UnsupportedTokenError("令牌必须是字符串")
    if len(token) == 0:
        raise UnsupportedTokenError("空字符串不是合法令牌")
    if len(token) > MAX_TOKEN_LENGTH:
        raise TokenTooLongError(
            f"令牌长度 {len(token)} 字符，超过上限 {MAX_TOKEN_LENGTH} 字符"
        )
    return token
