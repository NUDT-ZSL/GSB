"""Core data models shared across layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Rejection:
    """A refused declaration entry."""

    index: int           # 0-based position of the entry in the source file
    rule_id: str | None  # declared id if one was present, else None
    codes: tuple[str, ...]
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "rule_id": self.rule_id,
            "codes": list(self.codes),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class NormalizedRule:
    """A validated, normalized rule. ``raw`` keeps the original declaration
    so reports remain reproducible from the output alone."""

    rule_id: str
    target: str
    action: str
    priority: int
    version: str
    depends_on: tuple[str, ...]
    condition_text: str
    # DNF: tuple of conjuncts; each conjunct maps field -> (lo, hi) inclusive.
    dnf: tuple[tuple[tuple[str, float, float], ...], ...]
    raw: dict[str, Any] = field(compare=False)


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    target: str
    rule_ids: tuple[str, ...]
    # Optional deterministic workload used only by local samples/tests to
    # exercise real time/memory consumption. The scheduler always measures
    # actual elapsed time and actual allocation; it never trusts these hints.
    simulate: dict[str, Any] | None = None


@dataclass
class TaskOutcome:
    task_id: str
    status: str  # succeeded|timeout|memory_exceeded|failed|rejected_queue|not_run
    code: str | None
    elapsed_ms: float
    mem_bytes: int
    findings: dict[str, Any] | None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "code": self.code,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "mem_bytes": self.mem_bytes,
            "findings": self.findings,
            "error": self.error,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "TaskOutcome":
        return TaskOutcome(
            task_id=data["task_id"],
            status=data["status"],
            code=data.get("code"),
            elapsed_ms=float(data.get("elapsed_ms", 0.0)),
            mem_bytes=int(data.get("mem_bytes", 0)),
            findings=data.get("findings"),
            error=data.get("error"),
        )
