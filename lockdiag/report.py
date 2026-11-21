"""Stable machine-readable reports and status projections."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from .clock import sample_by_id
from .store import StateStore
from .util import format_micro, format_units, pretty_json, tolerance_to_units


def _sample_projection(sample: dict[str, Any] | None) -> dict[str, Any] | None:
    if sample is None:
        return None
    return {
        "sample_id": sample["sample_id"],
        "sequence_index": sample["sequence_index"],
        "raw_value": sample["raw_value"],
        "purpose": sample.get("purpose"),
    }


def _config_projection(connection) -> dict[str, Any]:
    import json as _json

    row = connection.execute("SELECT manifest_json, fingerprint FROM run_info WHERE id=1").fetchone()
    manifest = _json.loads(row["manifest_json"])
    config = manifest["config"]
    clock = manifest["clock"]
    scale = int(config["fixed_scale"])
    return {
        "fingerprint": row["fingerprint"],
        "max_concurrent_transactions": int(config["max_concurrent_transactions"]),
        "worker_processes": int(config.get("worker_processes", 2)),
        "default_lock_wait_budget_ms": int(config["default_lock_wait_budget_ms"]),
        "deadlock_check_period": int(config.get("deadlock_check_period", 1)),
        "fixed_scale": scale,
        "wait_tolerance_ms": str(config["wait_tolerance_ms"]),
        "wait_tolerance_units": max(
            1, tolerance_to_units(Decimal(str(config["wait_tolerance_ms"])), scale)
        ),
        "clock": {
            "raw_units_per_ms": int(clock["raw_units_per_ms"]),
            "step_units": int(clock["step_units"]),
            "injected_sample_count": len(clock["samples"]),
        },
    }


def build_report(store: StateStore) -> dict[str, Any]:
    connection = store.open()
    config = _config_projection(connection)
    scale = int(config["fixed_scale"])
    run_info = connection.execute("SELECT * FROM run_info WHERE id=1").fetchone()
    schedule = connection.execute("SELECT * FROM schedule_state WHERE id=1").fetchone()
    clock_state = connection.execute("SELECT * FROM clock_state WHERE id=1").fetchone()

    transactions = []
    for row in connection.execute(
        "SELECT * FROM transactions ORDER BY start_order, tx_id"
    ):
        transactions.append(
            {
                "tx_id": row["tx_id"],
                "start_order": row["start_order"],
                "version": row["version"],
                "isolation": row["isolation"],
                "budget_ms": row["budget_ms"],
                "status": row["status"],
                "started_sample": row["started_sample"],
                "finished_sample": row["finished_sample"],
                "wait_count": row["wait_count"],
                "wait_total_units": row["wait_total_units"],
                "wait_total_ms": format_units(int(row["wait_total_units"]), scale),
                "actual_rounding_error_micro_ms": row["wait_actual_error_micro"],
                "actual_rounding_error_ms": format_micro(int(row["wait_actual_error_micro"])),
                "error_bound_micro_ms": row["wait_error_micro"],
                "error_bound_ms": format_micro(int(row["wait_error_micro"])),
                "end_reason": row["end_reason"],
            }
        )

    wait_ledger = []
    for row in connection.execute(
        """
        SELECT w.*, q.queue_no
        FROM waits w
        LEFT JOIN wait_queue q ON q.wait_id=w.wait_id
        ORDER BY w.tx_id, w.op_seq, w.wait_id
        """
    ):
        enqueue_sample = connection.execute(
            "SELECT * FROM clock_samples WHERE sample_id=?", (row["enqueue_sample"],)
        ).fetchone()
        release_sample = (
            connection.execute(
                "SELECT * FROM clock_samples WHERE sample_id=?", (row["resolve_sample"],)
            ).fetchone()
            if row["resolve_sample"] is not None
            else None
        )
        item: dict[str, Any] = {
            "wait_id": row["wait_id"],
            "queue_no": row["queue_no"],
            "tx_id": row["tx_id"],
            "op_seq": row["op_seq"],
            "resource_key": row["resource_key"],
            "holder_tx": row["holder_tx"],
            "holder_txs": json.loads(row["holder_txs_json"]),
            "index_path": row["index_path"],
            "status": row["status"],
            "resolution": row["resolution"],
            "enqueue": _sample_projection(dict(enqueue_sample) if enqueue_sample else None),
            "release": _sample_projection(dict(release_sample) if release_sample else None),
        }
        if row["status"] == "RESOLVED":
            item["duration"] = {
                "raw_delta": row["raw_delta"],
                "scale": row["scale"],
                "raw_units_per_ms": row["raw_units_per_ms"],
                "rounded_units": row["rounded_units"],
                "rounded_ms": format_units(int(row["rounded_units"]), int(row["scale"])),
                "exact_numerator": row["exact_numerator"],
                "exact_denominator": row["exact_denominator"],
                "rounding_method": "HALF_UP",
                "rounding_direction": row["rounding_direction"],
                "residual_numerator": row["residual_numerator"],
                "actual_residual_micro_ms": row["actual_residual_micro"],
                "actual_residual_ms": format_micro(int(row["actual_residual_micro"])),
                "error_bound_micro_ms": row["error_bound_micro"],
                "error_bound_ms": format_micro(int(row["error_bound_micro"])),
            }
        else:
            item["duration"] = None
        wait_ledger.append(item)

    propagation = []
    for row in connection.execute(
        "SELECT * FROM tx_error_chains ORDER BY tx_id, ordinal"
    ):
        propagation.append(
            {
                "tx_id": row["tx_id"],
                "ordinal": row["ordinal"],
                "wait_id": row["wait_id"],
                "resource_key": row["resource_key"],
                "holders": json.loads(row["holders_json"]),
                "enqueue_sample_id": row["enqueue_sample"],
                "release_sample_id": row["resolve_sample"],
                "error_bound_micro_ms": row["error_bound_micro"],
                "error_bound_ms": format_micro(int(row["error_bound_micro"])),
                "source_chain": json.loads(row["source_json"]),
            }
        )

    attribution = []
    for row in connection.execute(
        """
        SELECT * FROM attribution
        ORDER BY holder_tx, waiter_tx, index_path
        """
    ):
        attribution.append(
            {
                "holder_tx": row["holder_tx"],
                "waiter_tx": row["waiter_tx"],
                "index_path": row["index_path"],
                "wait_count": row["wait_count"],
                "total_units": row["total_units"],
                "total_ms": format_units(int(row["total_units"]), scale),
                "actual_error_micro_ms": row["actual_error_micro"],
                "error_bound_micro_ms": row["error_bound_micro"],
                "error_bound_ms": format_micro(int(row["error_bound_micro"])),
            }
        )

    precision = []
    for row in connection.execute(
        "SELECT * FROM precision_flags ORDER BY reason, tx_a, tx_b"
    ):
        details = json.loads(row["details_json"])
        precision.append(
            {
                "reason": row["reason"],
                "tx_a": row["tx_a"],
                "tx_b": row["tx_b"],
                "difference_units": row["difference_units"],
                "difference_ms": details.get("difference_ms"),
                "tolerance_units": row["tolerance_units"],
                "tolerance_ms": details.get("tolerance_ms"),
                "first_divergent_sample_id": row["first_divergent_sample"],
                "intermediates": {
                    row["tx_a"]: details.get("tx_a"),
                    row["tx_b"]: details.get("tx_b"),
                },
                "explanation": details.get("explanation"),
            }
        )

    deadlocks = []
    for row in connection.execute("SELECT * FROM deadlocks ORDER BY deadlock_id"):
        deadlocks.append(
            {
                "deadlock_id": row["deadlock_id"],
                "detected_sample_id": row["detected_sample"],
                "cycle": json.loads(row["cycle_json"]),
                "victim_tx": row["victim_tx"],
                "victim_start_order": row["victim_start_order"],
                "basis": row["basis"],
                "released_wait_id": row["released_wait_id"],
                "continued_transactions": json.loads(row["continued_json"]),
                "queue_after_resolution": json.loads(row["queue_json"]),
                "locks_after_resolution": json.loads(row["locks_json"]),
                "queue_revalidated": bool(row["revalidated"]),
                "details": json.loads(row["details_json"]),
            }
        )

    index_hits = []
    for row in connection.execute(
        """
        SELECT tx_id, op_seq, op_id, kind, index_path, plan_json
        FROM operations
        WHERE index_path IS NOT NULL
        ORDER BY tx_id, op_seq
        """
    ):
        plan = json.loads(row["plan_json"]) if row["plan_json"] else {}
        index_hits.append(
            {
                "tx_id": row["tx_id"],
                "op_seq": row["op_seq"],
                "op_id": row["op_id"],
                "kind": row["kind"],
                "index_path": row["index_path"],
                "examined_rows_preview": len(plan.get("rows", [])),
            }
        )

    queue = [
        {
            "queue_no": row["queue_no"],
            "wait_id": row["wait_id"],
            "tx_id": row["tx_id"],
            "resource_key": row["resource_key"],
            "requested_mode": row["requested_mode"],
            "status": row["status"],
            "resolution": row["resolution"],
        }
        for row in connection.execute("SELECT * FROM wait_queue ORDER BY queue_no")
    ]
    locks = [
        {
            "resource_key": row["resource_key"],
            "table_name": row["table_name"],
            "mode": row["mode"],
            "tx_id": row["tx_id"],
            "acquired_sample": row["acquired_sample"],
        }
        for row in connection.execute(
            "SELECT * FROM lock_holders ORDER BY table_name, resource_key, tx_id"
        )
    ]
    status_counts: dict[str, int] = {}
    for tx in transactions:
        status_counts[tx["status"]] = status_counts.get(tx["status"], 0) + 1
    report = {
        "format": "lockdiag-report",
        "format_version": "1.0",
        "run_status": run_info["status"],
        "scheduler": {
            "steps": schedule["steps"],
            "status": schedule["status"],
            "rr_start_order": schedule["rr_start_order"],
        },
        "logical_clock": {
            **config["clock"],
            "next_sequence_index": clock_state["next_sequence"],
            "watermark_sample_id": clock_state["last_sample_id"],
            "watermark_raw_value": clock_state["last_raw"],
        },
        "config": config,
        "transactions": transactions,
        "wait_ledger": wait_ledger,
        "error_propagation": propagation,
        "index_attribution": attribution,
        "index_hits": index_hits,
        "precision_sensitive": precision,
        "deadlocks": deadlocks,
        "final_queue": queue,
        "final_locks": locks,
        "summary": {
            "transaction_count": len(transactions),
            "status_counts": dict(sorted(status_counts.items())),
            "wait_count": len(wait_ledger),
            "resolved_wait_count": sum(
                1 for wait in wait_ledger if wait["status"] == "RESOLVED"
            ),
            "wait_total_units": sum(int(tx["wait_total_units"]) for tx in transactions),
            "wait_total_ms": format_units(
                sum(int(tx["wait_total_units"]) for tx in transactions), scale
            ),
            "error_bound_micro_ms": sum(
                int(tx["error_bound_micro_ms"]) for tx in transactions
            ),
            "error_bound_ms": format_micro(
                sum(int(tx["error_bound_micro_ms"]) for tx in transactions)
            ),
            "attribution_triples": len(attribution),
            "precision_sensitive_count": len(precision),
            "deadlock_count": len(deadlocks),
            "index_paths": sorted({item["index_path"] for item in index_hits}),
        },
    }
    return report


def report_json(store: StateStore) -> str:
    return pretty_json(build_report(store))
