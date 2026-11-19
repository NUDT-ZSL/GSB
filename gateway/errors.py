from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class GatewayError(Exception):
    code: str
    message: str
    http_status: int = 400
    stage: str = "request"
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
            "details": self.details,
        }


def config_error(code: str, message: str, field_name: str, **details: Any) -> GatewayError:
    payload = {"field": field_name}
    payload.update(details)
    return GatewayError(code, message, 400, "config", payload)
