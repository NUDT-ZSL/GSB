from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class RuleSet:
    event_id: str
    version: str
    ticket_whitelist: tuple[str, ...]
    window_start: str
    window_end: str
    reentry_policy: str
    child_requires_adult: bool
    sources: dict[str, str]
    start_at: datetime = field(compare=False)
    end_at: datetime = field(compare=False)

    @property
    def child_policy(self) -> dict[str, Any]:
        return {"requires_adult": self.child_requires_adult}

    def normalized(self) -> dict[str, Any]:
        return {
            "ticket_whitelist": list(self.ticket_whitelist),
            "entry_window": {
                "start": self.window_start,
                "end": self.window_end,
            },
            "reentry_policy": self.reentry_policy,
            "child_policy": self.child_policy,
        }

    def comparable_fields(self) -> dict[str, Any]:
        return {
            "ticket_whitelist": list(self.ticket_whitelist),
            "entry_window.start": self.window_start,
            "entry_window.end": self.window_end,
            "reentry_policy": self.reentry_policy,
            "child_policy.requires_adult": self.child_requires_adult,
        }

    def max_entries(self) -> int | None:
        if self.reentry_policy == "single":
            return 1
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "version": self.version,
            **self.normalized(),
            "sources": dict(self.sources),
        }


@dataclass(frozen=True)
class EventDefinition:
    event_id: str
    name: str
    current_version: str
    versions: dict[str, RuleSet]


@dataclass(frozen=True)
class Gate:
    gate_id: str
    name: str
    entrance_id: str
    seat_zone_ids: tuple[str, ...]
    subscriptions: tuple[str, ...]


@dataclass(frozen=True)
class Change:
    event_id: str
    from_version: str
    to_version: str


@dataclass(frozen=True)
class Topology:
    entrance_dependencies: dict[str, tuple[str, ...]]
    seat_zone_dependencies: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class Fault:
    attempt: int
    error_code: str
    retryable: bool


@dataclass(frozen=True)
class VerificationRequest:
    request_id: str
    gate_id: str
    event_id: str
    at: str
    at_time: datetime
    ticket_type: str
    prior_entries: int
    is_child: bool
    accompanied_adult_count: int
    after_batch: int | None
    source_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "gate_id": self.gate_id,
            "event_id": self.event_id,
            "at": self.at,
            "ticket_type": self.ticket_type,
            "prior_entries": self.prior_entries,
            "is_child": self.is_child,
            "accompanied_adult_count": self.accompanied_adult_count,
            "after_batch": self.after_batch,
            "source_path": self.source_path,
        }


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    path: str
    message: str
    event_id: str | None = None
    version: str | None = None
    gate_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }
        if self.event_id is not None:
            result["event_id"] = self.event_id
        if self.version is not None:
            result["version"] = self.version
        if self.gate_id is not None:
            result["gate_id"] = self.gate_id
        return result


@dataclass
class Project:
    root: str
    project_id: str
    seed: int
    schema_version: int
    manifest: dict[str, Any]
    events: dict[str, EventDefinition]
    gates: dict[str, Gate]
    changes: dict[str, Change]
    topology: Topology
    faults: dict[str, tuple[Fault, ...]]
    requests: tuple[VerificationRequest, ...]
    source_files: dict[str, str]
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.issues

    def target_version(self, event_id: str) -> str:
        change = self.changes.get(event_id)
        if change is not None:
            return change.to_version
        return self.events[event_id].current_version

    def baseline_version(self, event_id: str) -> str:
        change = self.changes.get(event_id)
        if change is not None:
            return change.from_version
        return self.events[event_id].current_version

    def target_versions_for_gate(self, gate: Gate) -> dict[str, str]:
        return {
            event_id: self.target_version(event_id)
            for event_id in gate.subscriptions
        }

    def baseline_versions_for_gate(self, gate: Gate) -> dict[str, str]:
        return {
            event_id: self.baseline_version(event_id)
            for event_id in gate.subscriptions
        }

    def rule(self, event_id: str, version: str) -> RuleSet:
        return self.events[event_id].versions[version]

    def to_summary(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "seed": self.seed,
            "schema_version": self.schema_version,
            "event_count": len(self.events),
            "gate_count": len(self.gates),
            "changed_events": [
                {
                    "event_id": change.event_id,
                    "from_version": change.from_version,
                    "to_version": change.to_version,
                }
                for change in sorted(self.changes.values(), key=lambda c: c.event_id)
            ],
            "verification_request_count": len(self.requests),
            "source_files": dict(sorted(self.source_files.items())),
            "issues": [issue.to_dict() for issue in self.issues],
        }
