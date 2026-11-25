from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


class ErrorCode:
    PLAN_INVALID = "E_PLAN_INVALID"
    NODES_INVALID = "E_NODES_INVALID"
    EVENTS_INVALID = "E_EVENTS_INVALID"
    INJECTIONS_INVALID = "E_INJECTIONS_INVALID"
    SPEC_INVALID = "E_SPEC_INVALID"
    SPEC_VERSION_CONFLICT = "E_SPEC_VERSION_CONFLICT"
    STALE_SPEC_REPORT = "E_STALE_SPEC_REPORT"
    STATE_INVALID = "E_STATE_INVALID"
    DB_INVALID = "E_DB_INVALID"
    CLI_INVALID = "E_CLI_INVALID"
    NOT_FOUND = "E_NOT_FOUND"


@dataclass
class EngineError(Exception):
    code: str
    message: str
    details: dict[str, Any] | None = None
    exit_code: int = 1

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "code": self.code,
            "error_code": self.code,
            "error": {
                "code": self.code,
                "message": self.message,
            },
        }
        if self.details is not None:
            payload["error"]["details"] = _jsonable(self.details)
        return payload

    def to_json(self) -> str:
        import json

        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return value
