"""Deterministic replication-delay and lag computation."""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Iterable

from .canonical import fraction_text, fraction_to_json
from .config import Config
from .errors import DiagnosticsError
from .models import LogRecord, PositionRecord


def _quantile(sorted_values: list[Fraction], probability: Fraction) -> Fraction:
    if not sorted_values:
        raise DiagnosticsError("E_STATE", message="quantile requires at least one observation")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = probability * (len(sorted_values) - 1)
    lower = position.numerator // position.denominator
    fraction = position - lower
    upper = min(lower + 1, len(sorted_values) - 1)
    return sorted_values[lower] + fraction * (sorted_values[upper] - sorted_values[lower])


def _fraction_json(value: Fraction) -> float | int:
    return fraction_to_json(value)


def _fraction_text(value: Fraction) -> str:
    return fraction_text(value)


def _tx_observations(record: PositionRecord) -> list[str]:
    if record.source_version == "v2":
        return ["v2.commit_time_ms", "v2.replay_time_ms"]
    return ["v1.relative_delay_ms", "v1.logical_clock_ms"]


def _validate_time_observations(record: PositionRecord) -> None:
    if record.commit_time_ms is None or record.replay_time_ms is None:
        raise DiagnosticsError(
            "E_MISSING_TIME",
            location=record.location,
            details={"tx_id": record.tx_id, "source_version": record.source_version},
        )
    if record.source_version == "v2" and record.delay_ms != record.replay_time_ms - record.commit_time_ms:
        raise DiagnosticsError(
            "E_MISSING_TIME",
            location=record.location,
            details={"tx_id": record.tx_id, "reason": "inconsistent absolute observations"},
        )
    if record.source_version == "v1" and record.delay_ms < 0:
        raise DiagnosticsError(
            "E_MISSING_TIME",
            location=record.location,
            details={"tx_id": record.tx_id, "reason": "negative migrated delay"},
        )


def _tx_json(record: PositionRecord, logs_by_tx: dict[str, LogRecord]) -> dict[str, Any]:
    log = logs_by_tx.get(record.tx_id)
    return {
        "tx_id": record.tx_id,
        "tx_num": record.tx_num,
        "source_version": record.source_version,
        "logical_clock": record.logical_clock,
        "replay_generation": record.replay_generation,
        "source_node": record.source_node,
        "replica_node": record.replica_node,
        "committed": record.committed,
        "applied": bool(log.applied) if log else False,
        "observed_in_replay_stream": log is not None,
        "payload_bytes": record.payload_bytes,
        "commit_time_ms": record.commit_time_ms,
        "replay_time_ms": record.replay_time_ms,
        "delay_ms": record.delay_ms,
        "delay_exact": _fraction_text(Fraction(record.delay_ms, 1)),
        "tombstone_applied": record.tombstone_applied,
        "observations": _tx_observations(record),
        "original_time": record.original_time,
        "original_delay": record.original_delay,
        "migration": record.migration,
        "error_upper_bound_ms": _fraction_json(record.error_upper_bound),
        "error_upper_bound_exact": _fraction_text(record.error_upper_bound),
        "error_sources": list(record.error_sources),
        "precision_sensitive": False,
        "sensitive_with": [],
        "precision_comparisons": [],
    }


def _window_json(
    index: int,
    records: list[PositionRecord],
    committed_watermark: str,
) -> dict[str, Any]:
    delays = [Fraction(record.delay_ms, 1) for record in records]
    sorted_delays = sorted(delays)
    p50 = _quantile(sorted_delays, Fraction(1, 2))
    p95 = _quantile(sorted_delays, Fraction(19, 20))
    error_bound = sum((record.error_upper_bound for record in records), Fraction(0, 1))
    quantile_error = max((record.error_upper_bound for record in records), default=Fraction(0, 1))
    sources: set[str] = set()
    for record in records:
        sources.update(record.error_sources)
    return {
        "window_index": index,
        "tx_start": records[0].tx_id,
        "tx_end": records[-1].tx_id,
        "tx_ids": [record.tx_id for record in records],
        "participant_count": len(records),
        "committed_watermark": committed_watermark,
        "quantile_method": "linear_r7_0_based",
        "p50_ms": _fraction_json(p50),
        "p50_exact": _fraction_text(p50),
        "p95_ms": _fraction_json(p95),
        "p95_exact": _fraction_text(p95),
        "min_ms": record_min(delays),
        "max_ms": record_max(delays),
        "error_upper_bound_ms": _fraction_json(error_bound),
        "error_upper_bound_exact": _fraction_text(error_bound),
        "quantile_error_bound_ms": _fraction_json(quantile_error),
        "error_sources": sorted(sources),
        "precision_sensitive": False,
        "sensitive_with": [],
        "precision_comparisons": [],
    }


