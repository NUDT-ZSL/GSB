from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DiagError(Exception):
    code: str
    message: str
    fields: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "fields": self.fields,
            }
        }


def fail(code: str, message: str, **fields: Any) -> DiagError:
    return DiagError(code=code, message=message, fields=fields)
