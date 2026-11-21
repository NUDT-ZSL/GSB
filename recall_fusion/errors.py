"""Structured business error codes used across the recall/fusion pipeline.

Every error carries a stable machine-readable code so that worker processes can
report failures back to the main process without losing stage/query attribution.
"""

from __future__ import annotations

from dataclasses import dataclass


E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_RECALL_BUDGET = "E_RECALL_BUDGET"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_WORKER_FAILURE = "E_WORKER_FAILURE"


@dataclass(frozen=True)
class BusinessError(Exception):
    """Raised for expected, code-defined failure conditions.

    ``stage`` names the pipeline stage that detected the problem and ``field``
    optionally points at the offending configuration/snapshot field.
    """

    code: str
    message: str
    stage: str = ""
    field: str = ""
    query_id: str = ""

    def __str__(self) -> str:  # pragma: no cover - trivial
        loc = ".".join(part for part in (self.stage, self.field) if part)
        head = f"{self.code}({loc})" if loc else self.code
        q = f" query_id={self.query_id}" if self.query_id else ""
        return f"{head}: {self.message}{q}"

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
            "field": self.field,
            "query_id": self.query_id,
        }
