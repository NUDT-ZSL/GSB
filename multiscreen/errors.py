from __future__ import annotations

import json
from typing import Any

E_SCHEMA = "E_SCHEMA"
E_DUP_SESSION = "E_DUP_SESSION"
E_DUP_RECEIVER = "E_DUP_RECEIVER"
E_GEOMETRY = "E_GEOMETRY"
E_CLOCK = "E_CLOCK"
E_FRAME_ORDER = "E_FRAME_ORDER"
E_FRAME_TYPE = "E_FRAME_TYPE"
E_REFERENCE = "E_REFERENCE"
E_TIMELINE = "E_TIMELINE"
E_DIGEST = "E_DIGEST"
E_REPLAY = "E_REPLAY"
E_BUDGET = "E_BUDGET"
E_INTERRUPT = "E_INTERRUPT"
E_LOCK = "E_LOCK"
E_IO = "E_IO"


class ScreenError(Exception):
    """带稳定 E_xxx 业务码的可预期错误。"""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)


def error_json(exc: BaseException) -> str:
    if isinstance(exc, ScreenError):
        return exc.to_json()
    return json.dumps(
        {
            "ok": False,
            "error": {
                "code": E_IO,
                "message": str(exc) or exc.__class__.__name__,
                "details": {"type": exc.__class__.__name__},
            },
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
