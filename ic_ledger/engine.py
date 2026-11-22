"""Pure deterministic interrupt-coalescing simulation."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

import numpy as np

from .fixedpoint import ConversionResult, fraction_decimal, frames_to_ticks
from .models import (
    ConversionTrace,
    EngineConfig,
    FlowRecord,
    PathState,
    QueueRuntime,
    QueueSpec,
    SampleDocument,
)


@dataclass
class LedgerRow:
    record_seq: int
    iface: str
    queue: int
    direction: str
    param_version: int
    duration_us: int
    total_frames: int
    total_bytes: int
    fixed_irq_delta: int
    fixed_irq_total: int
    fixed_trigger: str
    fixed_window_occupancy: Fraction
    fixed_irq_rate: Fraction
    fixed_throughput_bps: Fraction
    float_irq_delta: int
    float_irq_total: int
    float_trigger: str
    float_window_occupancy: float
    float_irq_rate: float
    float_throughput_bps: float
    residue_after: Fraction
    discarded_total: Fraction
    conversion_error_us: Fraction
    arithmetic_error_us: Fraction
    error_bound_us: Fraction
    metric_deviations: dict[str, tuple[Fraction, Fraction]]
    abs_deviation: Fraction
    relative_deviation: Fraction
    divergence_metric: str | None
    diverged: bool
    first_divergence_record: int | None
    first_divergence_metric: str | None
    conversion_traces: list[ConversionTrace] = field(default_factory=list)


@dataclass
class StepResult:
    runtime: QueueRuntime
    ledger: LedgerRow


def clone_runtime(runtime: QueueRuntime) -> QueueRuntime:
    return copy.deepcopy(runtime)


def _record_trace(
    *,
    path: str,
    param_name: str,
    scale: int,
    conversion: ConversionResult,
    record_seq: int,
    float_value: float,
) -> ConversionTrace:
    return ConversionTrace(
        path=path,
        param_name=param_name,
        scale=scale,
        exact_num=conversion.exact_num,
        exact_den=conversion.exact_den,
        rounded_units=conversion.rounded_units,
        rounding=conversion.rounding,
        discarded=conversion.discarded,
        residue_before=conversion.residue_before,
        residue_after=conversion.residue_after,
        float_value=float_value,
        record_seq=record_seq,
    )


def _trigger_source(elapsed_us: int, usecs: int, pending_frames: int, frames: int) -> str:
    time_hit = usecs > 0 and elapsed_us >= usecs
    frame_hit = frames > 0 and pending_frames >= frames
    if time_hit and frame_hit:
        return "both"
    if time_hit:
        return "time"
    if frame_hit:
        return "frames"
    if usecs == 0 and frames == 0:
        return "immediate"
    return "none"


def _trigger_source_float(
    elapsed_us: np.float64,
    usecs: int,
    pending_frames: int,
    frames: int,
) -> str:
    time_hit = usecs > 0 and elapsed_us >= np.float64(usecs)
    frame_hit = frames > 0 and pending_frames >= frames
    if time_hit and frame_hit:
        return "both"
    if time_hit:
        return "time"
    if frame_hit:
        return "frames"
    if usecs == 0 and frames == 0:
        return "immediate"
    return "none"


def _occupancy(elapsed_us: int, usecs: int, threshold_units: int, scale: int) -> Fraction:
    if usecs > 0:
        denominator = usecs * scale
    elif threshold_units > 0:
        denominator = threshold_units
    else:
        return Fraction(0)
    return Fraction(elapsed_us * scale, denominator)


def _float_occupancy(elapsed_us: int, usecs: int, threshold_us: float) -> float:
    if usecs > 0:
        denominator = np.float64(usecs)
    elif threshold_us > 0.0:
        denominator = np.float64(threshold_us)
    else:
        return np.float64(0.0)
    return np.float64(elapsed_us) / denominator


def _fraction_from_float64(value: float) -> Fraction:
    return Fraction.from_float(float(value))


def _compare_paths(
    fixed_rate: Fraction,
    fixed_throughput: Fraction,
    fixed_occupancy: Fraction,
    float_rate: float,
    float_throughput: float,
    float_occupancy: float,
    tolerance: Fraction,
) -> tuple[
    dict[str, tuple[Fraction, Fraction]],
    Fraction,
    Fraction,
    str | None,
    bool,
]:
    metrics = (
        ("irq_rate", fixed_rate, float_rate),
        ("throughput", fixed_throughput, float_throughput),
        ("window_occupancy", fixed_occupancy, float_occupancy),
    )
    max_abs = Fraction(0)
    max_relative = Fraction(0)
    max_metric: str | None = None
    diverged = False
    per_metric: dict[str, tuple[Fraction, Fraction]] = {}
    for name, fixed_value, float_value in metrics:
        float_fraction = _fraction_from_float64(float_value)
        difference = abs(fixed_value - float_fraction)
        denominator = max(abs(float_fraction), Fraction(1, 10**30))
        relative = difference / denominator
        per_metric[name] = (difference, relative)
        max_abs = max(max_abs, difference)
        if relative > max_relative:
            max_relative = relative
            max_metric = name
        if relative > tolerance:
            diverged = True
    if max_metric is None:
        max_metric = "window_occupancy"
    return per_metric, max_abs, max_relative, max_metric, diverged


def _initialize_fixed_thresholds(
    runtime: QueueRuntime,
    usecs: int,
    frames: int,
    config: EngineConfig,
    record_seq: int,
) -> list[ConversionTrace]:
    traces: list[ConversionTrace] = []
    if usecs > 0:
        ticks = usecs * config.fixed_scale
        exact = Fraction(ticks)
        traces.append(
            ConversionTrace(
                path="fixed",
                param_name="usecs_to_ticks",
                scale=config.fixed_scale,
                exact_num=exact.numerator,
                exact_den=exact.denominator,
                rounded_units=ticks,
                rounding="exact",
                discarded=Fraction(0),
                residue_before=runtime.residue,
                residue_after=runtime.residue,
                float_value=float(exact),
                record_seq=record_seq,
            )
        )
    if frames <= 0:
        return traces
    interval_float = np.float64(config.avg_frame_interval_us.numerator) / np.float64(
        config.avg_frame_interval_us.denominator
    )
    conversion = frames_to_ticks(
        frames,
        config.avg_frame_interval_us,
        config.fixed_scale,
        runtime.residue,
    )
    runtime.residue = conversion.residue_after
    runtime.discarded_total += conversion.discarded
    runtime.error_bound_ticks += conversion.discarded
    runtime.fixed.threshold_ticks = conversion.rounded_units
    runtime.float_state.threshold_us = float(
        np.float64(frames) * interval_float
    )
    traces.append(
        _record_trace(
            path="fixed",
            param_name="frames_to_equivalent_ticks",
            scale=config.fixed_scale,
            conversion=conversion,
            record_seq=record_seq,
            float_value=float(runtime.float_state.threshold_us),
        )
    )
    return traces


def _reset_window(
    runtime: QueueRuntime,
    path_state: PathState,
    *,
    clock_us: int,
    usecs_threshold: int,
    frames_threshold: int,
    config: EngineConfig,
    record_seq: int,
) -> list[ConversionTrace]:
    path_state.window_start_us = clock_us
    path_state.frames = 0
    return _initialize_fixed_thresholds(
        runtime,
        usecs_threshold,
        frames_threshold,
        config,
        record_seq,
    )


def _advance_fixed(
    runtime: QueueRuntime,
    record: FlowRecord,
    usecs: int,
    frames: int,
    config: EngineConfig,
) -> tuple[str, int, Fraction, list[ConversionTrace]]:
    traces: list[ConversionTrace] = []
    state = runtime.fixed
    if state.window_start_us is None:
        state.window_start_us = record.logical_clock_us
        traces.extend(
            _initialize_fixed_thresholds(
                runtime,
                usecs,
                frames,
                config,
                record.record_seq,
            )
        )
    elapsed_us = max(0, record.logical_clock_us - int(state.window_start_us))
    pending_frames = state.frames + record.arrival_frames
    source = _trigger_source(elapsed_us, usecs, pending_frames, frames)
    occupancy = _occupancy(elapsed_us, usecs, state.threshold_ticks, config.fixed_scale)
    delta = 0
    if source != "none":
        delta = 1
        state.interrupts += 1
        traces.extend(
            _reset_window(
                runtime,
                state,
                clock_us=record.logical_clock_us,
                usecs_threshold=usecs,
                frames_threshold=frames,
                config=config,
                record_seq=record.record_seq,
            )
        )
    else:
        state.frames = pending_frames
    return source, delta, occupancy, traces


def _advance_float(
    runtime: QueueRuntime,
    record: FlowRecord,
    usecs: int,
    frames: int,
    config: EngineConfig,
) -> tuple[str, int, float]:
    state = runtime.float_state
    interval_float = np.float64(config.avg_frame_interval_us.numerator) / np.float64(
        config.avg_frame_interval_us.denominator
    )
    if state.window_start_us is None:
        state.window_start_us = record.logical_clock_us
        state.threshold_us = float(np.float64(frames) * interval_float) if frames > 0 else 0.0
    elapsed_us = max(
        np.float64(0),
        np.float64(record.logical_clock_us) - np.float64(int(state.window_start_us)),
    )
    pending_frames = state.frames + record.arrival_frames
    source = _trigger_source_float(elapsed_us, usecs, pending_frames, frames)
    occupancy = _float_occupancy(elapsed_us, usecs, state.threshold_us)
    delta = 0
    if source != "none":
        delta = 1
        state.interrupts += 1
        state.window_start_us = record.logical_clock_us
        state.frames = 0
        state.threshold_us = float(np.float64(frames) * interval_float) if frames > 0 else 0.0
    else:
        state.frames = pending_frames
    return source, delta, float(occupancy)


def _duration_us(runtime: QueueRuntime, config: EngineConfig) -> int:
    if runtime.first_clock_us is None or runtime.last_clock_us is None:
        return config.clock_step_us
    return max(runtime.last_clock_us - runtime.first_clock_us, config.clock_step_us)


def step_record(
    runtime: QueueRuntime,
    record: FlowRecord,
    spec: QueueSpec,
    config: EngineConfig,
    *,
    param_version: int,
) -> StepResult:
    state = clone_runtime(runtime)
    state.param_version = param_version
    usecs, frames = spec.params.for_direction(record.direction)

    if state.first_clock_us is None:
        state.first_clock_us = record.logical_clock_us
    state.last_clock_us = record.logical_clock_us
    state.last_record_seq = record.record_seq
    state.total_frames += record.arrival_frames
    state.total_bytes += record.frame_bytes

    fixed_trigger, fixed_delta, fixed_occupancy, traces = _advance_fixed(
        state, record, usecs, frames, config
    )
    float_trigger, float_delta, float_occupancy = _advance_float(
        state, record, usecs, frames, config
    )
    duration = _duration_us(state, config)
    fixed_rate = Fraction(state.fixed.interrupts * 1_000_000, duration)
    fixed_throughput = Fraction(state.total_bytes * 8 * 1_000_000, duration)
    float_rate = float(
        np.float64(state.float_state.interrupts)
        / np.float64(duration)
        * np.float64(1_000_000)
    )
    float_throughput = float(
        np.float64(state.total_bytes)
        * np.float64(8)
        * np.float64(1_000_000)
        / np.float64(duration)
    )
    metric_deviations, abs_dev, relative_dev, metric, row_diverged = _compare_paths(
        fixed_rate,
        fixed_throughput,
        fixed_occupancy,
        float_rate,
        float_throughput,
        float_occupancy,
        config.float_tolerance,
    )
    if row_diverged and not state.diverged:
        state.diverged = True
        state.first_divergence_record = record.record_seq
        state.first_divergence_metric = metric

    arithmetic_bound = Fraction(duration, 1 << 52)
    conversion_error_us = state.error_bound_ticks / config.fixed_scale
    error_bound_us = conversion_error_us + arithmetic_bound
    ledger = LedgerRow(
        record_seq=record.record_seq,
        iface=record.iface,
        queue=record.queue,
        direction=record.direction,
        param_version=param_version,
        duration_us=duration,
        total_frames=state.total_frames,
        total_bytes=state.total_bytes,
        fixed_irq_delta=fixed_delta,
        fixed_irq_total=state.fixed.interrupts,
        fixed_trigger=fixed_trigger,
        fixed_window_occupancy=fixed_occupancy,
        fixed_irq_rate=fixed_rate,
        fixed_throughput_bps=fixed_throughput,
        float_irq_delta=float_delta,
        float_irq_total=state.float_state.interrupts,
        float_trigger=float_trigger,
        float_window_occupancy=float_occupancy,
        float_irq_rate=float_rate,
        float_throughput_bps=float_throughput,
        residue_after=state.residue,
        discarded_total=state.discarded_total,
        conversion_error_us=conversion_error_us,
        arithmetic_error_us=arithmetic_bound,
        error_bound_us=error_bound_us,
        metric_deviations=metric_deviations,
        abs_deviation=abs_dev,
        relative_deviation=relative_dev,
        divergence_metric=metric if row_diverged else None,
        diverged=row_diverged,
        first_divergence_record=state.first_divergence_record,
        first_divergence_metric=state.first_divergence_metric,
        conversion_traces=traces,
    )
    return StepResult(runtime=state, ledger=ledger)


def initial_runtime(
    iface: str,
    queue: int,
    direction: str,
    param_version: int,
) -> QueueRuntime:
    return QueueRuntime(
        iface=iface,
        queue=queue,
        direction=direction,
        param_version=param_version,
    )


def queue_records(
    document: SampleDocument,
    iface: str,
    queue: int,
    direction: str | None = None,
) -> list[FlowRecord]:
    records = [
        item
        for item in document.flows
        if item.iface == iface
        and item.queue == queue
        and (direction is None or item.direction == direction)
    ]
    return sorted(records, key=lambda item: item.record_seq)


def replay_queue(
    document: SampleDocument,
    spec: QueueSpec,
    direction: str,
    *,
    param_version: int = 0,
) -> tuple[QueueRuntime, list[LedgerRow]]:
    state = initial_runtime(spec.iface, spec.queue, direction, param_version)
    rows: list[LedgerRow] = []
    for record in queue_records(document, spec.iface, spec.queue, direction):
        result = step_record(
            state,
            record,
            spec,
            document.config,
            param_version=param_version,
        )
        state = result.runtime
        rows.append(result.ledger)
    return state, rows


def replay_document(
    document: SampleDocument,
    *,
    param_versions: dict[str, int] | None = None,
) -> dict[tuple[str, int, str], tuple[QueueRuntime, list[LedgerRow]]]:
    versions = param_versions or {}
    result: dict[tuple[str, int, str], tuple[QueueRuntime, list[LedgerRow]]] = {}
    for spec in document.sorted_queues():
        directions = sorted(
            {
                item.direction
                for item in document.flows
                if item.iface == spec.iface and item.queue == spec.queue
            }
        )
        for direction in directions:
            key = (spec.iface, spec.queue, direction)
            result[key] = replay_queue(
                document,
                spec,
                direction,
                param_version=versions.get(spec.key, 0),
            )
    return result


def runtime_summary(runtime: QueueRuntime, config: EngineConfig) -> dict[str, Any]:
    duration = _duration_us(runtime, config)
    fixed_rate = Fraction(runtime.fixed.interrupts * 1_000_000, duration)
    fixed_throughput = Fraction(runtime.total_bytes * 8 * 1_000_000, duration)
    float_rate = float(
        np.float64(runtime.float_state.interrupts)
        / np.float64(duration)
        * np.float64(1_000_000)
    )
    float_throughput = float(
        np.float64(runtime.total_bytes)
        * np.float64(8)
        * np.float64(1_000_000)
        / np.float64(duration)
    )
    return {
        "discarded_total_decimal": fraction_decimal(runtime.discarded_total),
        "direction": runtime.direction,
        "diverged": runtime.diverged,
        "error_bound_us": fraction_decimal(
            runtime.error_bound_ticks / config.fixed_scale
            + Fraction(duration, 1 << 52)
        ),
        "first_divergence_metric": runtime.first_divergence_metric,
        "first_divergence_record": runtime.first_divergence_record,
        "fixed_interrupts": runtime.fixed.interrupts,
        "fixed_irq_rate_decimal": fraction_decimal(fixed_rate),
        "fixed_throughput_bps_decimal": fraction_decimal(fixed_throughput),
        "float_interrupts": runtime.float_state.interrupts,
        "float_irq_rate": float_rate,
        "float_throughput_bps": float_throughput,
        "frames": runtime.total_frames,
        "bytes": runtime.total_bytes,
        "residue": fraction_decimal(runtime.residue),
    }
