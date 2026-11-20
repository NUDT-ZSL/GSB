from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_DUP_SAMPLE = "E_DUP_SAMPLE"
E_COMPUTE_BUDGET = "E_COMPUTE_BUDGET"
E_MEMORY_LIMIT = "E_MEMORY_LIMIT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_INCONSISTENT = "E_INCONSISTENT"
E_WORKER = "E_WORKER"
E_LEDGER_INSUFFICIENT = "E_LEDGER_INSUFFICIENT"


@dataclass
class PipelineError(Exception):
    code: str
    stage: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f"{self.code} [{self.stage}]: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "stage": self.stage,
            "message": self.message,
            "details": self.details,
        }

