"""Structured business error codes for the 3D scene pipeline.

Every failure that crosses a worker/main-process boundary is represented by a
:class:`StageFailure`, carrying a stable machine-readable code, the pipeline
stage where it occurred and the ``scene_id`` it is attributed to.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional


E_INGEST_INVALID = "E_INGEST_INVALID"
E_FACE_BUDGET = "E_FACE_BUDGET"
E_TEX_CONSISTENCY = "E_TEX_CONSISTENCY"
E_MEMORY_BUDGET = "E_MEMORY_BUDGET"
E_REG_BUDGET = "E_REG_BUDGET"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_INCONSISTENT = "E_INCONSISTENT"
E_WORKER_FAULT = "E_WORKER_FAULT"


class PipelineError(Exception):
    """Base class for all pipeline errors carrying a structured code."""

    code = "E_PIPELINE"

    def __init__(self, message: str, *, scene_id: Optional[str] = None,
                 stage: Optional[str] = None, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.scene_id = scene_id
        self.stage = stage
        self.details = details or {}

    def to_failure(self) -> "StageFailure":
        return StageFailure(
            code=self.code,
            stage=self.stage or "unknown",
            scene_id=self.scene_id or "unknown",
            message=self.message,
            details=self.details,
        )


class IngestError(PipelineError):
    code = E_INGEST_INVALID


class FaceBudgetError(PipelineError):
    code = E_FACE_BUDGET


class TextureConsistencyError(PipelineError):
    code = E_TEX_CONSISTENCY


class MemoryBudgetError(PipelineError):
    code = E_MEMORY_BUDGET


class RegistrationBudgetError(PipelineError):
    code = E_REG_BUDGET


class SnapshotCorruptError(PipelineError):
    code = E_SNAPSHOT_CORRUPT


class StageBusyError(PipelineError):
    code = E_STAGE_BUSY


class InconsistentError(PipelineError):
    code = E_INCONSISTENT


class WorkerFault(PipelineError):
    """Synthetic fault injected/raised inside a worker for robustness tests."""

    code = E_WORKER_FAULT


@dataclass
class StageFailure:
    """Structured failure payload returned from a worker to the main process."""

    code: str
    stage: str
    scene_id: str
    message: str
    details: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "StageFailure":
        return cls(**payload)


def failure_from_exception(exc: BaseException, stage: str, scene_id: str) -> StageFailure:
    """Convert any exception into a structured :class:`StageFailure`."""
    if isinstance(exc, PipelineError):
        failure = exc.to_failure()
        if exc.stage is None:
            failure.stage = stage
        if exc.scene_id is None:
            failure.scene_id = scene_id
        return failure
    return StageFailure(
        code=E_WORKER_FAULT,
        stage=stage,
        scene_id=scene_id,
        message=f"{type(exc).__name__}: {exc}",
        details={"exception_type": type(exc).__name__},
    )
