"""令牌类型识别。

支持两种格式：
1. JWT：header.payload.signature，共 3 段（被剥离签名时表现为 2 段，
   且第一段能解出含 "alg" 的 JWT 头，仍按 JWT 处理以便报告风险）。
2. 自定义令牌：payload.signature，共 2 段，载荷为 Base64URL(JSON)，
   签名为 HMAC-SHA256 的十六进制字符串。
无法归入上述结构时抛 UnsupportedTokenError。
"""

from __future__ import annotations

import json

from .core import (
    MalformedTokenError,
    UnsupportedTokenError,
    b64url_decode,
    check_input,
)

TOKEN_TYPE_JWT = "jwt"
TOKEN_TYPE_CUSTOM = "custom"


def _looks_like_jwt_header(segment: str) -> bool:
    """2 段令牌的第一段若能解出含字符串字段 alg 的 JSON 对象，视为被剥离签名的 JWT。"""
    try:
        raw = b64url_decode(segment)
        header = json.loads(raw.decode("utf-8"))
    except (MalformedTokenError, ValueError, UnicodeDecodeError):
        return False
    return isinstance(header, dict) and isinstance(header.get("alg"), str)


def detect_token_type(token: str) -> str:
    """返回 TOKEN_TYPE_JWT / TOKEN_TYPE_CUSTOM，否则抛 UnsupportedTokenError。"""
    check_input(token)

    # JWT 与自定义格式都只允许 ASCII 点分结构；出现空白等字符直接判不支持。
    if any(ch.isspace() for ch in token):
        raise UnsupportedTokenError("令牌包含空白字符，结构不符")

    parts = token.split(".")

    if len(parts) == 3:
        # 标准 3 段结构：JWT。
        if any(part == "" for part in parts[:2]):
            raise UnsupportedTokenError("JWT 的头部或载荷段为空，结构不符")
        return TOKEN_TYPE_JWT

    if len(parts) == 2:
        header_or_payload, signature_segment = parts
        if header_or_payload == "":
            raise UnsupportedTokenError("令牌首段为空，结构不符")
        # 2 段且头部形态 -> JWT 签名剥离场景（验收点 3），仍归入 JWT 走风险分析。
        if _looks_like_jwt_header(header_or_payload):
            return TOKEN_TYPE_JWT
        # 否则按自定义 payload.signature 处理；签名段为空属于该格式下的损坏，
        # 由 custom 分析器报告“签名剥离”风险而非类型不支持。
        return TOKEN_TYPE_CUSTOM

    raise UnsupportedTokenError(
        f"令牌含 {len(parts)} 个点分段，既不是 JWT(3段) 也不是 payload.signature(2段)"
    )