def record_min(values: list[Fraction]) -> float | int:
    return _fraction_json(min(values))


def record_max(values: list[Fraction]) -> float | int:
    return _fraction_json(max(values))


def _mark_transaction_sensitivity(
    transactions: list[dict[str, Any]],
    tolerance: Fraction,
) -> list[dict[str, Any]]:
    comparisons: list[dict[str, Any]] = []
    for left_index in range(len(transactions)):
        for right_index in range(left_index + 1, len(transactions)):
            left = transactions[left_index]
            right = transactions[right_index]
            difference = abs(Fraction(left["delay_ms"]) - Fraction(right["delay_ms"]))
            if difference < tolerance:
                source_chain = sorted(set(left["error_sources"]) | set(right["error_sources"]))
                comparison = {
                    "scope": "transaction",
                    "left_tx_id": left["tx_id"],
                    "right_tx_id": right["tx_id"],
                    "left_delay_ms": left["delay_ms"],
                    "right_delay_ms": right["delay_ms"],
                    "difference_ms": _fraction_json(difference),
                    "difference_exact": _fraction_text(difference),
                    "tolerance_ms": _fraction_json(tolerance),
                    "reason": "ordering_conclusion_within_tolerance",
                    "error_source_chain": source_chain,
                }
                comparisons.append(comparison)
                left["precision_sensitive"] = True
                right["precision_sensitive"] = True
                left["sensitive_with"].append(right["tx_id"])
                right["sensitive_with"].append(left["tx_id"])
                left["precision_comparisons"].append(
                    {"other_tx_id": right["tx_id"], "difference_ms": comparison["difference_ms"]}
                )
                right["precision_comparisons"].append(
                    {"other_tx_id": left["tx_id"], "difference_ms": comparison["difference_ms"]}
                )
    for transaction in transactions:
        transaction["sensitive_with"] = sorted(set(transaction["sensitive_with"]))
        transaction["precision_comparisons"].sort(key=lambda item: item["other_tx_id"])
    comparisons.sort(key=lambda item: (item["left_tx_id"], item["right_tx_id"]))
    return comparisons


def _mark_window_sensitivity(
    windows: list[dict[str, Any]],
    tolerance: Fraction,
) -> list[dict[str, Any]]:
    comparisons: list[dict[str, Any]] = []
    for left_index in range(len(windows)):
        for right_index in range(left_index + 1, len(windows)):
            left = windows[left_index]
            right = windows[right_index]
            difference = abs(Fraction(left["p50_exact"]) - Fraction(right["p50_exact"]))
            if difference < tolerance:
                source_chain = sorted(set(left["error_sources"]) | set(right["error_sources"]))
                comparison = {
                    "scope": "window",
                    "left_window_index": left["window_index"],
                    "right_window_index": right["window_index"],
                    "left_p50_exact": left["p50_exact"],
                    "right_p50_exact": right["p50_exact"],
                    "difference_exact": _fraction_text(difference),
                    "difference_ms": _fraction_json(difference),
                    "tolerance_ms": _fraction_json(tolerance),
                    "reason": "window_ordering_conclusion_within_tolerance",
                    "error_source_chain": source_chain,
                }
                comparisons.append(comparison)
                left["precision_sensitive"] = True
                right["precision_sensitive"] = True
                left["sensitive_with"].append(right["window_index"])
                right["sensitive_with"].append(left["window_index"])
                left["precision_comparisons"].append(
                    {
                        "other_window_index": right["window_index"],
                        "difference_ms": comparison["difference_ms"],
                    }
                )
                right["precision_comparisons"].append(
                    {
                        "other_window_index": left["window_index"],
                        "difference_ms": comparison["difference_ms"],
                    }
                )
    for window in windows:
        window["sensitive_with"] = sorted(set(window["sensitive_with"]))
        window["precision_comparisons"].sort(
            key=lambda item: item["other_window_index"]
        )
    comparisons.sort(key=lambda item: (item["left_window_index"], item["right_window_index"]))
    return comparisons


