from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any


EXIT_CODES = {
    "E_STAGE_BUSY": 4,
    "E_INCONSISTENT": 5,
    "E_CACHE_CORRUPT": 6,
    "E_SNAPSHOT_ROLLBACK": 7,
}


@dataclass
class BusinessError(Exception):
    code: str
    message: str
    job_id: str | None = None
    field: str | None = None
    stage: str | None = None
    details: dict[str, Any] = dc_field(default_factory=dict)
    upstream_code: str | None = None
    category: str | None = None

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "job_id": self.job_id,
            "field": self.field,
            "stage": self.stage,
            "upstream_code": self.upstream_code,
            "category": self.category,
            "details": self.details,
        }

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.code, 2)


def error_record(
    error: BusinessError,
    *,
    source: str = "stage",
    injected: bool = False,
) -> dict[str, Any]:
    record = error.to_dict()
    record.pop("ok", None)
    record.update(
        {
            "type": "injection" if injected else "error",
            "source": source,
        }
    )
    return record

