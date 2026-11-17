"""Public data models and JSON-normalized report structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import ErrorCode


@dataclass(frozen=True)
class ResourceLimits:
    max_steps_per_session: int | None = 1000
    max_file_seconds: float | None = 15.0

    def __post_init__(self) -> None:
        if self.max_steps_per_session is not None and self.max_steps_per_session < 0:
            raise ValueError("max_steps_per_session must be non-negative or None")
        if self.max_file_seconds is not None and self.max_file_seconds < 0:
            raise ValueError("max_file_seconds must be non-negative or None")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_steps_per_session": self.max_steps_per_session,
            "max_file_seconds": self.max_file_seconds,
        }


@dataclass(frozen=True)
class JudgeConfig:
    max_renegotiations: int = 3

    def __post_init__(self) -> None:
        if self.max_renegotiations < 0:
            raise ValueError("max_renegotiations must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {"max_renegotiations": self.max_renegotiations}


@dataclass(frozen=True)
class Violation:
    code: ErrorCode
    session_id: int | None
    sequences: tuple[int, ...]
    sync_ids: tuple[int, ...]
    detail: str
    offset: int | None = None

    @property
    def min_sequence(self) -> int:
        return min(self.sequences) if self.sequences else -1

    def sort_key(self) -> tuple[int, int, tuple[int, ...], str]:
        return (self.min_sequence, int(self.code), self.sync_ids, self.detail)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.name,
            "code_id": int(self.code),
            "session_id": self.session_id,
            "sequences": list(self.sequences),
            "sync_ids": list(self.sync_ids),
            "offset": self.offset,
            "detail": self.detail,
        }


@dataclass
class SessionVerdict:
    session_id: int
    packet_count: int
    directions: tuple[int, ...]
    sequence_min: int
    sequence_max: int
    renegotiations: int
    violations: list[Violation] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "rejected" if self.violations else "valid"

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "packet_count": self.packet_count,
            "directions": list(self.directions),
            "sequence_min": self.sequence_min,
            "sequence_max": self.sequence_max,
            "renegotiations": self.renegotiations,
            "violations": [violation.to_dict() for violation in self.violations],
        }


@dataclass
class TruthSummary:
    complete: bool = False
    expected_records: int = 0
    expected_events: int = 0
    matched: int = 0
    mismatched: int = 0
    unexpected: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)

    @property
    def all_consistent(self) -> bool:
        return (
            self.complete
            and self.mismatched == 0
            and self.unexpected == 0
            and self.matched == self.expected_events
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "expected_records": self.expected_records,
            "expected_events": self.expected_events,
            "matched": self.matched,
            "mismatched": self.mismatched,
            "unexpected": self.unexpected,
            "all_consistent": self.all_consistent,
            "details": list(self.details),
        }


@dataclass
class FileResult:
    path: str
    file_id: str
    status: str
    version: int | None
    declared_message_count: int | None
    processed_messages: int
    sessions: list[SessionVerdict]
    parse_error: dict[str, Any] | None
    termination: str | None
    termination_detail: str | None
    truth: TruthSummary
    elapsed_seconds: float
    steps_by_session: dict[int, int]
    limits: dict[str, Any]
    checkpoint_reused: bool = False

    @property
    def observed_violation_count(self) -> int:
        return sum(len(session.violations) for session in self.sessions)

    @property
    def has_business_failure(self) -> bool:
        return (
            self.status != "ok"
            or self.observed_violation_count > 0
            or not self.truth.all_consistent
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "file_id": self.file_id,
            "status": self.status,
            "version": self.version,
            "declared_message_count": self.declared_message_count,
            "processed_messages": self.processed_messages,
            "steps_by_session": {
                str(key): value for key, value in sorted(self.steps_by_session.items())
            },
            "limits": dict(self.limits),
            "parse_error": self.parse_error,
            "termination": self.termination,
            "termination_detail": self.termination_detail,
            "truth": self.truth.to_dict(),
            "observed_violation_count": self.observed_violation_count,
            "sessions": [session.to_dict() for session in self.sessions],
            "elapsed_seconds": round(self.elapsed_seconds, 9),
            "checkpoint_reused": self.checkpoint_reused,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FileResult":
        from .analyzer import session_verdict_from_dict

        truth_data = data.get("truth", {})
        truth = TruthSummary(
            complete=bool(truth_data.get("complete", False)),
            expected_records=int(truth_data.get("expected_records", 0)),
            expected_events=int(truth_data.get("expected_events", 0)),
            matched=int(truth_data.get("matched", 0)),
            mismatched=int(truth_data.get("mismatched", 0)),
            unexpected=int(truth_data.get("unexpected", 0)),
            details=list(truth_data.get("details", [])),
        )
        return cls(
            path=str(data.get("path", "")),
            file_id=str(data.get("file_id", "")),
            status=str(data.get("status", "parse_error")),
            version=data.get("version"),
            declared_message_count=data.get("declared_message_count"),
            processed_messages=int(data.get("processed_messages", 0)),
            sessions=[
                session_verdict_from_dict(item) for item in data.get("sessions", [])
            ],
            parse_error=data.get("parse_error"),
            termination=data.get("termination"),
            termination_detail=data.get("termination_detail"),
            truth=truth,
            elapsed_seconds=float(data.get("elapsed_seconds", 0.0)),
            steps_by_session={
                int(key): int(value)
                for key, value in data.get("steps_by_session", {}).items()
            },
            limits=dict(data.get("limits", {})),
            checkpoint_reused=bool(data.get("checkpoint_reused", False)),
        )
