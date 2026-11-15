from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Iterable

from .canonical import canonical_json, sha256_value
from .config import RulesConfig
from .engine import MatchReplayResult, replay_match
from .errors import ErrorCode, Violation
from .state import GameState, ResourceBudgetTracker


MIB = 1024 * 1024


@dataclass(frozen=True)
class SchedulerBudget:
    thread_budget: int
    memory_budget_bytes: int
    per_match_memory_bytes: int

    @classmethod
    def from_mib(
        cls,
        *,
        threads: int,
        memory_budget_mib: int,
        per_match_memory_mib: int,
    ) -> "SchedulerBudget":
        return cls(
            thread_budget=threads,
            memory_budget_bytes=memory_budget_mib * MIB,
            per_match_memory_bytes=per_match_memory_mib * MIB,
        )


@dataclass(frozen=True)
class ScheduledMatch:
    match_id: str
    events: list[dict[str, Any]]
    input_index: int


@dataclass
class SchedulingResult:
    matches: list[MatchReplayResult]
    scheduler: dict[str, Any]

    @property
    def results_by_match_id(self) -> dict[str, MatchReplayResult]:
        return {result.match_id: result for result in self.matches}


def estimate_match_memory_bytes(
    events: list[dict[str, Any]], config: RulesConfig
) -> int:
    encoded = canonical_json(events).encode("utf-8")
    per_event = (
        4096
        + len(config.players) * 192
        + config.rng_stream_size * 16
        + 2048
    )
    return 64 * 1024 + len(encoded) * 3 + len(events) * per_event


def _denied_result(
    scheduled: ScheduledMatch,
    config: RulesConfig,
    *,
    reason: str,
    estimate_bytes: int,
) -> MatchReplayResult:
    state = GameState.initial(config)
    tracker = ResourceBudgetTracker.initial(state, config.resource_cap)
    violation = Violation(
        ErrorCode.BUDGET_DENIED,
        "match validation was denied by the memory budget",
        context={
            "reason": reason,
            "estimated_memory_bytes": estimate_bytes,
        },
    )
    final_state = state.to_dict()
    return MatchReplayResult(
        match_id=scheduled.match_id,
        seed=_event_seed(scheduled.events),
        status="violation",
        errors=[violation],
        traces=[],
        final_state=final_state,
        final_state_hash=sha256_value(final_state),
        budget=tracker.to_dict(),
        events_received=len(scheduled.events),
        events_processed=0,
        rejected_events=0,
        trigger_rollbacks=[],
        memory_estimate_bytes=estimate_bytes,
    )


def _event_seed(events: list[dict[str, Any]]) -> int:
    if events and isinstance(events[0], dict):
        seed = events[0].get("seed", 0)
        if isinstance(seed, int) and not isinstance(seed, bool):
            return int(seed)
    return 0


def _all_denied_budget_results(
    matches: list[ScheduledMatch],
    config: RulesConfig,
    budget: SchedulerBudget,
    *,
    reason: str,
) -> SchedulingResult:
    results = [
        _denied_result(
            scheduled,
            config,
            reason=reason,
            estimate_bytes=estimate_match_memory_bytes(scheduled.events, config),
        )
        for scheduled in matches
    ]
    scheduler = {
        "thread_budget": budget.thread_budget,
        "memory_budget_bytes": budget.memory_budget_bytes,
        "per_match_memory_bytes": budget.per_match_memory_bytes,
        "effective_workers": 0,
        "queued_matches": len(matches),
        "degraded": True,
        "degradations": [
            {
                "reason": reason,
                "requested_workers": budget.thread_budget,
                "effective_workers": 0,
            }
        ],
        "completion": {
            "scheduled": len(matches),
            "completed": len(results),
            "denied": len(results),
            "missing": 0,
        },
    }
    return SchedulingResult(
        matches=sorted(results, key=lambda result: result.match_id),
        scheduler=scheduler,
    )


