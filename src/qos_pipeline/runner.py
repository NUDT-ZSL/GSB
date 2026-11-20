"""Step execution loop with crash injection and stale-writer recovery."""

from __future__ import annotations

import os
from typing import Any, Callable

from .engine import Engine
from .errors import QosError
from .models import Sample
from .store import Store


def _default_crash_action(_: str) -> None:
    os._exit(137)


def run_steps(
    sample: Sample,
    store: Store,
    *,
    end_step: int | None = None,
    crash_at_step: int | None = None,
    crash_point: str | None = None,
    crash_action: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    limit = sample.total_steps if end_step is None else end_step
    if not 0 <= limit <= sample.total_steps:
        raise QosError(
            "E_USAGE",
            "end_step is outside the sample horizon",
            {"end_step": limit, "total_steps": sample.total_steps},
        )
    if (crash_at_step is None) != (crash_point is None):
        raise QosError(
            "E_USAGE",
            "crash_at_step and crash_point must be provided together",
        )
    if crash_point not in (None, "after_refill", "after_schedule", "before_commit"):
        raise QosError("E_USAGE", "unsupported crash point", {"crash_point": crash_point})

    action = crash_action or _default_crash_action

    def crash_hook(point: str, step_index: int) -> None:
        if crash_at_step == step_index and crash_point == point:
            action(point)

    engine = Engine(sample, crash_hook=crash_hook)
    committed_now: list[int] = []
    while True:
        snapshot = store.snapshot()
        next_step = snapshot["last_step"] + 1
        if next_step >= limit:
            break
        state = snapshot["state"]
        payload, new_state = engine.simulate_step(state, next_step)
        if crash_at_step == next_step and crash_point == "before_commit":
            action("before_commit")
        try:
            result = store.commit_step(
                step_index=next_step,
                logical_time_ms=payload["step_end_ms"],
                payload=payload,
                state=new_state,
                prev_digest=snapshot["step_digest"],
                version=snapshot["version"],
            )
        except QosError as exc:
            if exc.code == "E_STALE_COMMIT":
                continue
            raise
        if not result["already_committed"]:
            committed_now.append(next_step)
    final = store.status()
    return {
        "ok": True,
        "steps_committed_this_call": committed_now,
        "steps_committed_count": len(committed_now),
        "status": final,
    }

