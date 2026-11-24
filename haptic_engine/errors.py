from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class HapticError(Exception):
    code: str
    message: str
    details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        error = {
            "code": self.code,
            "message": self.message,
            "details": self.details or {},
        }
        return {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "error": error,
            "business_codes": [self.code],
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
