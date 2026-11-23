from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AuditError(Exception):
    code: str
    message: str
    tenant: str | None = None
    window_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.tenant is not None:
            payload["tenant"] = self.tenant
        if self.window_id is not None:
            payload["window_id"] = self.window_id
        if self.details:
            payload["details"] = self.details
        return payload


def validation_error(code: str, message: str, **details: Any) -> AuditError:
    return AuditError(code=code, message=message, details=details)
