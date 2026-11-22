from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable

from .derivation import ShardArtifact
from .errors import BusinessError


@dataclass(frozen=True)
class ShardOutcome:
    shard_id: str
    outcome: str
    reason: str | None
    started: bool
    logical_clock_ms: int
    logical_cost_ms: int
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def is_success(self) -> bool:
        return self.outcome in {"written", "hit"}


@dataclass(frozen=True)
class ScheduleResult:
    outcomes: tuple[ShardOutcome, ...]
    logical_clock_ms: int
    rounds: int
    started_count: int
    materialized_count: int
    hit_count: int

    @property
    def complete(self) -> bool:
        return all(outcome.is_success for outcome in self.outcomes)

    @property
    def abandoned(self) -> tuple[ShardOutcome, ...]:
        return tuple(
            outcome
            for outcome in self.outcomes
            if outcome.outcome in {"not_started", "abandoned"}
        )

    @property
    def failures(self) -> tuple[ShardOutcome, ...]:
        return tuple(
            outcome for outcome in self.outcomes if outcome.outcome == "failed"
        )


Materialize = Callable[[ShardArtifact], bool]
OutcomeCallback = Callable[[ShardOutcome], None]


def schedule_shards(
    pending: list[ShardArtifact],
    *,
    start_clock_ms: int,
    max_concurrency: int,
    logical_budget_ms: int,
    materialize: Materialize,
    on_outcome: OutcomeCallback,
    prior_hits: int = 0,
    fail_shards: set[str] | frozenset[str] = frozenset(),
    cancel_after_started: int | None = None,
) -> ScheduleResult:
    """Run one budgeted logical save round.

    Budget admission happens before a worker starts. Once started, a shard is
    waited on to completion and is never truncated by the logical deadline.
    """

    remaining = sorted(pending, key=lambda shard: shard.shard_id)
    outcomes: list[ShardOutcome] = []
    clock = start_clock_ms
    deadline = start_clock_ms + logical_budget_ms
    rounds = 0
    started_count = 0
    materialized_count = 0

    while remaining:
        rounds += 1
        wave: list[tuple[ShardArtifact, Future[bool]]] = []
        with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
            while remaining and len(wave) < max_concurrency:
                if (
                    cancel_after_started is not None
                    and started_count >= cancel_after_started
                ):
                    for shard in remaining:
                        outcome = ShardOutcome(
                            shard_id=shard.shard_id,
                            outcome="abandoned",
                            reason="operator_cancelled",
                            started=False,
                            logical_clock_ms=clock,
                            logical_cost_ms=shard.logical_cost_ms,
                            detail={
                                "started": False,
                                "disposition": "active_abandon",
                                "cancel_after_started": cancel_after_started,
                            },
                        )
                        outcomes.append(outcome)
                        on_outcome(outcome)
                    remaining.clear()
                    break

                shard = remaining[0]
                if clock + shard.logical_cost_ms > deadline:
                    for candidate in remaining:
                        outcome = ShardOutcome(
                            shard_id=candidate.shard_id,
                            outcome="not_started",
                            reason="logical_budget_exhausted",
                            started=False,
                            logical_clock_ms=clock,
                            logical_cost_ms=candidate.logical_cost_ms,
                            detail={
                                "started": False,
                                "disposition": "budget_not_started",
                                "deadline_ms": deadline,
                                "remaining_budget_ms": max(0, deadline - clock),
                            },
                        )
                        outcomes.append(outcome)
                        on_outcome(outcome)
                    remaining.clear()
                    break

                remaining.pop(0)
                started_count += 1
                wave.append((shard, pool.submit(materialize, shard)))

            if not wave:
                break
            wait(wave_future for _, wave_future in wave)

            wave_cost = max(shard.logical_cost_ms for shard, _ in wave)
            wave_end_clock = clock + wave_cost
            for shard, future in wave:
                try:
                    written = future.result()
                    reason = None if written else "object_reused_during_materialize"
                    outcome = ShardOutcome(
                        shard_id=shard.shard_id,
                        outcome="written",
                        reason=reason,
                        started=True,
                        logical_clock_ms=wave_end_clock,
                        logical_cost_ms=shard.logical_cost_ms,
                        detail={
                            "started": True,
                            "bytes": shard.size,
                            "digest": shard.digest,
                            "materialized": written,
                        },
                    )
                    materialized_count += 1
                except Exception as exc:  # noqa: BLE001 - worker failures are business data
                    if isinstance(exc, BusinessError):
                        code = exc.code
                        message = exc.message
                        detail = exc.details
                    else:
                        code = "IO_ERROR"
                        message = str(exc)
                        detail = {"exception_type": type(exc).__name__}
                    outcome = ShardOutcome(
                        shard_id=shard.shard_id,
                        outcome="failed",
                        reason=code,
                        started=True,
                        logical_clock_ms=wave_end_clock,
                        logical_cost_ms=shard.logical_cost_ms,
                        detail={
                            "started": True,
                            "disposition": "failed_after_start",
                            "message": message,
                            **detail,
                        },
                    )
                outcomes.append(outcome)
                on_outcome(outcome)

            clock += wave_cost
            if any(outcome.outcome == "failed" for outcome in outcomes):
                if remaining:
                    for shard in remaining:
                        outcome = ShardOutcome(
                            shard_id=shard.shard_id,
                            outcome="not_started",
                            reason="not_started_after_failure",
                            started=False,
                            logical_clock_ms=clock,
                            logical_cost_ms=shard.logical_cost_ms,
                            detail={
                                "started": False,
                                "disposition": "failure_isolated_not_started",
                                "failure_isolation": True,
                            },
                        )
                        outcomes.append(outcome)
                        on_outcome(outcome)
                    remaining.clear()

    outcomes.sort(key=lambda item: item.shard_id)
    return ScheduleResult(
        outcomes=tuple(outcomes),
        logical_clock_ms=clock,
        rounds=rounds,
        started_count=started_count,
        materialized_count=materialized_count,
        hit_count=prior_hits,
    )