def compute_lag(
    merged: list[PositionRecord],
    logs: Iterable[LogRecord],
    config: Config,
    *,
    snapshot_version: int,
    package_version: str,
    logical_clock_floor: int,
) -> dict[str, Any]:
    records = sorted(merged, key=lambda item: item.tx_num)
    logs_by_tx = {log.tx_id: log for log in logs}
    for record in records:
        _validate_time_observations(record)

    transactions = [_tx_json(record, logs_by_tx) for record in records]
    tx_comparisons = _mark_transaction_sensitivity(transactions, config.tolerance)

    committed = [record for record in records if record.committed]
    committed_watermark = committed[-1].tx_id if committed else None
    windows: list[dict[str, Any]] = []
    if committed_watermark is not None:
        for start in range(0, len(committed) - config.window_size + 1, config.window_stride):
            participant = committed[start : start + config.window_size]
            windows.append(_window_json(len(windows), participant, committed_watermark))
    window_comparisons = _mark_window_sensitivity(windows, config.tolerance)

    applied_logs = [log for log in logs_by_tx.values() if log.applied]
    applied_logs.sort(key=lambda item: item.seq)
    replay_watermark = applied_logs[-1].tx_id if applied_logs else None
    unapplied_logs = sorted(
        (log for log in logs_by_tx.values() if not log.applied), key=lambda item: item.seq
    )
    observed_committed_ids = {record.tx_id for record in committed if record.tx_id in logs_by_tx}
    unobserved_committed = [
        record for record in committed if record.tx_id not in observed_committed_ids
    ]
    unobserved_bytes = sum(record.payload_bytes for record in unobserved_committed)
    unapplied_log_bytes = sum(log.payload_bytes for log in unapplied_logs)

    min_tx = min(transactions, key=lambda item: (item["delay_ms"], item["tx_id"]), default=None)
    max_tx = max(transactions, key=lambda item: (item["delay_ms"], item["tx_id"]), default=None)
    ordering_span: Fraction | None = None
    ordering_sensitive = False
    if min_tx is not None and max_tx is not None:
        ordering_span = Fraction(max_tx["delay_ms"] - min_tx["delay_ms"], 1)
        ordering_sensitive = ordering_span < config.tolerance

    precision_sensitive_txs = [
        transaction["tx_id"] for transaction in transactions if transaction["precision_sensitive"]
    ]
    precision_sensitive_windows = [
        window["window_index"] for window in windows if window["precision_sensitive"]
    ]
    migration_count = sum(1 for record in records if record.migration is not None)
    tombstone_count = sum(1 for record in records if record.tombstone_applied)

    conclusions = {
        "min_delay_transaction": None
        if min_tx is None
        else {
            "tx_id": min_tx["tx_id"],
            "delay_ms": min_tx["delay_ms"],
            "observations": min_tx["observations"],
        },
        "max_delay_transaction": None
        if max_tx is None
        else {
            "tx_id": max_tx["tx_id"],
            "delay_ms": max_tx["delay_ms"],
            "observations": max_tx["observations"],
        },
        "ordering_span_ms": None if ordering_span is None else _fraction_json(ordering_span),
        "ordering_span_exact": None if ordering_span is None else _fraction_text(ordering_span),
        "ordering_precision_sensitive": ordering_sensitive,
    }

    output: dict[str, Any] = {
        "ok": True,
        "schema_version": 1,
        "snapshot_version": snapshot_version,
        "package_version": package_version,
        "logical_clock_floor": logical_clock_floor,
        "committed_watermark": committed_watermark,
        "replay_watermark": replay_watermark,
        "lag": {
            "unreplayed_transaction_count": len(unapplied_logs) + len(unobserved_committed),
            "unreplayed_log_bytes": unapplied_log_bytes,
            "unobserved_committed_transaction_count": len(unobserved_committed),
            "unobserved_committed_bytes": unobserved_bytes,
            "unreplayed_total_bytes": unapplied_log_bytes + unobserved_bytes,
            "applied_transaction_count": len(applied_logs),
        },
        "window_config": {
            "size": config.window_size,
            "stride": config.window_stride,
            "quantile_method": "linear_r7_0_based",
            "tolerance_ms": _fraction_json(config.tolerance),
            "tolerance_exact": _fraction_text(config.tolerance),
            "windows_are_full_only": True,
        },
        "transactions": transactions,
        "windows": windows,
        "precision": {
            "tolerance_ms": _fraction_json(config.tolerance),
            "tolerance_exact": _fraction_text(config.tolerance),
            "sensitive_transaction_ids": precision_sensitive_txs,
            "sensitive_window_indexes": precision_sensitive_windows,
            "transaction_comparisons": tx_comparisons,
            "window_comparisons": window_comparisons,
            "conclusions": conclusions,
        },
        "truth_source": {
            "kind": "full_recompute",
            "order": "tx_num_ascending",
            "inputs": ["merged_position_snapshot", "ordered_replay_stream"],
            "incremental_accumulation_used_for_delay_truth": False,
            "missing_time_inference_used": False,
        },
        "counts": {
            "transaction_count": len(records),
            "committed_transaction_count": len(committed),
            "migration_decision_count": migration_count,
            "tombstone_transaction_count": tombstone_count,
            "window_count": len(windows),
        },
    }
    return output
