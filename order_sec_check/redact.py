"""凭据脱敏工具。

两条防线：
1. 结构化脱敏：已知敏感请求头的 value 一律替换为 ***REDACTED***；
2. 文本级脱敏：把已知凭据原文从任意文本中剔除（挂在日志 Filter 上兜底）。
"""

from __future__ import annotations

from typing import Iterable

REDACTION_PLACEHOLDER = "***REDACTED***"

# 头部名称（小写）命中以下任一子串即视为敏感头部
_SENSITIVE_NAME_MARKERS = (
    "token",
    "authorization",
    "cookie",
    "secret",
    "api-key",
    "apikey",
    "credential",
    "password",
)


def is_sensitive_header(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in _SENSITIVE_NAME_MARKERS)


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """返回脱敏后的头部字典副本，不修改入参。"""
    safe: dict[str, str] = {}
    for name, value in headers.items():
        if is_sensitive_header(name):
            safe[name] = REDACTION_PLACEHOLDER
        else:
            safe[name] = redact_text(value, ())
    return safe


def redact_text(text: str, secrets: Iterable[str]) -> str:
    """把文本中出现的凭据原文替换为占位符。"""
    if not text:
        return text
    result = text
    for secret in secrets:
        if secret and secret in result:
            result = result.replace(secret, REDACTION_PLACEHOLDER)
    return result


class SecretRedactionFilter:
    """日志兜底过滤器：任何日志消息中出现凭据原文都会被替换。"""

    def __init__(self, secrets: Iterable[str]):
        # 长凭据优先替换，避免短串是长串子串时残留
        self._secrets = tuple(sorted({s for s in secrets if s}, key=len, reverse=True))

    def filter(self, record: object) -> bool:
        try:
            msg = record.getMessage()  # type: ignore[attr-defined]
        except Exception:
            return True
        redacted = redact_text(msg, self._secrets)
        record.msg = redacted  # type: ignore[attr-defined]
        record.args = ()  # type: ignore[attr-defined]
        return True
