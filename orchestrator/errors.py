"""Business error codes and structured exceptions.

All user-facing validation failures carry a stable business code so the CLI
layer can map them deterministically to process exit codes and JSON reports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List


class OrchestratorError(Exception):
    """Base class for structured, code-bearing errors."""

    code = "E_INTERNAL"

    def __init__(self, message: str, details: List[Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details: List[Any] = details or []

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class DagValidationError(OrchestratorError):
    code = "E_DAG"


class ExecutionError(OrchestratorError):
    code = "E_EXEC"


class VerifyError(OrchestratorError):
    code = "E_VERIFY"


class ReplayError(OrchestratorError):
    code = "E_REPLAY"


class SnapshotError(OrchestratorError):
    code = "E_SNAPSHOT"


@dataclass
class Issue:
    """A single validation deviation with a stable, sortable shape."""

    code: str
    stage_id: str
    field: str
    reason: str
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {
            "code": self.code,
            "stage_id": self.stage_id,
            "field": self.field,
            "reason": self.reason,
        }
        if self.evidence:
            out["evidence"] = self.evidence
        return out
