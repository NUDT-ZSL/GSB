"""业务错误与 E_xxx 业务码。

所有可预期的输入/运行错误都抛出 :class:`BusinessError`，由 CLI 统一序列化为
``{"ok": false, "error": {"code": ..., "message": ..., "details": ...}}``。
"""

from __future__ import annotations

import json
from typing import Any, Mapping


class BusinessError(Exception):
    """带稳定业务码的错误，保证调用方可以按码分支处理。"""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details or {})

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
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


E_JSON_PARSE = "E_JSON_PARSE"
E_SCENE_IO = "E_SCENE_IO"
E_SCENE_SCHEMA = "E_SCENE_SCHEMA"
E_DIMENSION = "E_DIMENSION"
E_ABSORPTION = "E_ABSORPTION"
E_COORDINATE = "E_COORDINATE"
E_DUPLICATE_POINT = "E_DUPLICATE_POINT"
E_SAMPLE_RATE = "E_SAMPLE_RATE"
E_LAYOUT = "E_LAYOUT"
E_ORDER = "E_ORDER"
E_BAND = "E_BAND"
E_WAV = "E_WAV"
E_BUDGET = "E_BUDGET"
E_STATE = "E_STATE"
E_OUTPUT = "E_OUTPUT"
E_INTERRUPTED = "E_INTERRUPTED"
E_INTERNAL = "E_INTERNAL"
E_OK = "E_OK"
