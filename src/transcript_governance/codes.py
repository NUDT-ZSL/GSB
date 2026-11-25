"""Structured business codes and diagnostics."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class Code:
    OK = "OK"
    E_FIELD_MISSING = "E_FIELD_MISSING"
    E_FIELD_TYPE = "E_FIELD_TYPE"
    E_FORMAT_HEADER = "E_FORMAT_HEADER"
    E_TIME_REGRESSION = "E_TIME_REGRESSION"
    E_DUPLICATE_UTTERANCE_ID = "E_DUPLICATE_UTTERANCE_ID"
    E_INVALID_INTERVAL = "E_INVALID_INTERVAL"
    E_METADATA_MISMATCH = "E_METADATA_MISMATCH"
    E_OVERLAP = "E_OVERLAP"
    E_GAP = "E_GAP"
    E_NOT_FOUND = "E_NOT_FOUND"
    E_CACHE_MISS = "E_CACHE_MISS"
    E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
    E_DEPENDENCY_CYCLE = "E_DEPENDENCY_CYCLE"
    E_CONFIG_IMPACT = "E_CONFIG_IMPACT"
    E_SNAPSHOT_VERSION_CONFLICT = "E_SNAPSHOT_VERSION_CONFLICT"
    E_SNAPSHOT_CHECKSUM_MISMATCH = "E_SNAPSHOT_CHECKSUM_MISMATCH"
    E_SNAPSHOT_STALE = "E_SNAPSHOT_STALE"
    E_STAGE_BUSY = "E_STAGE_BUSY"
    E_STAGE_NOT_FOUND = "E_STAGE_NOT_FOUND"
    E_VALIDATION = "E_VALIDATION"


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    severity: str = "error"
    source: str | None = None
    line: int | None = None
    utterance_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }
        if self.source is not None:
            data["source"] = self.source
        if self.line is not None:
            data["line"] = self.line
        if self.utterance_id is not None:
            data["utterance_id"] = self.utterance_id
        if self.details:
            data["details"] = self.details
        return data


class GovernanceError(RuntimeError):
    """Exception carrying the same structured diagnostic code."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details
        self.diagnostic = Diagnostic(code=code, message=message, details=details)
