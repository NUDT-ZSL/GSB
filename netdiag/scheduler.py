from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Mapping

from .clock import LogicalClock
from .models import (
    DiagnosticConfig,
    ProbeObservation,
    ProbeRecord,
    ProbeSpec,
    RoundSpec,
    TargetSpec,
)

ProbeCallable = Callable[[TargetSpec, ProbeSpec], ProbeObservation]


@dataclass(frozen=True)
class RoundExecution:
    records: tuple[ProbeRecord, ...]
    clock_before_ms: int
    clock_after_ms: int
    deadline_ms: int
    abandoned: tuple[ProbeRecord, ...]


def _failure_record(
    *,
    config_version: int,
    probe: ProbeSpec,
    target: TargetSpec,
    outcome: str,
    abandon_kind: str,
    start_ms: int,
    end_ms: int,
    detail: str,
    observed_latency_ms: int | None = None,
) -> ProbeRecord:
    return ProbeRecord(
        config_version=config_version,
        round_no=probe.round_no,
        target=probe.target,
        seq=probe.seq,
        probe_type=probe.probe_type,
        plan_index=probe.plan_index,
        logical_start_ms=start_ms,
        logical_end_ms=end_ms,
        outcome="abandoned",
        abandon_kind=abandon_kind,
        counted_in_loss=False,
        excluded_from_loss=True,
        response_status=None,
        expected_status=target.expected_status,
        observed_latency_ms=observed_latency_ms,
        detail=f"{detail}; observed outcome before exclusion: {outcome}",
    )


