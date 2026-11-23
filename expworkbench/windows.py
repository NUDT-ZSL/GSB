from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Iterable

from .canonical import (
    MEAN_PRECISION,
    canonical_json,
    decimal_from_text,
    decimal_text,
    digest_json,
    parse_json_text,
    quantize_mean,
)
from .db import (
    allocate_sequence,
    get_meta,
    get_meta_int,
    require_active_snapshot,
    set_meta,
    transaction,
)
from .errors import (
    E_CLOCK_REGRESSION,
    E_STATE_CORRUPT,
    E_WINDOW_ARGUMENT,
    E_WINDOW_ASSIGNMENT,
    fail,
)
from .faults import maybe_fault
from .state import load_experiments, load_metric_records, snapshot_view_rows
from .validation import Experiment, MetricRecord


@dataclass
class WindowComputation:
    experiment_id: str
    metric: str
    window_start_ms: int
    window_end_ms: int
    window_number: int
    records: list[MetricRecord]
    sum_text: str
    mean_text: str
    min_text: str
    max_text: str
    precision_sensitive: bool
    summary_json:str
    summary_digest: str
    input_digest: str
    lineage_fingerprint: str


def validate_window_ms(window_ms: int) -> int:
    if isinstance(window_ms, bool) or not isinstance(window_ms, int) or window_ms <= 0:
        fail(E_WINDOW_ARGUMENT, "window_ms must be a positive integer")
    return window_ms


