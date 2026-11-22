from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WarbiterError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


EXIT_CODES = {
    "E_BAD_SCHEMA": 2,
    "E_BAD_SCRIPT": 2,
    "E_BAD_CONFIG": 2,
    "E_BAD_POLICY": 2,
    "E_BAD_ARGUMENT": 2,
    "E_NOT_INITIALIZED": 3,
    "E_SCHEMA_CHANGED": 3,
    "E_TX_CONFLICT": 3,
    "E_COMMIT_ORDER": 3,
    "E_POLICY_CHANGED": 3,
    "E_REPLAY_LIMIT": 4,
    "E_VERIFY_FAILED": 4,
    "E_DB": 5,
}

