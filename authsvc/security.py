"""恒定时间令牌比较工具。

令牌比较只使用 ``hmac.compare_digest``。比较前先对双方做 SHA-256 摘要，
使参与比较的字符串长度固定，从而避免因长度或前缀差异带来的时间侧信道。
"""

from __future__ import annotations

import hashlib
import hmac

_DIGEST_DOMAIN = b"authsvc-local-shared-token-v1:"


def digest_token(token_text: str) -> bytes:
    """把任意长度令牌映射为固定长度的 SHA-256 摘要。"""
    return hashlib.sha256(_DIGEST_DOMAIN + token_text.encode("utf-8")).digest()


def constant_time_token_match(supplied: str, expected: str) -> bool:
    """恒定时间比较两个令牌；字符串长度和内容差异不影响比较耗时。"""
    supplied_text = supplied if isinstance(supplied, str) else ""
    expected_text = expected if isinstance(expected, str) else ""
    return hmac.compare_digest(digest_token(supplied_text), digest_token(expected_text))
