from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Any

from .canonical import digest_json, json_safe, parse_json_text
from .db import get_meta, get_meta_int, require_active_snapshot
from .errors import E_CLOCK_REGRESSION, E_STATE_CORRUPT, E_WINDOW_ARGUMENT, fail
from .snapshots import snapshot_info
from .state import (
    known_metrics,
    load_experiments,
    load_metric_records,
    snapshot_view_rows,
)
from .windows import (
    build_comparison,
    compute_windows,
    dependency_fingerprints,
    metric_dependency_fingerprint,
    refresh_trace,
    summarize_computed_windows,
)


def _decimal_as_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return json_safe(value)


def _view_details(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "source_format": row["selected_source_format"],
        "source_version": row["selected_source_version"],
        "entry_key": row["selected_entry_key"],
        "selected_clock_ms": int(row["selected_clock_ms"]),
        "selected_trace_generation": int(row["selected_generation"]),
        "source_priority": int(row["selected_priority"]),
        "raw_time": parse_json_text(str(row["raw_time_json"])),
        "relative_refresh_seconds": row["relative_refresh_seconds"],
        "absolute_refresh_at_ms": int(row["absolute_refresh_at_ms"]),
        "conversion_scale": int(row["conversion_scale"]),
        "conversion_remainder_ms": int(row["conversion_remainder_ms"]),
        "rounding_direction": row["rounding_direction"],
        "metric_digest": row["metric_digest"],
        "tombstone": bool(row["tombstone"]),
        "decision": parse_json_text(str(row["decision_json"])),
        "candidates": parse_json_text(str(row["candidates_json"])),
    }


