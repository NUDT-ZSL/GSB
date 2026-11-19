"""Stable error codes shared by the pipeline, CLI and validation tool."""

from __future__ import annotations

from typing import Any, Mapping


class PipelineError(Exception):
    """Base class for all explicitly adjudicated pipeline failures.

    Each error carries a stable machine-readable ``code`` plus a context map so
    callers can inspect, serialize or assert on the failure deterministically.
    """

    code: str = "E_PIPELINE"

    def __init__(self, message: str, context: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = dict(context or {})

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "context": self.context}


class ChunkMismatchError(PipelineError):
    code = "E_CHUNK_MISMATCH"


class LatencyBudgetError(PipelineError):
    code = "E_LATENCY_BUDGET"


class SnapshotCorruptError(PipelineError):
    code = "E_SNAPSHOT_CORRUPT"


class StageBusyError(PipelineError):
    code = "E_STAGE_BUSY"


class InconsistentError(PipelineError):
    code = "E_INCONSISTENT"


class InvalidInputError(PipelineError):
    code = "E_INVALID_INPUT"


class ConfigError(PipelineError):
    code = "E_CONFIG"


ERROR_CODES: tuple[str, ...] = (
    "E_CHUNK_MISMATCH",
    "E_LATENCY_BUDGET",
    "E_SNAPSHOT_CORRUPT",
    "E_STAGE_BUSY",
    "E_INCONSISTENT",
    "E_INVALID_INPUT",
    "E_CONFIG",
)