def window_for_clock(logical_clock_ms: int, window_ms: int) -> tuple[int, int, int]:
    start = (logical_clock_ms // window_ms) * window_ms
    end = start + window_ms
    return start, end, start // window_ms


def _sum_decimals(values: Iterable[Decimal]) -> Decimal:
    with localcontext() as context:
        context.prec = 200
        total = Decimal(0)
        for value in values:
            total += value
        return total


def _mean_decimals(total: Decimal, count: int) -> tuple[Decimal, Decimal, bool]:
    with localcontext() as context:
        context.prec = 200
        raw = total / Decimal(count)
        try:
            rounded = raw.quantize(MEAN_PRECISION)
        except InvalidOperation:
            rounded = raw
        return raw, rounded, raw != rounded


def _view_map(connection: sqlite3.Connection, snapshot_version: str) -> dict[tuple[str, str], sqlite3.Row]:
    return {
        (str(row["experiment_id"]), str(row["metric"])): row
        for row in snapshot_view_rows(connection, snapshot_version)
    }


def dependency_fingerprints(experiments: dict[str, Experiment]) -> dict[str, str]:
    cache: dict[str, str] = {}

    def compute(experiment_id: str) -> str:
        if experiment_id in cache:
            return cache[experiment_id]
        experiment = experiments[experiment_id]
        parent_fingerprint = (
            compute(experiment.parent_id) if experiment.parent_id is not None else None
        )
        references = {
            metric: {
                "target_experiment_id": target[0],
                "target_metric": target[1],
                "target_dependency_fingerprint": compute(target[0]),
            }
            for metric, target in sorted(experiment.metric_references.items())
        }
        cache[experiment_id] = digest_json(
            {
                "experiment_fingerprint": experiment.fingerprint,
                "parent_experiment_id": experiment.parent_id,
                "parent_dependency_fingerprint": parent_fingerprint,
                "metric_references": references,
            }
        )
        return cache[experiment_id]

    for identifier in sorted(experiments):
        compute(identifier)
    return cache


def metric_dependency_fingerprint(
    experiment: Experiment,
    metric: str,
    experiments: dict[str, Experiment],
    experiment_dependencies: dict[str, str],
    view_row: sqlite3.Row | None,
) -> str:
    target = experiment.metric_references.get(metric)
    target_payload = None
    if target is not None:
        target_experiment = experiments.get(target[0])
        target_payload = {
            "experiment_id": target[0],
            "metric": target[1],
            "target_dependency_fingerprint": (
                None
                if target_experiment is None
                else experiment_dependencies.get(target[0])
            ),
        }
    snapshot_payload = None
    if view_row is not None:
        snapshot_payload = {
            "source_format": view_row["selected_source_format"],
            "source_version": view_row["selected_source_version"],
            "entry_key": view_row["selected_entry_key"],
            "selected_clock_ms": view_row["selected_clock_ms"],
            "selected_generation": view_row["selected_generation"],
            "metric_digest": view_row["metric_digest"],
            "absolute_refresh_at_ms": view_row["absolute_refresh_at_ms"],
            "conversion_scale": view_row["conversion_scale"],
            "conversion_remainder_ms": view_row["conversion_remainder_ms"],
            "rounding_direction": view_row["rounding_direction"],
            "tombstone": bool(view_row["tombstone"]),
        }
    return digest_json(
        {
            "experiment_id": experiment.id,
            "metric": metric,
            "experiment_dependency_fingerprint": experiment_dependencies[experiment.id],
            "metric_reference": target_payload,
            "snapshot_entry": snapshot_payload,
        }
    )


def refresh_trace(view_row: sqlite3.Row | None) -> dict[str, Any]:
    if view_row is None:
        return {"selected": None, "reason": "no_snapshot_entry"}
    decision = parse_json_text(str(view_row["decision_json"]))
    return {
        "selected_source_format": view_row["selected_source_format"],
        "selected_source_version": view_row["selected_source_version"],
        "selected_entry_key": view_row["selected_entry_key"],
        "selected_clock_ms": int(view_row["selected_clock_ms"]),
        "selected_trace_generation": int(view_row["selected_generation"]),
        "selected_priority": int(view_row["selected_priority"]),
        "original_time_representation": parse_json_text(str(view_row["raw_time_json"])),
        "relative_refresh_seconds": view_row["relative_refresh_seconds"],
        "absolute_refresh_at_ms": int(view_row["absolute_refresh_at_ms"]),
        "conversion_scale": int(view_row["conversion_scale"]),
        "conversion_remainder_ms": int(view_row["conversion_remainder_ms"]),
        "rounding_direction": view_row["rounding_direction"],
        "decision_reason": decision.get("reason"),
        "tombstone": bool(view_row["tombstone"]),
    }


def compute_windows(
    records: list[MetricRecord],
    *,
    now_ms: int,
    window_ms: int,
    dependency_fingerprint: str,
    trace: dict[str, Any],
) -> list[WindowComputation]:
    validate_window_ms(window_ms)
    by_window: dict[tuple[int, int], list[MetricRecord]] = {}
    for record in records:
        if record.logical_clock_ms >= now_ms:
            continue
        start, end, _ = window_for_clock(record.logical_clock_ms, window_ms)
        if end > now_ms:
            continue
        by_window.setdefault((start, end), []).append(record)
    results: list[WindowComputation] = []
    for (start, end) in sorted(by_window):
        window_records = sorted(
            by_window[(start, end)],
            key=lambda record: (record.logical_clock_ms, record.seq, record.record_id),
        )
        values = [record.value for record in window_records]
        total = _sum_decimals(values)
        raw_mean, mean, mean_rounded = _mean_decimals(total, len(values))
        minimum = min(values)
        maximum = max(values)
        fractional = any(value != value.to_integral_value() for value in values)
        precision_sensitive = fractional or mean_rounded
        record_payloads = [
            {
                "record_id": record.record_id,
                "seq": record.seq,
                "step": record.step,
                "value": decimal_from_text(record.value_text),
                "logical_clock_ms": record.logical_clock_ms,
                "record_digest": record.record_digest,
            }
            for record in window_records
        ]
        window_number = start // window_ms
        summary_core = {
            "experiment_id": window_records[0].experiment_id,
            "metric": window_records[0].metric,
            "window_start_ms": start,
            "window_end_ms": end,
            "window_number": window_number,
            "record_count": len(window_records),
            "records": record_payloads,
            "sum": total,
            "mean": mean,
            "min": minimum,
            "max": maximum,
            "rounding": {
                "mean": "half_even",
                "precision": "0.000000000001",
                "mean_was_rounded": mean_rounded,
            },
            "precision_sensitive": precision_sensitive,
            "lineage_fingerprint": dependency_fingerprint,
            "refresh_trace": trace,
        }
        summary_json = canonical_json(summary_core)
        summary_digest = digest_json(summary_core)
        input_digest = digest_json(
            {
                "lineage_fingerprint": dependency_fingerprint,
                "window_start_ms": start,
                "window_end_ms": end,
                "record_digests": [record.record_digest for record in window_records],
                "rounding": summary_core["rounding"],
            }
        )
        results.append(
            WindowComputation(
                experiment_id=window_records[0].experiment_id,
                metric=window_records[0].metric,
                window_start_ms=start,
                window_end_ms=end,
                window_number=window_number,
                records=window_records,
                sum_text=decimal_text(total),
                mean_text=decimal_text(mean),
                min_text=decimal_text(minimum),
                max_text=decimal_text(maximum),
                precision_sensitive=precision_sensitive,
                summary_json=summary_json,
                summary_digest=summary_digest,
                input_digest=input_digest,
                lineage_fingerprint=dependency_fingerprint,
            )
        )
    return results


def build_metric_summary(
    connection: sqlite3.Connection,
    snapshot_version: str,
    experiment_id: str,
    metric: str,
    *,
    lineage_fingerprint: str,
    event_id: str | None,
    data_epoch: int,
) -> dict[str, Any] | None:
    rows = connection.execute(
        """
        SELECT * FROM windows
        WHERE snapshot_version = ? AND experiment_id = ? AND metric = ?
        ORDER BY window_start_ms
        """,
        (snapshot_version, experiment_id, metric),
    ).fetchall()
    if not rows:
        connection.execute(
            """
            DELETE FROM metric_summaries
            WHERE snapshot_version = ? AND experiment_id = ? AND metric = ?
            """,
            (snapshot_version, experiment_id, metric),
        )
        return None
    total = _sum_decimals(decimal_from_text(row["sum_text"]) for row in rows)
    record_count = sum(int(row["record_count"]) for row in rows)
    raw_mean, mean, mean_rounded = _mean_decimals(total, record_count)
    minimum = min(decimal_from_text(row["min_text"]) for row in rows)
    maximum = max(decimal_from_text(row["max_text"]) for row in rows)
    precision_sensitive = any(bool(row["precision_sensitive"]) for row in rows) or mean_rounded
    summary_core = {
        "snapshot_version": snapshot_version,
        "experiment_id": experiment_id,
        "metric": metric,
        "window_count": len(rows),
        "record_count": record_count,
        "sum": total,
        "mean": mean,
        "min": minimum,
        "max": maximum,
        "rounding": {
            "mean": "half_even",
            "precision": "0.000000000001",
            "mean_was_rounded": mean_rounded,
        },
        "precision_sensitive": precision_sensitive,
        "windows": [
            {
                "window_start_ms": int(row["window_start_ms"]),
                "window_end_ms": int(row["window_end_ms"]),
                "window_number": int(row["window_number"]),
                "summary_digest": row["summary_digest"],
            }
            for row in rows
        ],
        "lineage_fingerprint": lineage_fingerprint,
    }
    summary_json = canonical_json(summary_core)
    summary_digest = digest_json(summary_core)
    connection.execute(
        """
        INSERT INTO metric_summaries(
            snapshot_version, experiment_id, metric, window_count, record_count,
            sum_text, mean_text, min_text, max_text, precision_sensitive,
            summary_digest, summary_json, lineage_fingerprint, last_event_id,
            updated_data_epoch
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(snapshot_version, experiment_id, metric) DO UPDATE SET
            window_count = excluded.window_count,
            record_count = excluded.record_count,
            sum_text = excluded.sum_text,
            mean_text = excluded.mean_text,
            min_text = excluded.min_text,
            max_text = excluded.max_text,
            precision_sensitive = excluded.precision_sensitive,
            summary_digest = excluded.summary_digest,
            summary_json = excluded.summary_json,
            lineage_fingerprint = excluded.lineage_fingerprint,
            last_event_id = excluded.last_event_id,
            updated_data_epoch = excluded.updated_data_epoch
        """,
        (
            snapshot_version,
            experiment_id,
            metric,
            len(rows),
            record_count,
            decimal_text(total),
            decimal_text(mean),
            decimal_text(minimum),
            decimal_text(maximum),
            1 if precision_sensitive else 0,
            summary_digest,
            summary_json,
            lineage_fingerprint,
            event_id,
            data_epoch,
        ),
    )
    return {
        "snapshot_version": snapshot_version,
        "experiment_id": experiment_id,
        "metric": metric,
        "window_count": len(rows),
        "record_count": record_count,
        "sum": decimal_text(total),
        "mean": decimal_text(mean),
        "min": decimal_text(minimum),
        "max": decimal_text(maximum),
        "precision_sensitive": precision_sensitive,
        "summary_digest": summary_digest,
    }


def summarize_computed_windows(
    computed: list[WindowComputation],
    *,
    snapshot_version: str,
    experiment_id: str,
    metric: str,
    lineage_fingerprint: str,
) -> tuple[dict[str, Any], str] | None:
    if not computed:
        return None
    total = _sum_decimals(decimal_from_text(window.sum_text) for window in computed)
    record_count = sum(len(window.records) for window in computed)
    _, mean, mean_rounded = _mean_decimals(total, record_count)
    minimum = min(decimal_from_text(window.min_text) for window in computed)
    maximum = max(decimal_from_text(window.max_text) for window in computed)
    precision_sensitive = any(window.precision_sensitive for window in computed) or mean_rounded
    payload = {
        "snapshot_version": snapshot_version,
        "experiment_id": experiment_id,
        "metric": metric,
        "window_count": len(computed),
        "record_count": record_count,
        "sum": total,
        "mean": mean,
        "min": minimum,
        "max": maximum,
        "rounding": {
            "mean": "half_even",
            "precision": "0.000000000001",
            "mean_was_rounded": mean_rounded,
        },
        "precision_sensitive": precision_sensitive,
        "windows": [
            {
                "window_start_ms": window.window_start_ms,
                "window_end_ms": window.window_end_ms,
                "window_number": window.window_number,
                "summary_digest": window.summary_digest,
            }
            for window in computed
        ],
        "lineage_fingerprint": lineage_fingerprint,
    }
    return payload, digest_json(payload)


def build_comparison(
    experiment: Experiment,
    metric: str,
    window: WindowComputation,
    *,
    snapshot_version: str,
    data_epoch: int,
) -> dict[str, Any]:
    value = decimal_from_text(window.mean_text)
    baseline = experiment.baseline_metrics.get(metric)
    precision_sensitive = window.precision_sensitive
    if baseline is None:
        direction = "unavailable"
        delta_text = None
        abs_delta_text = None
        relative_delta_text = None
        baseline_text = None
    else:
        baseline_text = decimal_text(baseline)
        delta = value - baseline
        delta_text = decimal_text(delta)
        abs_delta_text = decimal_text(abs(delta))
        if delta > 0:
            direction = "up"
        elif delta < 0:
            direction = "down"
        else:
            direction = "flat"
        if baseline == 0:
            relative_delta_text = None
        else:
            with localcontext() as context:
                context.prec = 200
                relative = delta / abs(baseline)
                rounded = relative.quantize(MEAN_PRECISION)
            relative_delta_text = decimal_text(rounded)
            precision_sensitive = precision_sensitive or relative != rounded
    payload = {
        "snapshot_version": snapshot_version,
        "experiment_id": experiment.id,
        "metric": metric,
        "window_start_ms": window.window_start_ms,
        "window_end_ms": window.window_end_ms,
        "window_number": window.window_number,
        "baseline_value": baseline_text,
        "value": window.mean_text,
        "delta": delta_text,
        "abs_delta": abs_delta_text,
        "relative_delta": relative_delta_text,
        "direction": direction,
        "precision_sensitive": precision_sensitive,
        "window_summary_digest": window.summary_digest,
    }
    digest = digest_json(payload)
    return {
        **payload,
        "comparison_digest": digest,
        "comparison_json": canonical_json(payload),
        "updated_data_epoch": data_epoch,
    }


def materialize_metric(
    connection: sqlite3.Connection,
    *,
    snapshot_version: str,
    experiment: Experiment,
    metric: str,
    records: list[MetricRecord],
    now_ms: int,
    window_ms: int,
    event_id: str | None,
    data_epoch: int,
    only_new: bool,
    experiments: dict[str, Experiment],
    experiment_dependencies: dict[str, str],
    view: dict[tuple[str, str], sqlite3.Row],
) -> dict[str, Any]:
    view_row = view.get((experiment.id, metric))
    dependency = metric_dependency_fingerprint(
        experiment,
        metric,
        experiments,
        experiment_dependencies,
        view_row,
    )
    trace = refresh_trace(view_row)
    if view_row is not None and bool(view_row["tombstone"]):
        return {
            "experiment_id": experiment.id,
            "metric": metric,
            "skipped": True,
            "reason": "tombstone",
            "windows_updated": 0,
            "windows_reused": 0,
        }
    watermark_row = connection.execute(
        """
        SELECT last_seq FROM refresh_watermarks
        WHERE snapshot_version = ? AND experiment_id = ? AND metric = ?
        """,
        (snapshot_version, experiment.id, metric),
    ).fetchone()
    watermark = 0 if watermark_row is None else int(watermark_row["last_seq"])
    computed = compute_windows(
        records,
        now_ms=now_ms,
        window_ms=window_ms,
        dependency_fingerprint=dependency,
        trace=trace,
    )
    updated = 0
    reused = 0
    max_closed_seq = 0
    for window in computed:
        window_seq = max(record.seq for record in window.records)
        max_closed_seq = max(max_closed_seq, window_seq)
        if only_new and watermark > 0 and window_seq <= watermark:
            continue
        existing = connection.execute(
            """
            SELECT * FROM windows
            WHERE snapshot_version = ? AND experiment_id = ? AND metric = ?
              AND window_start_ms = ? AND window_end_ms = ?
            """,
            (
                snapshot_version,
                experiment.id,
                metric,
                window.window_start_ms,
                window.window_end_ms,
            ),
        ).fetchone()
        if existing is not None and str(existing["input_digest"]) == window.input_digest:
            reused += 1
            continue
        for record in window.records:
            assignment = connection.execute(
                """
                SELECT experiment_id, metric, window_start_ms, window_end_ms
                FROM window_record_assignments
                WHERE snapshot_version = ? AND record_id = ?
                """,
                (snapshot_version, record.record_id),
            ).fetchone()
            if assignment is None:
                connection.execute(
                    """
                    INSERT INTO window_record_assignments(
                        snapshot_version, record_id, experiment_id, metric,
                        window_start_ms, window_end_ms
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_version,
                        record.record_id,
                        experiment.id,
                        metric,
                        window.window_start_ms,
                        window.window_end_ms,
                    ),
                )
            elif (
                assignment["experiment_id"] != experiment.id
                or assignment["metric"] != metric
                or int(assignment["window_start_ms"]) != window.window_start_ms
                or int(assignment["window_end_ms"]) != window.window_end_ms
            ):
                fail(
                    E_WINDOW_ASSIGNMENT,
                    "A metric record was assigned to two different windows",
                    record_id=record.record_id,
                    first={
                        "experiment_id": assignment["experiment_id"],
                        "metric": assignment["metric"],
                        "window_start_ms": int(assignment["window_start_ms"]),
                        "window_end_ms": int(assignment["window_end_ms"]),
                    },
                    second={
                        "experiment_id": experiment.id,
                        "metric": metric,
                        "window_start_ms": window.window_start_ms,
                        "window_end_ms": window.window_end_ms,
                    },
                )
        connection.execute(
            """
            INSERT INTO windows(
                snapshot_version, experiment_id, metric, window_start_ms,
                window_end_ms, window_number, record_count, sum_text, mean_text,
                min_text, max_text, precision_sensitive, summary_digest,
                summary_json, input_digest, lineage_fingerprint, last_event_id,
                updated_data_epoch
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_version, experiment_id, metric, window_start_ms, window_end_ms)
            DO UPDATE SET
                record_count = excluded.record_count,
                sum_text = excluded.sum_text,
                mean_text = excluded.mean_text,
                min_text = excluded.min_text,
                max_text = excluded.max_text,
                precision_sensitive = excluded.precision_sensitive,
                summary_digest = excluded.summary_digest,
                summary_json = excluded.summary_json,
                input_digest = excluded.input_digest,
                lineage_fingerprint = excluded.lineage_fingerprint,
                last_event_id = excluded.last_event_id,
                updated_data_epoch = excluded.updated_data_epoch
            """,
            (
                snapshot_version,
                experiment.id,
                metric,
                window.window_start_ms,
                window.window_end_ms,
                window.window_number,
                len(window.records),
                window.sum_text,
                window.mean_text,
                window.min_text,
                window.max_text,
                1 if window.precision_sensitive else 0,
                window.summary_digest,
                window.summary_json,
                window.input_digest,
                dependency,
                event_id,
                data_epoch,
            ),
        )
        comparison = build_comparison(
            experiment,
            metric,
            window,
            snapshot_version=snapshot_version,
            data_epoch=data_epoch,
        )
        connection.execute(
            """
            INSERT INTO baseline_comparisons(
                snapshot_version, experiment_id, metric, window_start_ms,
                window_end_ms, window_number, baseline_value_text, value_text,
                delta_text, abs_delta_text, relative_delta_text, direction,
                precision_sensitive, comparison_digest, comparison_json,
                updated_data_epoch
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_version, experiment_id, metric, window_start_ms, window_end_ms)
            DO UPDATE SET
                baseline_value_text = excluded.baseline_value_text,
                value_text = excluded.value_text,
                delta_text = excluded.delta_text,
                abs_delta_text = excluded.abs_delta_text,
                relative_delta_text = excluded.relative_delta_text,
                direction = excluded.direction,
                precision_sensitive = excluded.precision_sensitive,
                comparison_digest = excluded.comparison_digest,
                comparison_json = excluded.comparison_json,
                updated_data_epoch = excluded.updated_data_epoch
            """,
            (
                snapshot_version,
                experiment.id,
                metric,
                window.window_start_ms,
                window.window_end_ms,
                window.window_number,
                comparison["baseline_value"],
                comparison["value"],
                comparison["delta"],
                comparison["abs_delta"],
                comparison["relative_delta"],
                comparison["direction"],
                1 if comparison["precision_sensitive"] else 0,
                comparison["comparison_digest"],
                comparison["comparison_json"],
                data_epoch,
            ),
        )
        updated += 1
    metric_summary = build_metric_summary(
        connection,
        snapshot_version,
        experiment.id,
        metric,
        lineage_fingerprint=dependency,
        event_id=event_id,
        data_epoch=data_epoch,
    )
    if max_closed_seq > watermark:
        connection.execute(
            """
            INSERT INTO refresh_watermarks(
                snapshot_version, experiment_id, metric, last_seq,
                last_clock_ms, updated_data_epoch
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_version, experiment_id, metric) DO UPDATE SET
                last_seq = excluded.last_seq,
                last_clock_ms = excluded.last_clock_ms,
                updated_data_epoch = excluded.updated_data_epoch
            """,
            (
                snapshot_version,
                experiment.id,
                metric,
                max_closed_seq,
                now_ms,
                data_epoch,
            ),
        )
    return {
        "experiment_id": experiment.id,
        "metric": metric,
        "windows_updated": updated,
        "windows_reused": reused,
        "metric_summary": metric_summary,
        "lineage_fingerprint": dependency,
    }


def _record_groups(
    records: list[MetricRecord],
) -> dict[tuple[str, str], list[MetricRecord]]:
    groups: dict[tuple[str, str], list[MetricRecord]] = {}
    for record in records:
        groups.setdefault((record.experiment_id, record.metric), []).append(record)
    for values in groups.values():
        values.sort(key=lambda record: (record.logical_clock_ms, record.seq, record.record_id))
    return groups


def refresh(
    connection: sqlite3.Connection,
    *,
    now_ms: int,
    window_ms: int,
) -> dict[str, Any]:
    validate_window_ms(window_ms)
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        fail(E_WINDOW_ARGUMENT, "now_ms must be a non-negative integer logical clock")
    snapshot_version = require_active_snapshot(connection)
    current_clock = get_meta_int(connection, "logical_clock_watermark_ms")
    if now_ms < current_clock:
        fail(
            E_CLOCK_REGRESSION,
            "Injected logical clock cannot move backwards",
            current_watermark_ms=current_clock,
            injected_now_ms=now_ms,
        )
    configured_window = get_meta(connection, "window_ms")
    if configured_window is not None and int(configured_window) != window_ms:
        fail(
            E_WINDOW_ARGUMENT,
            "The active workbench is bound to a different window size",
            configured_window_ms=int(configured_window),
            requested_window_ms=window_ms,
        )
    experiments = load_experiments(connection)
    records = load_metric_records(connection)
    groups = _record_groups(records)
    data_epoch = get_meta_int(connection, "data_epoch")
    view = _view_map(connection, snapshot_version)
    experiment_dependencies = dependency_fingerprints(experiments)
    input_digest = digest_json(
        {
            "snapshot_version": snapshot_version,
            "data_epoch": data_epoch,
            "now_ms": now_ms,
            "window_ms": window_ms,
            "records": [record.record_digest for record in records],
        }
    )
    run_id = digest_json(
        {
            "operation": "refresh",
            "snapshot_version": snapshot_version,
            "data_epoch": data_epoch,
            "now_ms": now_ms,
            "window_ms": window_ms,
            "input_digest": input_digest,
        }
    )
    prior_run = connection.execute(
        "SELECT * FROM refresh_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if prior_run is not None and prior_run["status"] == "complete":
        result = parse_json_text(str(prior_run["result_json"]))
        result["duplicate"] = True
        return result
    if configured_window is None:
        set_meta(connection, "window_ms", window_ms)
    results = []
    for experiment_id, metric in sorted(groups):
        experiment = experiments.get(experiment_id)
        if experiment is None:
            fail(
                E_STATE_CORRUPT,
                "Metric record references a missing experiment",
                experiment_id=experiment_id,
            )
        result = materialize_metric(
            connection,
            snapshot_version=snapshot_version,
            experiment=experiment,
            metric=metric,
            records=groups[(experiment_id, metric)],
            now_ms=now_ms,
            window_ms=window_ms,
            event_id=None,
            data_epoch=data_epoch,
            only_new=True,
            experiments=experiments,
            experiment_dependencies=experiment_dependencies,
            view=view,
        )
        results.append(result)
    set_meta(connection, "logical_clock_watermark_ms", now_ms)
    result = {
        "ok": True,
        "operation": "refresh",
        "snapshot_version": snapshot_version,
        "data_epoch": data_epoch,
        "now_ms": now_ms,
        "window_ms": window_ms,
        "run_id": run_id,
        "input_digest": input_digest,
        "windows_updated": sum(item.get("windows_updated", 0) for item in results),
        "windows_reused": sum(item.get("windows_reused", 0) for item in results),
        "metrics": sorted(
            results, key=lambda item: (item["experiment_id"], item["metric"])
        ),
        "duplicate": False,
    }
    connection.execute(
        """
        INSERT INTO refresh_runs(
            run_id, snapshot_version, data_epoch, now_ms, window_ms,
            input_digest, status, result_json
        ) VALUES (?, ?, ?, ?, ?, ?, 'complete', ?)
        """,
        (
            run_id,
            snapshot_version,
            data_epoch,
            now_ms,
            window_ms,
            input_digest,
            canonical_json(result),
        ),
    )
    maybe_fault("after_refresh_commit_stage")
    return result


def run_refresh(
    connection: sqlite3.Connection,
    *,
    now_ms: int,
    window_ms: int,
) -> dict[str, Any]:
    validate_window_ms(window_ms)
    with transaction(connection):
        result = refresh(connection, now_ms=now_ms, window_ms=window_ms)
    maybe_fault("after_refresh_commit")
    return result

