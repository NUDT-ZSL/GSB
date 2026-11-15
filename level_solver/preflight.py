"""Bounded, order-preserving concurrent constraint preflight."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Sequence

from .constraints import (
    ConstraintViolation,
    CandidateRejected,
    check_constraint,
    check_state_integrity,
    estimate_state_bytes,
)
from .errors import SolverError
from .grid import LevelState
from .model import Constraint, LevelSpec


@dataclass
class PreflightReport:
    ok: bool
    errors: list[ConstraintViolation] = field(default_factory=list)
    workers: int = 0
    checks_submitted: int = 0
    checks_completed: int = 0
    cancelled: int = 0
    max_concurrent_states: int = 1
    state_limit_by_memory: int = 0
    state_bytes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "workers": self.workers,
            "checks_submitted": self.checks_submitted,
            "checks_completed": self.checks_completed,
            "cancelled": self.cancelled,
            "max_concurrent_states": self.max_concurrent_states,
            "state_limit_by_memory": self.state_limit_by_memory,
            "state_bytes": self.state_bytes,
            "errors": [error.as_dict() for error in self.errors],
        }


def memory_admission(spec: LevelSpec) -> tuple[int, int]:
    state_bytes = estimate_state_bytes(spec)
    budget_bytes = spec.config.memory_budget_kb * 1024
    max_states = budget_bytes // state_bytes
    if max_states < 2:
        raise SolverError(
            "E_RESOURCE",
            "memory budget cannot hold the base and candidate grid states",
            {
                "memory_budget_kb": spec.config.memory_budget_kb,
                "estimated_state_bytes": state_bytes,
                "states_needed": 2,
            },
        )
    return state_bytes, max_states


def preflight(
    spec: LevelSpec,
    state: LevelState,
    constraints: Sequence[Constraint],
) -> PreflightReport:
    """Check a candidate state against constraints.

    Checks are submitted in declaration order.  Results are consumed in that
    order so the first reported error is exactly the serial-preflight error,
    independent of thread completion order.  On failure, all still-pending
    checks are cancelled.
    """

    state_bytes, max_states = memory_admission(spec)
    try:
        check_state_integrity(spec, state)
    except CandidateRejected as exc:
        return PreflightReport(
            ok=False,
            errors=[
                ConstraintViolation(
                    constraint_id="<state>", message=exc.message
                )
            ],
            max_concurrent_states=1,
            state_limit_by_memory=max_states,
            state_bytes=state_bytes,
        )
    if not constraints:
        return PreflightReport(
            ok=True,
            max_concurrent_states=1,
            state_limit_by_memory=max_states,
            state_bytes=state_bytes,
        )

    workers = min(spec.config.concurrency, len(constraints), max_states)
    report = PreflightReport(
        ok=True,
        workers=workers,
        checks_submitted=len(constraints),
        max_concurrent_states=1,
        state_limit_by_memory=max_states,
        state_bytes=state_bytes,
    )
    futures: list[Future[ConstraintViolation | None]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="preflight") as pool:
        for constraint in constraints:
            futures.append(
                pool.submit(_check_one, spec, constraint, state)
            )
        for index, future in enumerate(futures):
            violation = future.result()
            report.checks_completed += 1
            if violation is None:
                continue
            report.ok = False
            report.errors.append(violation)
            # Preserve serial first-error semantics while still cancelling
            # work that has not started.
            for pending in futures[index + 1 :]:
                pending.cancel()
            # Report the deterministic number of remaining checks for which
            # cancellation was requested.  Thread timing must not leak into
            # the result's selection telemetry.
            report.cancelled = len(futures) - index - 1
            break
    return report


def _check_one(
    spec: LevelSpec, constraint: Constraint, state: LevelState
) -> ConstraintViolation | None:
    try:
        check_constraint(spec, constraint, state)
    except ConstraintViolation as exc:
        return exc
    return None
