"""Stable, reproducible status projection and invariant verification."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from .canonical import canonical_json, digest_json
from .store import Store


STATUS_FORMAT_VERSION = 1


def _json_or_empty(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_status(store: Store) -> dict[str, Any]:
    versions = store.get_versions()
    current = store.current_version_number()
    precision = store.get_precision()
    scheduler = store.get_scheduler_config()
    execution = store.get_execution_config()
    model = store.get_model()

    windows = []
    for row in store.fetchall(
        "SELECT * FROM windows ORDER BY end_ms, start_ms, replica_id, replica_version"
    ):
        windows.append(
            {
                "window_id": row["window_id"],
                "replica_id": row["replica_id"],
                "replica_version": int(row["replica_version"]),
                "start_ms": int(row["start_ms"]),
                "end_ms": int(row["end_ms"]),
                "status": row["status"],
                "batch_count": int(row["batch_count"]),
                "used_compute_fixed": int(row["used_compute_fixed"]),
                "used_memory_peak_fixed": int(row["used_memory_peak_fixed"]),
                "deferred_count": int(row["deferred_count"]),
                "decision_digest": row["decision_digest"],
                "prepared_clock_ms": row["prepared_clock_ms"],
                "committed_clock_ms": row["committed_clock_ms"],
                "decision": _json_or_empty(row["decision_json"]),
            }
        )

    assignments = [
        {
            "request_seq": int(row["request_seq"]),
            "window_id": row["window_id"],
            "replica_id": row["replica_id"],
            "replica_version": int(row["replica_version"]),
            "start_ms": int(row["start_ms"]),
            "end_ms": int(row["end_ms"]),
            "state": row["state"],
            "assigned_clock_ms": int(row["assigned_clock_ms"]),
        }
        for row in store.fetchall(
            "SELECT * FROM request_assignments ORDER BY request_seq"
        )
    ]

    requests = [
        {
            "seq": int(row["seq"]),
            "arrival_ms": int(row["arrival_ms"]),
            "priority": int(row["priority"]),
            "input_tokens": int(row["input_tokens"]),
            "max_output_tokens": int(row["max_output_tokens"]),
            "deadline_ms": int(row["deadline_ms"]),
            "status": row["status"],
            "replica_id": row["replica_id"],
            "replica_version": row["replica_version"],
            "window_id": row["window_id"],
            "batch_id": row["batch_id"],
            "not_before_ms": int(row["not_before_ms"]),
            "attempts": int(row["attempts"]),
            "failure_count": int(row["failure_count"]),
            "consecutive_failures": int(row["consecutive_failures"]),
            "expiry_count": int(row["expiry_count"]),
            "updated_clock_ms": int(row["updated_clock_ms"]),
        }
        for row in store.fetchall("SELECT * FROM requests ORDER BY seq")
    ]

    batches = [
        {
            "batch_id": row["batch_id"],
            "window_id": row["window_id"],
            "ordinal": int(row["ordinal"]),
            "replica_id": row["replica_id"],
            "replica_version": int(row["replica_version"]),
            "status": row["status"],
            "request_count": int(row["request_count"]),
            "compute_raw": float(row["compute_raw"]),
            "memory_raw": float(row["memory_raw"]),
            "latency_raw": float(row["latency_raw"]),
            "queue_raw": float(row["latency_raw"]),
            "compute_fixed": int(row["compute_fixed"]),
            "memory_fixed": int(row["memory_fixed"]),
            "latency_fixed": int(row["latency_fixed"]),
            "queue_fixed": int(row["latency_fixed"]),
            "compute_error_bound": float(row["compute_error_bound"]),
            "memory_error_bound": float(row["memory_error_bound"]),
            "latency_error_bound": float(row["latency_error_bound"]),
            "queue_error_bound": float(row["latency_error_bound"]),
            "first_constraint": row["first_constraint"],
            "precision_sensitive": bool(row["precision_sensitive"]),
            "decision": _json_or_empty(row["decision_json"]),
            "prepared_clock_ms": int(row["prepared_clock_ms"]),
            "committed_clock_ms": row["committed_clock_ms"],
            "execution_status": row["execution_status"],
            "started_clock_ms": row["started_clock_ms"],
            "finished_clock_ms": row["finished_clock_ms"],
            "failure_reason": row["failure_reason"],
        }
        for row in store.fetchall(
            "SELECT * FROM batches ORDER BY window_id, ordinal, batch_id"
        )
    ]

    error_ledger = [
        {
            "op_id": row["op_id"],
            "batch_id": row["batch_id"],
            "metric": row["metric"],
            "ordinal": int(row["ordinal"]),
            "operation": row["operation"],
            "operand_a_raw": row["operand_a_raw"],
            "operand_b_raw": row["operand_b_raw"],
            "result_raw": float(row["result_raw"]),
            "scale": row["scale"],
            "fixed_result": row["fixed_result"],
            "rounding": row["rounding"],
            "propagated_error_bound": float(row["propagated_error_bound"]),
            "cumulative_error_bound": float(row["cumulative_error_bound"]),
            "details": _json_or_empty(row["details_json"]),
        }
        for row in store.fetchall(
            "SELECT * FROM error_operations ORDER BY batch_id, metric, ordinal"
        )
    ]

    precision_flags = [
        {
            "flag_id": row["flag_id"],
            "window_id": row["window_id"],
            "kind": row["kind"],
            "subject_a": row["subject_a"],
            "subject_b": row["subject_b"],
            "metric": row["metric"],
            "raw_a": float(row["raw_a"]),
            "raw_b": float(row["raw_b"]),
            "difference": float(row["difference"]),
            "tolerance": float(row["tolerance"]),
            "depended": bool(row["depended"]),
            "reason": row["reason"],
        }
        for row in store.fetchall(
            "SELECT * FROM precision_flags ORDER BY kind, window_id, flag_id"
        )
    ]

    replica_runtime = [
        {
            "replica_id": row["replica_id"],
            "replica_version": int(row["replica_version"]),
            "consecutive_failures": int(row["consecutive_failures"]),
            "last_batch_id": row["last_batch_id"],
        }
        for row in store.fetchall(
            "SELECT * FROM replica_runtime ORDER BY replica_id, replica_version"
        )
    ]

    lifecycle_events = [
        {
            "event_key": row["event_key"],
            "request_seq": int(row["request_seq"]),
            "from_status": row["from_status"],
            "to_status": row["to_status"],
            "reason": row["reason"],
            "clock_ms": int(row["clock_ms"]),
            "window_id": row["window_id"],
            "batch_id": row["batch_id"],
        }
        for row in store.fetchall(
            "SELECT * FROM lifecycle_events ORDER BY request_seq, clock_ms, event_key"
        )
    ]

    watermark: dict[str, int] = {}
    for row in store.fetchall(
        """
        SELECT replica_id, MAX(end_ms) AS watermark
        FROM windows WHERE status = 'committed'
        GROUP BY replica_id
        """
    ):
        watermark[str(row["replica_id"])] = int(row["watermark"])

    request_status_counts = Counter(item["status"] for item in requests)
    batch_exec_counts = Counter(item["execution_status"] for item in batches)
    window_status_counts = Counter(item["status"] for item in windows)
    stats = {
        "requests_total": len(requests),
        "request_status_counts": dict(sorted(request_status_counts.items())),
        "expired_requests": sum(1 for item in requests if item["status"] == "EXPIRED"),
        "execution_failures": batch_exec_counts.get("failed", 0),
        "pending_requests": request_status_counts.get("PENDING", 0),
        "batches_total": len(batches),
        "batch_execution_counts": dict(sorted(batch_exec_counts.items())),
        "window_status_counts": dict(sorted(window_status_counts.items())),
        "precision_sensitive_flags": len(precision_flags),
        "error_operations": len(error_ledger),
    }

    payload: dict[str, Any] = {
        "status_format_version": STATUS_FORMAT_VERSION,
        "workflow_phase": store.get_meta("workflow_phase"),
        "logical_clock_ms": store.logical_clock(),
        "current_replica_version": current,
        "model": model.to_dict(),
        "precision": precision.to_dict(),
        "scheduler": scheduler.to_dict(),
        "execution": execution.to_dict(),
        "replica_versions": [
            {
                **version.to_dict(),
                "current": current == version.version,
            }
            for version in versions
        ],
        "window_watermark_by_replica": dict(sorted(watermark.items())),
        "windows": windows,
        "assignments": assignments,
        "requests": requests,
        "batches": batches,
        "error_ledger": error_ledger,
        "precision_sensitive": precision_flags,
        "replica_runtime": replica_runtime,
        "lifecycle_events": lifecycle_events,
        "stats": stats,
    }
    payload["state_digest"] = digest_json(payload)
    return payload


def status_json(store: Store, *, indent: int | None = 2) -> str:
    payload = build_status(store)
    if indent is None:
        return canonical_json(payload)
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        indent=indent,
        sort_keys=True,
    )


def verify_store(store: Store) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    def check(name: str, ok: bool, details: Any = None) -> None:
        checks.append({"name": name, "ok": bool(ok)})
        if not ok:
            errors.append({"check": name, "details": details})

    integrity = store.integrity_check()
    check("sqlite_integrity", integrity == "ok", {"result": integrity})
    journal_mode = str(store.fetchone("PRAGMA journal_mode")[0]).lower()
    check("wal_mode", journal_mode == "wal", {"journal_mode": journal_mode})

    duplicate_members = store.fetchall(
        """
        SELECT request_seq, COUNT(DISTINCT batch_id) AS count
        FROM batch_members WHERE state = 'committed'
        GROUP BY request_seq HAVING count > 1
        """
    )
    check("unique_request_batch_membership", not duplicate_members, [dict(r) for r in duplicate_members])

    invalid_statuses = store.fetchall(
        """
        SELECT seq, status FROM requests
        WHERE status NOT IN ('PENDING', 'BATCHED', 'RUNNING', 'COMPLETED', 'EXPIRED')
        """
    )
    check("request_status_domain", not invalid_statuses, [dict(r) for r in invalid_statuses])

    mixed_versions = store.fetchall(
        """
        SELECT bm.request_seq
        FROM batch_members bm
        JOIN batches b ON b.batch_id = bm.batch_id
        JOIN requests r ON r.seq = bm.request_seq
        WHERE bm.state = 'committed'
          AND r.replica_version IS NOT NULL
          AND r.replica_version != bm.replica_version
        """
    )
    check("request_batch_version_consistent", not mixed_versions, [dict(r) for r in mixed_versions])

    bad_requests = store.fetchall(
        """
        SELECT r.seq, r.status, r.batch_id, b.batch_id AS joined_batch,
               b.status AS batch_status
        FROM requests r LEFT JOIN batches b ON b.batch_id = r.batch_id
        WHERE (r.status = 'BATCHED' AND (r.batch_id IS NULL OR b.batch_id IS NULL
               OR b.status != 'committed'))
           OR (r.batch_id IS NOT NULL AND b.batch_id IS NULL)
        """
    )
    check("request_batch_references", not bad_requests, [dict(r) for r in bad_requests])

    missing_membership = store.fetchall(
        """
        SELECT r.seq, r.batch_id
        FROM requests r
        WHERE r.batch_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM batch_members bm
            WHERE bm.request_seq = r.seq AND bm.batch_id = r.batch_id
          )
        """
    )
    check("request_membership_evidence", not missing_membership, [dict(r) for r in missing_membership])

    bad_member_states = store.fetchall(
        """
        SELECT bm.batch_id, bm.request_seq, bm.state, b.status AS batch_status
        FROM batch_members bm
        JOIN batches b ON b.batch_id = bm.batch_id
        WHERE (b.status = 'committed' AND bm.state != 'committed')
           OR (b.status = 'proposed' AND bm.state != 'proposed')
        """
    )
    check("batch_member_state", not bad_member_states, [dict(r) for r in bad_member_states])

    invalid_execution = store.fetchall(
        """
        SELECT batch_id, execution_status FROM batches
        WHERE execution_status NOT IN (
            'pending', 'running', 'completed', 'completed_with_expiry', 'failed'
        )
        """
    )
    check("batch_execution_status_domain", not invalid_execution, [dict(r) for r in invalid_execution])

    windows_after_clock = store.fetchall(
        """
        SELECT window_id, end_ms FROM windows
        WHERE status = 'committed' AND end_ms > ?
        """,
        (store.logical_clock(),),
    )
    check("committed_window_clock", not windows_after_clock, [dict(r) for r in windows_after_clock])

    binding_errors: list[dict[str, Any]] = []
    for row in store.fetchall(
        "SELECT * FROM windows ORDER BY end_ms, start_ms, replica_id, replica_version"
    ):
        version = store.get_version(int(row["replica_version"]))
        replica = version.by_id().get(str(row["replica_id"]))
        bound = store.version_at(int(row["start_ms"]))
        if replica is None:
            binding_errors.append(
                {
                    "window_id": row["window_id"],
                    "error": "replica_missing_from_bound_version",
                }
            )
            continue
        if bound.version != version.version:
            binding_errors.append(
                {
                    "window_id": row["window_id"],
                    "error": "version_pointer_mismatch",
                    "expected": version.version,
                    "actual": bound.version,
                }
            )
        if int(row["end_ms"]) - int(row["start_ms"]) != replica.batch_window_ms:
            binding_errors.append(
                {
                    "window_id": row["window_id"],
                    "error": "window_duration_mismatch",
                }
            )
        if int(row["start_ms"]) % replica.batch_window_ms != 0:
            binding_errors.append(
                {
                    "window_id": row["window_id"],
                    "error": "window_not_aligned",
                }
            )
    check("window_version_binding", not binding_errors, binding_errors)

    bad_resources = store.fetchall(
        """
        SELECT w.window_id, w.used_compute_fixed,
               COALESCE(SUM(rl.compute_fixed), 0) AS actual_compute,
               w.used_memory_peak_fixed,
               COALESCE(MAX(rl.memory_fixed), 0) AS actual_memory
        FROM windows w
        LEFT JOIN resource_ledger rl ON rl.window_id = w.window_id
        WHERE w.status = 'committed'
        GROUP BY w.window_id
        HAVING w.used_compute_fixed != actual_compute
            OR w.used_memory_peak_fixed != actual_memory
        """
    )
    check("window_resource_ledger", not bad_resources, [dict(r) for r in bad_resources])

    missing_ledger = store.fetchall(
        """
        SELECT b.batch_id
        FROM batches b
        WHERE b.status = 'committed'
          AND (
            SELECT COUNT(DISTINCT eo.metric) FROM error_operations eo
            WHERE eo.batch_id = b.batch_id
          ) != 3
        """
    )
    check("batch_error_ledger_complete", not missing_ledger, [dict(r) for r in missing_ledger])

    bad_flags = store.fetchall(
        """
        SELECT flag_id, difference, tolerance FROM precision_flags
        WHERE difference >= tolerance OR depended != 1
        """
    )
    check("precision_flags_within_tolerance", not bad_flags, [dict(r) for r in bad_flags])

    committed_without_decision = store.fetchall(
        """
        SELECT window_id FROM windows
        WHERE status = 'committed' AND (decision_digest IS NULL OR decision_json IS NULL)
        """
    )
    check("committed_windows_have_decisions", not committed_without_decision, [dict(r) for r in committed_without_decision])

    status_a = build_status(store)
    status_b = build_status(store)
    check("status_repeatable", status_a["state_digest"] == status_b["state_digest"])

    return {
        "ok": not errors,
        "checks": checks,
        "errors": errors,
        "state_digest": status_a["state_digest"],
        "logical_clock_ms": status_a["logical_clock_ms"],
    }
