from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SCHEMA_VERSION = 1

EVENT_CODES = frozenset(
    {
        "CONNECT_OK",
        "HTTP_2XX",
        "HTTP_3XX",
        "HTTP_4XX",
        "HTTP_5XX",
        "BODY_VALID",
    }
)

INJECTION_TYPES = frozenset(
    {
        "network_outage",
        "service_reject",
        "data_corruption",
        "clock_rollback",
    }
)

ROUTE_MODES = frozenset({"normal", "malformed", "disconnect"})

STAGE_PENDING = "PENDING"
STAGE_RUNNING = "RUNNING"
STAGE_OBSERVED = "OBSERVED"
STAGE_TIMED_OUT = "TIMED_OUT"
STAGE_SKIPPED = "SKIPPED"
STAGE_ABORTED = "ABORTED"

FINAL_STAGES = frozenset(
    {STAGE_OBSERVED, STAGE_TIMED_OUT, STAGE_SKIPPED, STAGE_ABORTED}
)
DEPENDENCY_SATISFIED_STAGES = frozenset({STAGE_OBSERVED, STAGE_SKIPPED})

OUTCOME_NOT_STARTED = "NOT_STARTED"
OUTCOME_TIMEOUT_AFTER_START = "TIMED_OUT_AFTER_START"
OUTCOME_SKIPPED_MANUALLY = "SKIPPED_MANUALLY"
OUTCOME_OBSERVED = "OBSERVED"
OUTCOME_ABORTED = "ABORTED"

ERROR_CONN_REFUSED = "E_CONN_REFUSED"
ERROR_CONNECTION_DROPPED = "E_CONNECTION_DROPPED"
ERROR_STEP_TIMEOUT = "E_STEP_TIMEOUT"
ERROR_BAD_STATUS = "E_BAD_STATUS"
ERROR_MALFORMED_BODY = "E_MALFORMED_BODY"
ERROR_DATA_CORRUPTION = "E_DATA_CORRUPTION"
ERROR_SERVICE_REJECTED = "E_SERVICE_REJECTED"
ERROR_CLOCK_ROLLBACK = "E_CLOCK_ROLLBACK"
ERROR_EXPECTATION_MISMATCH = "E_EXPECTATION_MISMATCH"
ERROR_TRANSPORT = "E_TRANSPORT"
ERROR_INTERNAL = "E_INTERNAL"


def status_event(status_code: int) -> str:
    if 200 <= status_code <= 299:
        return "HTTP_2XX"
    if 300 <= status_code <= 399:
        return "HTTP_3XX"
    if 400 <= status_code <= 499:
        return "HTTP_4XX"
    if 500 <= status_code <= 599:
        return "HTTP_5XX"
    return "HTTP_UNKNOWN"


@dataclass(frozen=True)
class PlanStep:
    seq: int
    name: str
    depends_on: tuple[int, ...]
    expected_event: str
    timeout_logical_ms: int
    skippable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "name": self.name,
            "depends_on": list(self.depends_on),
            "expected_event": self.expected_event,
            "timeout_logical_ms": self.timeout_logical_ms,
            "skippable": self.skippable,
        }


@dataclass(frozen=True)
class Plan:
    name: str
    steps: tuple[PlanStep, ...]

    @property
    def step_map(self) -> dict[int, PlanStep]:
        return {step.seq: step for step in self.steps}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "steps": [step.to_dict() for step in self.steps],
        }


@dataclass(frozen=True)
class Route:
    step: int
    path: str
    status: int
    delay_logical_ms: int
    body: str
    content_type: str
    mode: str
    body_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "path": self.path,
            "status": self.status,
            "delay_logical_ms": self.delay_logical_ms,
            "body": self.body,
            "content_type": self.content_type,
            "mode": self.mode,
            "body_sha256": self.body_sha256,
        }


@dataclass(frozen=True)
class StepWindow:
    step: int
    start_ms: int
    end_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
        }


@dataclass(frozen=True)
class ClockSpec:
    start_ms: int
    tick_logical_ms: int
    tick_real_ms: int
    windows: tuple[StepWindow, ...]

    @property
    def window_map(self) -> dict[int, StepWindow]:
        return {window.step: window for window in self.windows}

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_ms": self.start_ms,
            "tick": {
                "logical_ms": self.tick_logical_ms,
                "real_ms": self.tick_real_ms,
            },
            "step_windows": [window.to_dict() for window in self.windows],
        }


@dataclass(frozen=True)
class Injection:
    injection_id: str
    step: int
    at_ms: int
    injection_type: str
    rollback_ms: int = 0
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.injection_id,
            "step": self.step,
            "at_ms": self.at_ms,
            "type": self.injection_type,
            "rollback_ms": self.rollback_ms,
            "description": self.description,
        }


@dataclass(frozen=True)
class Snapshot:
    plan: Plan
    routes: tuple[Route, ...]
    clock: ClockSpec
    injections: tuple[Injection, ...]
    source_digests: dict[str, str]
    content_digest: str
    plan_digest: str
    steps_digest: str
    services_digest: str
    clock_digest: str
    injections_digest: str

    @property
    def route_map(self) -> dict[int, Route]:
        return {route.step: route for route in self.routes}

    @property
    def injection_map(self) -> dict[str, Injection]:
        return {
            injection.injection_id: injection for injection in self.injections
        }

    @property
    def injection_map_by_step(self) -> dict[int, tuple[Injection, ...]]:
        grouped: dict[int, list[Injection]] = {}
        for injection in self.injections:
            grouped.setdefault(injection.step, []).append(injection)
        return {
            step: tuple(sorted(items, key=lambda item: (item.at_ms, item.injection_id)))
            for step, items in grouped.items()
        }

    def normalized(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "plan": self.plan.to_dict(),
            "routes": [
                route.to_dict()
                for route in sorted(self.routes, key=lambda route: route.step)
            ],
            "clock": self.clock.to_dict(),
            "injections": [
                injection.to_dict()
                for injection in sorted(
                    self.injections,
                    key=lambda injection: (
                        injection.at_ms,
                        injection.step,
                        injection.injection_id,
                    ),
                )
            ],
            "source_digests": dict(sorted(self.source_digests.items())),
        }

    def to_json_dict(self) -> dict[str, Any]:
        result = self.normalized()
        result["content_digest"] = self.content_digest
        return result


@dataclass
class StepObservation:
    step: int
    attempt_no: int
    code: str
    outcome_class: str
    stage: str
    event: str | None
    response_status: int | None
    body_sha256: str | None
    injected_at_ms: int | None
    injections: tuple[str, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)
    started_at_ms: int | None = None
    ended_at_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "attempt_no": self.attempt_no,
            "code": self.code,
            "outcome_class": self.outcome_class,
            "stage": self.stage,
            "event": self.event,
            "response_status": self.response_status,
            "body_sha256": self.body_sha256,
            "injected_at_ms": self.injected_at_ms,
            "injections": list(self.injections),
            "detail": self.detail,
            "started_at_ms": self.started_at_ms,
            "ended_at_ms": self.ended_at_ms,
        }

