from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_BAD_SAMPLE = "E_BAD_SAMPLE"
E_BACKLOG_LIMIT = "E_BACKLOG_LIMIT"
E_SUBSCRIBER_NOT_FOUND = "E_SUBSCRIBER_NOT_FOUND"
E_EVENT_NOT_FOUND = "E_EVENT_NOT_FOUND"
E_DELIVERY_NOT_FOUND = "E_DELIVERY_NOT_FOUND"
E_CLOCK_REGRESSION = "E_CLOCK_REGRESSION"
E_SUBSCRIBER_MISMATCH = "E_SUBSCRIBER_MISMATCH"
E_EVENT_CONFLICT = "E_EVENT_CONFLICT"
E_DB_CONFLICT = "E_DB_CONFLICT"
E_SEQUENCE_VIOLATION = "E_SEQUENCE_VIOLATION"
E_NOT_LOADED = "E_NOT_LOADED"
E_STORAGE = "E_STORAGE"


_HTTP_STATUS = {
    E_BAD_SAMPLE: 422,
    E_BACKLOG_LIMIT: 409,
    E_SUBSCRIBER_NOT_FOUND: 404,
    E_EVENT_NOT_FOUND: 404,
    E_DELIVERY_NOT_FOUND: 404,
    E_CLOCK_REGRESSION: 409,
    E_SUBSCRIBER_MISMATCH: 409,
    E_EVENT_CONFLICT: 409,
    E_DB_CONFLICT: 409,
    E_SEQUENCE_VIOLATION: 409,
    E_NOT_LOADED: 404,
    E_STORAGE: 500,
}


@dataclass
class GatewayError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    http_status: int | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message)
        if self.http_status is None:
            self.http_status = _HTTP_STATUS.get(self.code, 400)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


def bad_sample(message: str, **details: Any) -> GatewayError:
    return GatewayError(E_BAD_SAMPLE, message, details)
