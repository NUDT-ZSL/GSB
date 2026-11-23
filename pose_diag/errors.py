"""Structured business errors shared by CLI, GUI, and tests."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any


@dataclass
class BusinessError(Exception):
    code: str
    message: str
    field: str | None = None
    scene_id: str | None = None
    details: dict[str, Any] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "business_code": self.code,
            "message": self.message,
        }
        if self.field is not None:
            payload["field"] = self.field
        if self.scene_id is not None:
            payload["scene_id"] = self.scene_id
        if self.details:
            payload["details"] = self.details
        return payload


def error_payload(error: BusinessError) -> dict[str, Any]:
    return error.to_dict()


def ok_payload(**details: Any) -> dict[str, Any]:
    return {"business_code": "OK", **details}

