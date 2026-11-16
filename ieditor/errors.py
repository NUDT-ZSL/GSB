"""Business error codes and structured failure handling.

All business codes used across parsing, conversion, rendering, snapshots and
replay are defined here so that the interactive CLI, batch pipeline and replay
engine emit identical field names and codes.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


E_FORMAT_INVALID = "E_FORMAT_INVALID"
E_VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
E_SNAPSHOT_STALE = "E_SNAPSHOT_STALE"
E_RENDER_FAILED = "E_RENDER_FAILED"
E_CONVERSION_FAILED = "E_CONVERSION_FAILED"
E_RECOVERY_FAILED = "E_RECOVERY_FAILED"
E_OK = "E_OK"


class IEditorError(Exception):
    """Base exception carrying a business code and a structured context."""

    code = E_FORMAT_INVALID

    def __init__(self, message: str, code: Optional[str] = None,
                 context: Optional[dict] = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        self.context = context or {}


class FormatInvalid(IEditorError):
    code = E_FORMAT_INVALID


class VersionUnsupported(IEditorError):
    code = E_VERSION_UNSUPPORTED


class SnapshotStale(IEditorError):
    code = E_SNAPSHOT_STALE


@dataclass
class FailureRecord:
    """One entry in the failure list produced by every processing stage."""

    stage: str
    code: str
    message: str
    source_path: str = ""
    location: Any = None
    exception_type: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FailureList:
    """Accumulates failures while allowing processing of other projects."""

    records: list = field(default_factory=list)

    def add(self, stage: str, exc: BaseException, source_path: str = "",
            location: Any = None) -> FailureRecord:
        if isinstance(exc, IEditorError):
            code = exc.code
            if not location and exc.context:
                location = exc.context
        else:
            code = E_FORMAT_INVALID
        record = FailureRecord(
            stage=stage,
            code=code,
            message=str(exc),
            source_path=source_path,
            location=location,
            exception_type=type(exc).__name__,
        )
        self.records.append(record)
        return record

    def to_dict_list(self) -> list:
        return [r.to_dict() for r in self.records]
