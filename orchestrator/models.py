"""Core data structures for pipeline stages, DAGs and trace events."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


STAGE_STATES = (
    "pending",
    "ready",
    "running",
    "succeeded",
    "failed",
    "skipped",
    "unknown",
)

TERMINAL_STATES = ("succeeded", "failed", "skipped")


@dataclass(frozen=True)
class Dependency:
    """A reference to an upstream stage.

    kind = "strong" blocks execution and propagates failure as skip.
    kind = "weak" only blocks until the upstream reaches a terminal state;
    a failed weak upstream is reported as missing instead of skipping.
    """

    stage_id: str
    kind: str = "strong"

    def to_dict(self) -> dict:
        return {"stage_id": self.stage_id, "kind": self.kind}


@dataclass
class Stage:
    stage_id: str
    script_path: str
    dependencies: List[Dependency]
    resource_units: int
    estimated_ms: int
    retries: int
    output_path: str
    timeout_ms: Optional[int] = None
    max_concurrency: Optional[int] = None

    @property
    def strong_deps(self) -> List[str]:
        return sorted(
            d.stage_id for d in self.dependencies if d.kind == "strong"
        )

    @property
    def weak_deps(self) -> List[str]:
        return sorted(
            d.stage_id for d in self.dependencies if d.kind == "weak"
        )


@dataclass
class Attempt:
    """One subprocess attempt of a stage."""

    attempt_no: int
    stage_id: str
    start_mono_ms: int
    end_mono_ms: int
    duration_ms: int
    exit_code: Optional[int]
    timed_out: bool
    stderr_norm: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StageResult:
    stage_id: str
    state: str
    output_path: str
    output_sha256: Optional[str] = None
    output_bytes: int = 0
    output_records: int = 0
    missing_upstreams: List[str] = field(default_factory=list)
    skipped_by: Optional[str] = None
    attempts: List[Attempt] = field(default_factory=list)
    start_mono_ms: Optional[int] = None
    end_mono_ms: Optional[int] = None
    started_iso: Optional[str] = None
    ended_iso: Optional[str] = None

    def to_dict(self) -> dict:
        out = asdict(self)
        out["attempts"] = [a.to_dict() for a in self.attempts]
        out["missing_upstreams"] = sorted(self.missing_upstreams)
        return out


@dataclass
class TraceEvent:
    """A single, append-only scheduling/state-machine event."""

    seq: int
    iso_time: str
    mono_ms: int
    stage_id: str
    event: str
    reason: str
    resource_units: int
    start_mono_ms: Optional[int] = None
    end_mono_ms: Optional[int] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = asdict(self)
        return out


@dataclass
class PipelineDag:
    dag_id: str
    dag_version: str
    stages: Dict[str, Stage]
    global_resource_budget: int
    default_max_concurrency: int
    default_timeout_ms: Optional[int]
    source_path: Optional[str] = None

    def stage_ids_sorted(self) -> List[str]:
        return sorted(self.stages)

    def topological_order(self) -> List[str]:
        """Deterministic Kahn ordering; raises on a cycle."""
        indeg: Dict[str, int] = {sid: 0 for sid in self.stages}
        children: Dict[str, List[str]] = {sid: [] for sid in self.stages}
        for sid, stage in self.stages.items():
            for dep in stage.dependencies:
                indeg[sid] += 1
                children[dep.stage_id].append(sid)
        ready = sorted(sid for sid, deg in indeg.items() if deg == 0)
        order: List[str] = []
        while ready:
            current = ready.pop(0)
            order.append(current)
            for child in sorted(children[current]):
                indeg[child] -= 1
                if indeg[child] == 0:
                    ready.append(child)
            ready.sort()
        if len(order) != len(self.stages):
            raise ValueError("cycle")
        return order

    def to_dict(self) -> dict:
        return {
            "dag_id": self.dag_id,
            "dag_version": self.dag_version,
            "global_resource_budget": self.global_resource_budget,
            "default_max_concurrency": self.default_max_concurrency,
            "default_timeout_ms": self.default_timeout_ms,
            "stages": [
                {
                    "stage_id": s.stage_id,
                    "script_path": s.script_path,
                    "dependencies": [d.to_dict() for d in s.dependencies],
                    "resource_units": s.resource_units,
                    "estimated_ms": s.estimated_ms,
                    "retries": s.retries,
                    "output_path": s.output_path,
                    "timeout_ms": s.timeout_ms,
                    "max_concurrency": s.max_concurrency,
                }
                for sid, s in sorted(self.stages.items())
            ],
        }