def build_status(connection: sqlite3.Connection) -> dict[str, Any]:
    snapshot_version = require_active_snapshot(connection)
    info = snapshot_info(connection, snapshot_version)
    experiments = load_experiments(connection)
    view = {
        (str(row["experiment_id"]), str(row["metric"])): row
        for row in snapshot_view_rows(connection, snapshot_version)
    }
    summary_rows = {
        (str(row["experiment_id"]), str(row["metric"])): row
        for row in connection.execute(
            "SELECT * FROM metric_summaries WHERE snapshot_version = ?",
            (snapshot_version,),
        )
    }
    window_rows: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in connection.execute(
        """
        SELECT * FROM windows WHERE snapshot_version = ?
        ORDER BY experiment_id, metric, window_start_ms
        """,
        (snapshot_version,),
    ):
        window_rows.setdefault((row["experiment_id"], row["metric"]), []).append(row)
    comparison_rows = {
        (
            str(row["experiment_id"]),
            str(row["metric"]),
            int(row["window_start_ms"]),
        ): row
        for row in connection.execute(
            "SELECT * FROM baseline_comparisons WHERE snapshot_version = ?",
            (snapshot_version,),
        )
    }
    metric_universe = known_metrics(connection, snapshot_version, experiments)
    experiment_payloads: list[dict[str, Any]] = []
    for experiment_id in sorted(experiments):
        experiment = experiments[experiment_id]
        metric_payloads: list[dict[str, Any]] = []
        metrics_for_experiment = sorted(
            metric
            for (candidate_experiment, metric) in metric_universe
            if candidate_experiment == experiment_id
        )
        for metric in metrics_for_experiment:
            view_row = view.get((experiment_id, metric))
            summary_row = summary_rows.get((experiment_id, metric))
            windows_for_metric = []
            metric_precision = False
            for row in window_rows.get((experiment_id, metric), []):
                comparison = comparison_rows.get(
                    (experiment_id, metric, int(row["window_start_ms"]))
                )
                precision = bool(row["precision_sensitive"]) or (
                    comparison is not None and bool(comparison["precision_sensitive"])
                )
                metric_precision = metric_precision or precision
                windows_for_metric.append(
                    {
                        "window_start_ms": int(row["window_start_ms"]),
                        "window_end_ms": int(row["window_end_ms"]),
                        "window_number": int(row["window_number"]),
                        "record_count": int(row["record_count"]),
                        "sum": row["sum_text"],
                        "mean": row["mean_text"],
                        "min": row["min_text"],
                        "max": row["max_text"],
                        "precision_sensitive": precision,
                        "summary_digest": row["summary_digest"],
                        "lineage_fingerprint": row["lineage_fingerprint"],
                        "baseline_comparison": None
                        if comparison is None
                        else {
                            "baseline_value": comparison["baseline_value_text"],
                            "delta": comparison["delta_text"],
                            "abs_delta": comparison["abs_delta_text"],
                            "relative_delta": comparison["relative_delta_text"],
                            "direction": comparison["direction"],
                            "precision_sensitive": bool(
                                comparison["precision_sensitive"]
                            ),
                            "comparison_digest": comparison["comparison_digest"],
                        },
                    }
                )
            if summary_row is not None:
                metric_precision = metric_precision or bool(
                    summary_row["precision_sensitive"]
                )
            metric_payloads.append(
                {
                    "metric": metric,
                    "source": _view_details(view_row),
                    "summary": None
                    if summary_row is None
                    else {
                        "window_count": int(summary_row["window_count"]),
                        "record_count": int(summary_row["record_count"]),
                        "sum": summary_row["sum_text"],
                        "mean": summary_row["mean_text"],
                        "min": summary_row["min_text"],
                        "max": summary_row["max_text"],
                        "summary_digest": summary_row["summary_digest"],
                        "lineage_fingerprint": summary_row["lineage_fingerprint"],
                    },
                    "precision_sensitive": metric_precision,
                    "windows": windows_for_metric,
                }
            )
        experiment_payloads.append(
            {
                "experiment_id": experiment_id,
                "parent_id": experiment.parent_id,
                "dataset_version": experiment.dataset_version,
                "code_commit": experiment.code_commit,
                "logical_clock_ms": experiment.logical_clock_ms,
                "fingerprint": experiment.fingerprint,
                "hyperparameters": experiment.hyperparameters,
                "baseline_metrics": experiment.baseline_metrics,
                "alias": sorted(experiment.aliases),
                "metrics": metric_payloads,
                "precision_sensitive": any(
                    metric["precision_sensitive"] for metric in metric_payloads
                ),
            }
        )

    invalidation_payloads = []
    for event in connection.execute(
        """
        SELECT * FROM invalidation_events
        ORDER BY created_order, event_id
        """
    ):
        items = connection.execute(
            """
            SELECT * FROM invalidation_items
            WHERE event_id = ? ORDER BY experiment_id, metric
            """,
            (event["event_id"],),
        ).fetchall()
        invalidation_payloads.append(
            {
                "event_id": event["event_id"],
                "patch_id": event["patch_id"],
                "snapshot_version": event["snapshot_version"],
                "data_epoch": int(event["data_epoch"]),
                "now_ms": int(event["now_ms"]),
                "status": event["status"],
                "trigger": parse_json_text(str(event["trigger_json"])),
                "items": [
                    {
                        "experiment_id": item["experiment_id"],
                        "metric": item["metric"],
                        "reason": item["reason"],
                        "source_chain": parse_json_text(str(item["source_chain_json"])),
                        "rejected_reuse_candidates": parse_json_text(
                            str(item["rejected_candidates_json"])
                        ),
                        "status": item["status"],
                    }
                    for item in items
                ],
            }
        )
    pending = sum(1 for event in invalidation_payloads if event["status"] != "complete")
    return {
        "ok": True,
        "snapshot": info,
        "snapshot_version": snapshot_version,
        "logical_clock_watermark_ms": get_meta_int(
            connection, "logical_clock_watermark_ms"
        ),
        "data_epoch": get_meta_int(connection, "data_epoch"),
        "window_ms": int(get_meta(connection, "window_ms", "1000")),
        "experiments": experiment_payloads,
        "invalidations": invalidation_payloads,
        "pending_invalidation_events": pending,
    }