def run_scheduled(
    matches: Iterable[tuple[str, list[dict[str, Any]]]] | Iterable[ScheduledMatch],
    config: RulesConfig,
    budget: SchedulerBudget,
) -> SchedulingResult:
    scheduled_matches: list[ScheduledMatch] = []
    for input_index, item in enumerate(matches):
        if isinstance(item, ScheduledMatch):
            scheduled_matches.append(item)
        else:
            match_id, events = item
            scheduled_matches.append(
                ScheduledMatch(match_id=match_id, events=events, input_index=input_index)
            )

    if budget.thread_budget <= 0:
        return _all_denied_budget_results(
            scheduled_matches,
            config,
            budget,
            reason="non_positive_thread_budget",
        )
    if budget.memory_budget_bytes <= 0:
        return _all_denied_budget_results(
            scheduled_matches,
            config,
            budget,
            reason="non_positive_memory_budget",
        )
    if budget.per_match_memory_bytes <= 0:
        return _all_denied_budget_results(
            scheduled_matches,
            config,
            budget,
            reason="non_positive_per_match_memory_budget",
        )

    if not scheduled_matches:
        return SchedulingResult(
            matches=[],
            scheduler={
                "thread_budget": budget.thread_budget,
                "memory_budget_bytes": budget.memory_budget_bytes,
                "per_match_memory_bytes": budget.per_match_memory_bytes,
                "effective_workers": 0,
                "queued_matches": 0,
                "degraded": False,
                "degradations": [],
                "completion": {
                    "scheduled": 0,
                    "completed": 0,
                    "denied": 0,
                    "missing": 0,
                },
            },
        )

    estimates = {
        scheduled.match_id: estimate_match_memory_bytes(scheduled.events, config)
        for scheduled in scheduled_matches
    }
    runnable: list[ScheduledMatch] = []
    results_by_index: dict[int, MatchReplayResult] = {}
    denied = 0
    degradations: list[dict[str, Any]] = []

    for scheduled in scheduled_matches:
        estimate = estimates[scheduled.match_id]
        if estimate > budget.per_match_memory_bytes:
            results_by_index[scheduled.input_index] = _denied_result(
                scheduled,
                config,
                reason="estimated_match_memory_exceeds_per_match_budget",
                estimate_bytes=estimate,
            )
            denied += 1
        elif estimate > budget.memory_budget_bytes:
            results_by_index[scheduled.input_index] = _denied_result(
                scheduled,
                config,
                reason="estimated_match_memory_exceeds_total_memory_budget",
                estimate_bytes=estimate,
            )
            denied += 1
        else:
            runnable.append(scheduled)

    memory_parallel = budget.memory_budget_bytes // budget.per_match_memory_bytes
    forced_serial = False
    if runnable and memory_parallel < 1:
        memory_parallel = 1
        forced_serial = True
    desired_workers = min(budget.thread_budget, max(1, memory_parallel), len(runnable))
    if not runnable:
        desired_workers = 0

    if forced_serial:
        degradations.append(
            {
                "reason": "memory_budget_below_single_match_reservation",
                "requested_workers": budget.thread_budget,
                "effective_workers": 1,
                "memory_budget_bytes": budget.memory_budget_bytes,
                "per_match_memory_bytes": budget.per_match_memory_bytes,
            }
        )
    if (
        not forced_serial
        and runnable
        and desired_workers < min(budget.thread_budget, len(runnable))
    ):
        reason = (
            "memory_budget"
            if max(1, memory_parallel) < budget.thread_budget
            else "thread_budget"
        )
        degradations.append(
            {
                "reason": reason,
                "requested_workers": budget.thread_budget,
                "effective_workers": desired_workers,
                "queued_matches": max(0, len(runnable) - desired_workers),
            }
        )

    if desired_workers <= 1:
        for scheduled in runnable:
            result = replay_match(scheduled.match_id, scheduled.events, config)
            result.memory_estimate_bytes = estimates[scheduled.match_id]
            results_by_index[scheduled.input_index] = result
    else:
        with ThreadPoolExecutor(
            max_workers=desired_workers,
            thread_name_prefix="card-replay",
        ) as executor:
            future_to_scheduled: dict[Future[MatchReplayResult], ScheduledMatch] = {}
            for scheduled in runnable:
                future = executor.submit(
                    replay_match,
                    scheduled.match_id,
                    scheduled.events,
                    config,
                )
                future_to_scheduled[future] = scheduled
            for future in as_completed(future_to_scheduled):
                scheduled = future_to_scheduled[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = _denied_result(
                        scheduled,
                        config,
                        reason=f"worker_exception:{type(exc).__name__}",
                        estimate_bytes=estimates[scheduled.match_id],
                    )
                    result.errors = [
                        Violation(
                            ErrorCode.MALFORMED_EVENT,
                            "worker failed while replaying the match",
                            context={"exception_type": type(exc).__name__},
                        )
                    ]
                result.memory_estimate_bytes = estimates[scheduled.match_id]
                results_by_index[scheduled.input_index] = result

    results = [results_by_index[index] for index in sorted(results_by_index)]
    missing = len(scheduled_matches) - len(results)
    scheduler = {
        "thread_budget": budget.thread_budget,
        "memory_budget_bytes": budget.memory_budget_bytes,
        "per_match_memory_bytes": budget.per_match_memory_bytes,
        "effective_workers": desired_workers,
        "queued_matches": max(0, len(runnable) - desired_workers),
        "degraded": bool(degradations),
        "degradations": degradations,
        "completion": {
            "scheduled": len(scheduled_matches),
            "completed": len(results),
            "denied": denied,
            "missing": missing,
        },
    }
    return SchedulingResult(
        matches=sorted(results, key=lambda result: result.match_id),
        scheduler=scheduler,
    )
