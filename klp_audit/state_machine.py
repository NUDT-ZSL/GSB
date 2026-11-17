"""Order-independent KLP lifecycle state machine."""

from __future__ import annotations

import time
from dataclasses import dataclass

from .constants import STAGES
from .errors import E_INCOMPLETE, E_TRANSITION
from .models import Event, Issue


@dataclass(frozen=True)
class StateResult:
    issues: tuple[Issue, ...]
    stage_seqs: dict[str, tuple[int, ...]]
    passed: bool


def evaluate_state_machine(
    session_id: str,
    events: list[Event],
    *,
    deadline: float | None = None,
) -> StateResult:
    """Evaluate a pre-sorted, deduplicated session.

    Stage ranks are non-decreasing. ``derive`` and ``rotate`` may repeat
    consecutively because one session can derive multiple hierarchy levels and
    may rotate more than once. All other repeats, backward moves, and jumps
    produce E_TRANSITION. Missing stages produce E_INCOMPLETE.
    """

    current_index = -1
    issues: list[Issue] = []
    stage_seqs: dict[str, list[int]] = {stage: [] for stage in STAGES}

    for index, event in enumerate(events):
        if deadline is not None and (index & 63) == 0 and time.perf_counter() > deadline:
            raise TimeoutError(f"session {session_id!r} exceeded its time budget")
        stage_seqs[event.stage].append(event.seq)
        observed_index = STAGES.index(event.stage)
        if current_index == -1:
            if observed_index == 0:
                current_index = 0
                continue
        elif observed_index == current_index and event.stage in {"derive", "rotate"}:
            continue
        elif observed_index == current_index + 1:
            current_index = observed_index
            continue

        expected_index = current_index + 1
        expected_stage = STAGES[expected_index] if expected_index < len(STAGES) else None
        if expected_stage is None:
            message = (
                f"session {session_id!r} has event {event.stage!r} at seq {event.seq} "
                "after destroy"
            )
        else:
            message = (
                f"session {session_id!r} expected {expected_stage!r} but observed "
                f"{event.stage!r} at seq {event.seq}"
            )
        issues.append(
            Issue(
                code=E_TRANSITION,
                message=message,
                stage=event.stage,
                seq=event.seq,
                offset=event.offset,
                details={
                    "expected_stage": expected_stage,
                    "observed_stage": event.stage,
                },
            )
        )

    expected_index = current_index + 1
    if expected_index < len(STAGES):
        missing = list(STAGES[expected_index:])
        issues.append(
            Issue(
                code=E_INCOMPLETE,
                message=f"session {session_id!r} is missing required stages: {', '.join(missing)}",
                details={"missing_stages": missing},
            )
        )

    return StateResult(
        issues=tuple(issues),
        stage_seqs={stage: tuple(values) for stage, values in stage_seqs.items()},
        passed=not issues,
    )