def _truth_divergences(
    connection: sqlite3.Connection,
    *,
    now_ms: int,
    window_ms: int,
) -> list[dict[str, Any]]:
    snapshot_version = require_active_snapshot(connection)
    stored_clock = get_meta_int(connection, "logical_clock_watermark_ms")
    if now_ms < stored_clock:
        fail(
            E_CLOCK_REGRESSION,
            "Diagnostic logical clock cannot be earlier than the persisted watermark",
            persisted_watermark_ms=stored_clock,
            injected_now_ms=now_ms,
        )
    if isinstance(window_ms, bool) or not isinstance(window_ms, int) or window_ms <= 0:
        fail(E_WINDOW_ARGUMENT, "window_ms must be a positive integer")
    configured_window = get_meta(connection, "window_ms")
    if configured_window is not None and int(configured_window) != window_ms:
        fail(
            E_WINDOW_ARGUMENT,
            "Diagnostic window size differs from the active workbench window",
            configured_window_ms=int(configured_window),
            requested_window_ms=window_ms,
        )
    experiments = load_experiments(connection)
    records = load_metric_records(connection)
    groups: dict[tuple[str, str], list[Any]] = {}
    for record in records:
        groups.setdefault((record.experiment_id, record.metric), []).append(record)
    for values in groups.values():
        values.sort(key=lambda record: (record.logical_clock_ms, record.seq, record.record_id))
    view = {
        (str(row["experiment_id"]), str(row["metric"])): row
        for row in snapshot_view_rows(connection, snapshot_version)
    }
    dependencies = dependency_fingerprints(experiments)
    universe = known_metrics(connection, snapshot_version, experiments)
    persisted_windows = {
        (
            str(row["experiment_id"]),
            str(row["metric"]),
            int(row["window_start_ms"]),
        ): row
        for row in connection.execute(
            "SELECT * FROM windows WHERE snapshot_version = ?", (snapshot_version,)
        )
    }
    persisted_summaries = {
        (str(row["experiment_id"]), str(row["metric"])): row
        for row in connection.execute(
            "SELECT * FROM metric_summaries WHERE snapshot_version = ?",
            (snapshot_version,),
        )
    }
    persisted_comparisons = {
        (
            str(row["experiment_id"]),
            str(row["metric"]),
            int(row["window_start_ms"]),
        ): row
        for row in connection.execute(
            "SELECT * FROM baseline_comparisons WHERE snapshot_version = ?",
            (snapshot_version,),
        )
    }
    divergences: list[dict[str, Any]] = []
    expected_keys: set[tuple[str, str, int]] = set()
    for experiment_id, metric in sorted(universe):
        experiment = experiments.get(experiment_id)
        if experiment is None:
            continue
        view_row = view.get((experiment_id, metric))
        if view_row is not None and bool(view_row["tombstone"]):
            continue
        dependency = metric_dependency_fingerprint(
            experiment,
            metric,
            experiments,
            dependencies,
            view_row,
        )
        computed = compute_windows(
            groups.get((experiment_id, metric), []),
            now_ms=now_ms,
            window_ms=window_ms,
            dependency_fingerprint=dependency,
            trace=refresh_trace(view_row),
        )
        expected_summary = summarize_computed_windows(
            computed,
            snapshot_version=snapshot_version,
            experiment_id=experiment_id,
            metric=metric,
            lineage_fingerprint=dependency,
        )
        for window in computed:
            key = (experiment_id, metric, window.window_start_ms)
            expected_keys.add(key)
            persisted = persisted_windows.get(key)
            mismatch_fields = []
            if persisted is None:
                mismatch_fields = ["missing_committed_window"]
            else:
                checks = {
                    "summary_digest": window.summary_digest,
                    "mean_text": window.mean_text,
                    "sum_text": window.sum_text,
                    "min_text": window.min_text,
                    "max_text": window.max_text,
                    "record_count": len(window.records),
                    "precision_sensitive": 1 if window.precision_sensitive else 0,
                    "lineage_fingerprint": dependency,
                }
                for field, expected in checks.items():
                    if persisted[field] != expected:
                        mismatch_fields.append(field)
            expected_comparison = build_comparison(
                experiment,
                metric,
                window,
                snapshot_version=snapshot_version,
                data_epoch=get_meta_int(connection, "data_epoch"),
            )
            persisted_comparison = persisted_comparisons.get(key)
            comparison_mismatch = []
            if persisted_comparison is None:
                comparison_mismatch = ["missing_baseline_comparison"]
            elif (
                persisted_comparison["comparison_digest"]
                != expected_comparison["comparison_digest"]
            ):
                comparison_mismatch = ["comparison_digest"]
            if mismatch_fields:
                divergences.append(
                    {
                        "kind": "window_summary",
                        "experiment_id": experiment_id,
                        "metric": metric,
                        "window_number": window.window_number,
                        "window_start_ms": window.window_start_ms,
                        "window_end_ms": window.window_end_ms,
                        "snapshot_version": snapshot_version,
                        "mismatched_fields": mismatch_fields,
                        "incremental_value": None
                        if persisted is None
                        else {
                            "mean": persisted["mean_text"],
                            "sum": persisted["sum_text"],
                            "summary_digest": persisted["summary_digest"],
                        },
                        "truth_value": {
                            "mean": window.mean_text,
                            "sum": window.sum_text,
                            "summary_digest": window.summary_digest,
                        },
                        "depends_on_snapshot_version": snapshot_version,
                    }
                )
            if comparison_mismatch:
                divergences.append(
                    {
                        "kind": "baseline_comparison",
                        "experiment_id": experiment_id,
                        "metric": metric,
                        "window_number": window.window_number,
                        "window_start_ms": window.window_start_ms,
                        "window_end_ms": window.window_end_ms,
                        "snapshot_version": snapshot_version,
                        "mismatched_fields": comparison_mismatch,
                        "incremental_value": None
                        if persisted_comparison is None
                        else {
                            "value": persisted_comparison["value_text"],
                            "delta": persisted_comparison["delta_text"],
                            "direction": persisted_comparison["direction"],
                            "comparison_digest": persisted_comparison[
                                "comparison_digest"
                            ],
                        },
                        "truth_value": {
                            "value": expected_comparison["value"],
                            "delta": expected_comparison["delta"],
                            "direction": expected_comparison["direction"],
                            "comparison_digest": expected_comparison[
                                "comparison_digest"
                            ],
                        },
                        "depends_on_snapshot_version": snapshot_version,
                    }
                )
        persisted_summary = persisted_summaries.get((experiment_id, metric))
        if expected_summary is None:
            if persisted_summary is not None:
                divergences.append(
                    {
                        "kind": "metric_summary",
                        "experiment_id": experiment_id,
                        "metric": metric,
                        "window_number": None,
                        "snapshot_version": snapshot_version,
                        "mismatched_fields": ["unexpected_metric_summary"],
                        "incremental_value": {
                            "summary_digest": persisted_summary["summary_digest"]
                        },
                        "truth_value": None,
                        "depends_on_snapshot_version": snapshot_version,
                    }
                )
        else:
            expected_payload, expected_digest = expected_summary
            if persisted_summary is None:
                divergences.append(
                    {
                        "kind": "metric_summary",
                        "experiment_id": experiment_id,
                        "metric": metric,
                        "window_number": None,
                        "snapshot_version": snapshot_version,
                        "mismatched_fields": ["missing_metric_summary"],
                        "incremental_value": None,
                        "truth_value": {
                            "mean": _decimal_as_text(expected_payload["mean"]),
                            "summary_digest": expected_digest,
                        },
                        "depends_on_snapshot_version": snapshot_version,
                    }
                )
            elif persisted_summary["summary_digest"] != expected_digest:
                divergences.append(
                    {
                        "kind": "metric_summary",
                        "experiment_id": experiment_id,
                        "metric": metric,
                        "window_number": None,
                        "snapshot_version": snapshot_version,
                        "mismatched_fields": ["summary_digest"],
                        "incremental_value": {
                            "mean": persisted_summary["mean_text"],
                            "summary_digest": persisted_summary["summary_digest"],
                        },
                        "truth_value": {
                            "mean": _decimal_as_text(expected_payload["mean"]),
                            "summary_digest": expected_digest,
                        },
                        "depends_on_snapshot_version": snapshot_version,
                    }
                )
    for key in sorted(set(persisted_windows) - expected_keys):
        row = persisted_windows[key]
        divergences.append(
            {
                "kind": "window_summary",
                "experiment_id": key[0],
                "metric": key[1],
                "window_number": int(row["window_number"]),
                "window_start_ms": int(row["window_start_ms"]),
                "window_end_ms": int(row["window_end_ms"]),
                "snapshot_version": snapshot_version,
                "mismatched_fields": ["unexpected_committed_window"],
                "incremental_value": {
                    "mean": row["mean_text"],
                    "summary_digest": row["summary_digest"],
                },
                "truth_value": None,
                "depends_on_snapshot_version": snapshot_version,
            }
        )
    for event in connection.execute(
        """
        SELECT event_id, status FROM invalidation_events
        WHERE status != 'complete' ORDER BY created_order
        """
    ):
        divergences.append(
            {
                "kind": "pending_invalidation",
                "experiment_id": None,
                "metric": None,
                "window_number": None,
                "snapshot_version": snapshot_version,
                "event_id": event["event_id"],
                "event_status": event["status"],
                "mismatched_fields": ["incomplete_dependency_recompute"],
                "incremental_value": event["status"],
                "truth_value": "complete",
                "depends_on_snapshot_version": snapshot_version,
            }
        )
    kind_priority = {
        "window_summary": 0,
        "baseline_comparison": 1,
        "metric_summary": 2,
        "pending_invalidation": 3,
    }
    divergences.sort(
        key=lambda item: (
            item.get("experiment_id") or "~",
            item.get("metric") or "~",
            kind_priority.get(item["kind"], 9),
            -1 if item.get("window_start_ms") is None else item["window_start_ms"],
            item["kind"],
        )
    )
    return divergences


def diagnose(
    connection: sqlite3.Connection,
    *,
    now_ms: int | None = None,
    window_ms: int | None = None,
) -> dict[str, Any]:
    status = build_status(connection)
    effective_now = (
        get_meta_int(connection, "logical_clock_watermark_ms")
        if now_ms is None
        else now_ms
    )
    effective_window = (
        int(get_meta(connection, "window_ms", "1000"))
        if window_ms is None
        else window_ms
    )
    divergences = _truth_divergences(
        connection, now_ms=effective_now, window_ms=effective_window
    )
    return {
        "ok": True,
        "operation": "diagnose",
        "snapshot_version": status["snapshot_version"],
        "logical_clock_watermark_ms": status["logical_clock_watermark_ms"],
        "diagnostic_now_ms": effective_now,
        "window_ms": effective_window,
        "consistent": not divergences,
        "first_divergence": None if not divergences else divergences[0],
        "divergences": divergences,
        "status": status,
    }


def verify(
    connection: sqlite3.Connection,
    *,
    now_ms: int | None = None,
    window_ms: int | None = None,
) -> dict[str, Any]:
    report = diagnose(connection, now_ms=now_ms, window_ms=window_ms)
    report["operation"] = "verify"
    report["ok"] = report["consistent"]
    return report