def execute_round(
    *,
    round_spec: RoundSpec,
    targets: Mapping[str, TargetSpec],
    config: DiagnosticConfig,
    config_version: int,
    clock_watermark_ms: int,
    probe_fn: ProbeCallable,
    precompleted: Mapping[int, ProbeRecord] | None = None,
) -> RoundExecution:
    """Execute one round under both concurrency and logical-time budgets.

    The scheduler starts batches in target round-robin order. A batch starts
    while logical time is before the deadline. Any started observation whose
    natural logical end exceeds the deadline is an active abandonment, not a
    packet loss. Any probe never started is a not-started abandonment.
    """

    clock = LogicalClock(
        start_ms=max(round_spec.logical_tick, clock_watermark_ms),
        step_ms=config.clock_step_ms,
    )
    clock_before = clock.now_ms
    deadline = clock_before + config.round_budget_ms

    precompleted = precompleted or {}
    grouped: dict[str, deque[ProbeSpec]] = {}
    group_order: list[str] = []
    for probe in round_spec.probes:
        if probe.target not in grouped:
            grouped[probe.target] = deque()
            group_order.append(probe.target)
        grouped[probe.target].append(probe)

    pending = sum(len(items) for items in grouped.values())
    records: list[ProbeRecord] = []
    group_cursor = 0

    for probe in round_spec.probes:
        fixed = precompleted.get(probe.seq)
        if (
            fixed is not None
            and fixed.outcome == "abandoned"
            and fixed.abandon_kind == "not_started_budget"
        ):
            queue = grouped[probe.target]
            if queue and queue[0].seq == probe.seq:
                queue.popleft()
            else:
                try:
                    queue.remove(probe)
                except ValueError:
                    pass
            pending -= 1
            records.append(fixed)

    def abandon_not_started() -> None:
        nonlocal pending, group_cursor
        while pending:
            if not group_order:
                break
            target_name = group_order[group_cursor % len(group_order)]
            group_cursor += 1
            queue = grouped[target_name]
            if not queue:
                continue
            probe = queue.popleft()
            pending -= 1
            target = targets[probe.target]
            records.append(
                _failure_record(
                    config_version=config_version,
                    probe=probe,
                    target=target,
                    outcome="not_started",
                    abandon_kind="not_started_budget",
                    start_ms=deadline,
                    end_ms=deadline,
                    detail=(
                        "probe was never started because the round logical-time "
                        "budget was exhausted; excluded from loss denominator"
                    ),
                )
            )

    while pending and clock.now_ms < deadline:
        selected: list[ProbeSpec] = []
        slot_scan = 0
        max_scan = len(group_order) * 2 + pending
        while len(selected) < config.max_concurrency and slot_scan < max_scan:
            target_name = group_order[group_cursor % len(group_order)]
            group_cursor += 1
            slot_scan += 1
            queue = grouped[target_name]
            if not queue:
                continue
            selected.append(queue.popleft())
        if not selected:
            break
        start_ms = clock.now_ms
        batch_results = _run_batch(
            selected,
            targets,
            probe_fn,
            config.max_concurrency,
            precompleted=precompleted,
        )
        latest_end = start_ms
        for probe, observation in batch_results:
            target = targets[probe.target]
            fixed = precompleted.get(probe.seq)
            if fixed is not None:
                records.append(fixed)
                latest_end = max(latest_end, fixed.logical_end_ms)
                continue
            natural_end = _quantize_ceil(
                start_ms + observation.logical_duration_ms,
                config.clock_step_ms,
            )
            if natural_end > deadline:
                records.append(
                    _failure_record(
                        config_version=config_version,
                        probe=probe,
                        target=target,
                        outcome=observation.outcome,
                        abandon_kind="active_budget_cutoff",
                        start_ms=start_ms,
                        end_ms=deadline,
                        detail=(
                            "started probe was actively abandoned at the round "
                            "deadline before its own wait constraint; excluded "
                            "from loss denominator"
                        ),
                        observed_latency_ms=observation.observed_latency_ms,
                    )
                )
                latest_end = max(latest_end, deadline)
                continue
            counted = observation.outcome != "success"
            records.append(
                ProbeRecord(
                    config_version=config_version,
                    round_no=probe.round_no,
                    target=probe.target,
                    seq=probe.seq,
                    probe_type=probe.probe_type,
                    plan_index=probe.plan_index,
                    logical_start_ms=start_ms,
                    logical_end_ms=natural_end,
                    outcome=observation.outcome,
                    abandon_kind=None,
                    counted_in_loss=counted,
                    excluded_from_loss=False,
                    response_status=observation.response_status,
                    expected_status=target.expected_status,
                    observed_latency_ms=observation.observed_latency_ms,
                    detail=observation.detail,
                )
            )
            latest_end = max(latest_end, natural_end)
        pending -= len(selected)
        clock.advance_to(min(latest_end, deadline))

    if pending:
        abandon_not_started()

    records.sort(key=lambda record: record.plan_index)
    abandoned = tuple(record for record in records if record.outcome == "abandoned")
    return RoundExecution(
        records=tuple(records),
        clock_before_ms=clock_before,
        clock_after_ms=max(clock.now_ms, deadline if abandoned else clock.now_ms),
        deadline_ms=deadline,
        abandoned=abandoned,
    )


def _quantize_ceil(value: int, step_ms: int) -> int:
    return ((value + step_ms - 1) // step_ms) * step_ms


def _run_batch(
    probes: list[ProbeSpec],
    targets: Mapping[str, TargetSpec],
    probe_fn: ProbeCallable,
    workers: int,
    precompleted: Mapping[int, ProbeRecord] | None = None,
) -> list[tuple[ProbeSpec, ProbeObservation]]:
    pairs: list[tuple[ProbeSpec, ProbeObservation]] = []
    fixed = precompleted or {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(probe_fn, targets[probe.target], probe): probe
            for probe in probes
            if probe.seq not in fixed
        }
        for future, probe in futures.items():
            pairs.append((probe, future.result()))
        for probe in probes:
            if probe.seq in fixed:
                record = fixed[probe.seq]
                pairs.append(
                    (
                        probe,
                        ProbeObservation(
                            outcome=record.outcome,
                            logical_duration_ms=max(
                                0, record.logical_end_ms - record.logical_start_ms
                            ),
                            response_status=record.response_status,
                            detail=record.detail,
                            observed_latency_ms=record.observed_latency_ms,
                        ),
                    )
                )
    pairs.sort(key=lambda pair: pair[0].plan_index)
    return pairs
