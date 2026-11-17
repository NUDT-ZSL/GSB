"""业务错误码与结构化错误类型。"""
from __future__ import annotations

E_CONFIG = "E_CONFIG"
E_COMMIT = "E_COMMIT"
E_PUSH = "E_PUSH"
E_SUB = "E_SUB"
E_SAMPLE = "E_SAMPLE"
E_VERSION = "E_VERSION"
E_VERIFY = "E_VERIFY"
E_REPLAY = "E_REPLAY"

ALL_CODES = frozenset(
    {E_CONFIG, E_COMMIT, E_PUSH, E_SUB, E_SAMPLE, E_VERSION, E_VERIFY, E_REPLAY}
)


class BizError(Exception):
    """携带业务码与结构化细节的错误。"""

    def __init__(self, code: str, message: str, details: dict | None = None):
        if code not in ALL_CODES:
            raise ValueError(f"unknown business code: {code}")
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}
