"""Stable JSON status projection."""

from __future__ import annotations

from typing import Any


def _task_memory(tasks: list[dict[str, Any]]) -> dict[str, int]:
    by_status: dict[str, int] = {}
    for task in tasks:
        by_status[task["status"]] = by_status.get(task["status"], 0) + task[
            "memory_units"
        ]
    return dict(sorted(by_status.items()))


def build_status(store: Any) -> dict[str, Any]:
    with store.read_transaction():
        return _build_status(store)


def _build_status(store: Any) -> dict[str, Any]:
    store.require_initialized()
    watermark = store.watermark()
    current = store.current_snapshot()
    model = store.get_db_model()
    snapshots = sorted(
        store.list_snapshots(), key=lambda snapshot: snapshot["version"]
    )
    windows = store.list_windows()
    aggregates = store.list_aggregates()
    profiles = store.list_profiles()
    snapshots_meta = [
        {
            "version": snapshot["version"],
            "effective_at_ms": snapshot["effective_at_ms"],
            "digest": snapshot["digest"],
            "covered_tables": snapshot["covered_tables"],
        }
        for snapshot in snapshots
    ]
    aggregate_summaries = [
        {
            "template_id": aggregate["template_id"],
            "snapshot_version": aggregate["snapshot_version"],
            "sample_count": aggregate["sample_count"],
            "min_seq": aggregate["min_seq"],
            "max_seq": aggregate["max_seq"],
            "min_clock_ms": aggregate["min_clock_ms"],
            "max_clock_ms": aggregate["max_clock_ms"],
            "window_keys": aggregate["window_keys"],
            "p50": aggregate["p50"],
            "p90": aggregate["p90"],
            "p95": aggregate["p95"],
            "p99": aggregate["p99"],
            "avg_scanned_rows": aggregate["avg_scanned_rows"],
            "avg_returned_rows": aggregate["avg_returned_rows"],
            "aggregate_digest": aggregate["aggregate_digest"],
        }
        for aggregate in aggregates
    ]
    profile_payload = [
        {
            "template_id": profile["template_id"],
            "snapshot_version": profile["snapshot_version"],
            "aggregate_digest": profile["aggregate_digest"],
            "profile_digest": profile["profile_digest"],
            "created_run_id": profile["created_run_id"],
            "result": profile["result"],
        }
        for profile in profiles
    ]
    runs_payload = []
    abandonments = []
    latest = store.latest_run()
    run_rows = store.conn.execute(
        "SELECT * FROM plan_runs ORDER BY run_id"
    ).fetchall()
    for row in run_rows:
        tasks = store.list_tasks(row["run_id"])
        task_stage_counts: dict[str, int] = {}
        for task in tasks:
            task_stage_counts[task["status"]] = (
                task_stage_counts.get(task["status"], 0) + 1
            )
        for task in tasks:
            if task["status"] == "abandoned":
                abandonments.append(
                    {
                        "run_id": task["run_id"],
                        "template_id": task["template_id"],
                        "snapshot_version": task["snapshot_version"],
                        "memory_units": task["memory_units"],
                        "reason": task["abandon_reason"],
                    }
                )
        runs_payload.append(
            {
                "run_id": row["run_id"],
                "max_workers": row["max_workers"],
                "memory_budget_units": row["memory_budget"],
                "memory_charged_units": row["memory_charged"],
                "memory_unit_size": row["memory_unit"],
                "logical_clock_ms": row["logical_clock_ms"],
                "status": row["status"],
                "task_stage_counts": dict(sorted(task_stage_counts.items())),
                "memory_units_by_task_status": _task_memory(tasks),
                "tasks": tasks,
            }
        )
    latest_payload = runs_payload[-1] if runs_payload else None
    latest_memory = (
        None
        if latest_payload is None
        else {
            "budget_units": latest_payload["memory_budget_units"],
            "charged_units": latest_payload["memory_charged_units"],
            "active_units": sum(
                task["memory_units"]
                for task in latest_payload["tasks"]
                if task["status"] == "running"
            ),
            "units_by_task_status": latest_payload[
                "memory_units_by_task_status"
            ],
        }
    )
    invalidations = store.list_invalidation_events()
    return {
        "ok": True,
        "schema_version": 1,
        "database_model": {
            "digest": model["digest"],
            "tables": [
                {
                    "name": table_name,
                    "row_count": table["row_count"],
                    "columns": [
                        column["name"] for column in table["columns"]
                    ],
                    "indexes": [
                        {
                            "name": index_name,
                            "columns": index["columns"],
                            "unique": index["unique"],
                        }
                        for index_name, index in sorted(table["indexes"].items())
                    ],
                }
                for table_name, table in sorted(model["tables"].items())
            ],
        },
        "state": {
            "protocol_version": store.get_meta("protocol_version"),
            "sample_watermark": watermark,
            "logical_clock_watermark_ms": watermark["last_clock_ms"],
        },
        "statistics": {
            "current_version": current["version"],
            "current_effective_at_ms": current["effective_at_ms"],
            "snapshots": snapshots_meta,
        },
        "snapshot_pointer": {
            "version": current["version"],
            "effective_at_ms": current["effective_at_ms"],
            "digest": current["digest"],
        },
        "windows": windows,
        "aggregates": aggregate_summaries,
        "profiles": profile_payload,
        "latest_run_id": None if latest is None else latest["run_id"],
        "latest_run": latest_payload,
        "task_stages": (
            None if latest_payload is None else latest_payload["task_stage_counts"]
        ),
        "memory_occupancy": latest_memory,
        "runs": runs_payload,
        "abandonments": sorted(
            abandonments,
            key=lambda item: (
                item["run_id"],
                item["template_id"],
                item["snapshot_version"],
            ),
        ),
        "budget_abandonments": sorted(
            abandonments,
            key=lambda item: (
                item["run_id"],
                item["template_id"],
                item["snapshot_version"],
            ),
        ),
        "invalidations": invalidations,
    }
