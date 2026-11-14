"""token_guard：本地令牌检测与加固验证工具（仅依赖 Python 标准库）。"""

from .analyzer import analyze_token
from .core import (
    MAX_TOKEN_LENGTH,
    MalformedTokenError,
    TokenGuardError,
    TokenTooLongError,
    UnsupportedTokenError,
)
from .verifier import hardened_verify

__all__ = [
    "analyze_token",
    "hardened_verify",
    "MAX_TOKEN_LENGTH",
    "TokenGuardError",
    "UnsupportedTokenError",
    "TokenTooLongError",
    "MalformedTokenError",
]

__version__ = "1.0.0"
