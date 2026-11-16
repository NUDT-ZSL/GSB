"""End-to-end evaluation orchestration and adaptive path selection."""

from __future__ import annotations

import time
import tracemalloc
from pathlib import Path
from typing import Literal

from .aggregation import AggregationResult, FullWindowAggregator, StreamingWindowAggregator
from .alerts import AlertStateMachine
from .config import EvaluationConfig
from .errors import ConfigError
from .loader import SLIEventStream
from .models import EvaluationSession, ResourceReport


PathMode = Literal["auto", "full", "streaming"]

_EVENT_ESTIMATE_BYTES = 72
_RING_SLOT_ESTIMATE_BYTES = 56
_WINDOW_ESTIMATE_BYTES = 704
_STEP_ESTIMATE_BYTES = 288
_ALERT_ESTIMATE_BYTES = 512


def _estimate_peak_memory(
    *,
    path: str,
    row_count: int,
    threshold: int,
    config: EvaluationConfig,
    window_count: int,
    step_count: int,
) -> tuple[int, str]:
    if path == "full":
        event_bytes = row_count * _EVENT_ESTIMATE_BYTES
        estimator_text = (
            "full: row_count*72 + windows*704 + steps*288 + alerts*512; "
            "retained core structures only"
        )
    else:
        ring_slots = max(config.windows.long_minutes, config.windows.short_minutes) + 2
        buffer_rows = min(row_count, threshold)
        event_bytes = buffer_rows * _EVENT_ESTIMATE_BYTES + ring_slots * _RING_SLOT_ESTIMATE_BYTES
        estimator_text = (
            "streaming: min(row_count,threshold)*72 + ring_slots*56 + "
            "windows*704 + steps*288 + alerts*512; retained core structures only"
        )
    estimate = (
        event_bytes
        + window_count * _WINDOW_ESTIMATE_BYTES
        + step_count * _STEP_ESTIMATE_BYTES
        + step_count * _ALERT_ESTIMATE_BYTES
    )
    return estimate, estimator_text


def _aggregate_adaptive(
    stream: SLIEventStream,
    config: EvaluationConfig,
    force_path: PathMode,
) -> AggregationResult:
    if force_path not in {"auto", "full", "streaming"}:
        raise ConfigError("force_path must be 'auto', 'full', or 'streaming'")

    threshold = config.adaptive.row_threshold
    full_buffer = []
    streaming: StreamingWindowAggregator | None = None

    if force_path == "streaming" or (force_path == "auto" and threshold == 0):
        streaming = StreamingWindowAggregator(config)

    for event in stream:
        if force_path == "full":
            full_buffer.append(event)
            continue
        if streaming is not None:
            streaming.feed(event)
            continue

        full_buffer.append(event)
        if len(full_buffer) > threshold:
            streaming = StreamingWindowAggregator(config)
            for buffered_event in full_buffer:
                streaming.feed(buffered_event)
            full_buffer.clear()

    if streaming is not None:
        return streaming.finalize()
    return FullWindowAggregator(config).aggregate(full_buffer)


def evaluate_file(
    path: str | Path,
    config: EvaluationConfig,
    *,
    data_version: str | None = None,
    force_path: PathMode = "auto",
) -> EvaluationSession:
    """Load, validate, aggregate, and alert on one local SLI source.

    A single file pass is used in every mode. In auto mode the first
    ``row_threshold + 1`` records are buffered; exceeding the threshold switches
    to the streaming ring implementation without reopening the file.
    """

    source_path = Path(path)
    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    tracemalloc.reset_peak()
    started_ns = time.perf_counter_ns()
    try:
        stream = SLIEventStream(source_path, config.sli)
        aggregation = _aggregate_adaptive(stream, config, force_path)
        alerting = AlertStateMachine(config).evaluate(
            aggregation.long_windows, aggregation.short_windows
        )
        elapsed_ns = time.perf_counter_ns() - started_ns
        _, peak_observed = tracemalloc.get_traced_memory()
    finally:
        if not was_tracing:
            tracemalloc.stop()

    if stream.metadata is None:
        raise RuntimeError("validated SLI stream did not produce metadata")
    metadata = stream.metadata
    resolved_path = str(source_path.resolve())
    version = data_version if data_version is not None else f"sha256:{metadata.data_sha256}"
    window_count = len(aggregation.long_windows) + len(aggregation.short_windows)
    step_count = len(alerting.steps)
    estimated_peak, estimator_text = _estimate_peak_memory(
        path=aggregation.path,
        row_count=metadata.row_count,
        threshold=config.adaptive.row_threshold,
        config=config,
        window_count=window_count,
        step_count=step_count,
    )
    resource = ResourceReport(
        adaptive_path=aggregation.path,
        auto_row_threshold=config.adaptive.row_threshold,
        row_count=metadata.row_count,
        peak_memory_estimate_bytes=estimated_peak,
        peak_memory_observed_bytes=peak_observed,
        elapsed_ms=elapsed_ns // 1_000_000,
        elapsed_us=elapsed_ns // 1_000,
        memory_accounting=(
            "tracemalloc peak from the start of file parsing through alert construction; "
            "excludes interpreter startup and objects allocated before this evaluation"
        ),
        estimator=estimator_text,
    )
    return EvaluationSession(
        config=config,
        data_version=version,
        data_sha256=metadata.data_sha256,
        source_path=resolved_path,
        row_count=metadata.row_count,
        first_minute=metadata.first_minute,
        last_minute=metadata.last_minute,
        long_windows=aggregation.long_windows,
        short_windows=aggregation.short_windows,
        steps=alerting.steps,
        alert_records=alerting.records,
        alert_transitions=alerting.transitions,
        final_alert_state=alerting.final_state,
        resource=resource,
    )
