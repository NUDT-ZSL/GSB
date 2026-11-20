from __future__ import annotations

from collections import defaultdict
from typing import Any

from .canonical import strict_json_loads
from .models import Plan
from .storage import Store


def status_snapshot(store: Store, plan: Plan) -> dict[str, Any]:
    with store.transaction(immediate=False):
        return _status_snapshot(store, plan)


def _status_snapshot(store: Store, plan: Plan) -> dict[str, Any]:
    mode = plan.config.numeric_mode
    batch_rows = store.conn.execute(
        "SELECT * FROM batches WHERE mode = ? ORDER BY batch_seq",
        (mode,),
    ).fetchall()
    group_rows = store.conn.execute(
        "SELECT * FROM group_states WHERE mode = ? ORDER BY group_key_json, group_hash",
        (mode,),
    ).fetchall()
    failure_rows = store.conn.execute(
        "SELECT * FROM group_failures WHERE mode = ? ORDER BY group_key_json, group_hash",
        (mode,),
    ).fetchall()
    failure_by_group = {str(row["group_hash"]): row for row in failure_rows}

    event_rows = store.conn.execute(
        """
        SELECT batch_seq, group_hash, direction, bytes, start_clock, end_clock,
               duration_ticks, reused
        FROM spill_events
        WHERE mode = ?
        ORDER BY start_clock, direction, group_hash, batch_seq
        """,
        (mode,),
    ).fetchall()
    events_by_group: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: {
            "spill": {"rounds": 0, "bytes": 0, "ticks": 0, "reused": 0},
            "readback": {"rounds": 0, "bytes": 0, "ticks": 0, "reused": 0},
        }
    )
    totals = {
        "spill": {"rounds": 0, "groups": 0, "bytes": 0, "ticks": 0, "reused": 0},
        "readback": {"rounds": 0, "groups": 0, "bytes": 0, "ticks": 0, "reused": 0},
    }
    spill_groups: dict[str, set[str]] = {"spill": set(), "readback": set()}
    rounds: list[dict[str, Any]] = []
    for row in event_rows:
        direction = str(row["direction"])
        if direction not in totals:
            continue
        group_hash_value = str(row["group_hash"])
        bytes_value = int(row["bytes"])
        ticks = int(row["duration_ticks"])
        reused = int(row["reused"])
        events_by_group[group_hash_value][direction]["rounds"] += 1
        events_by_group[group_hash_value][direction]["bytes"] += bytes_value
        events_by_group[group_hash_value][direction]["ticks"] += ticks
        events_by_group[group_hash_value][direction]["reused"] += reused
        totals[direction]["rounds"] += 1
        totals[direction]["bytes"] += bytes_value
        totals[direction]["ticks"] += ticks
        totals[direction]["reused"] += reused
        spill_groups[direction].add(group_hash_value)
        rounds.append(
            {
                "batch_seq": int(row["batch_seq"]),
                "group_hash": group_hash_value,
                "direction": direction,
                "bytes": bytes_value,
                "start_clock": int(row["start_clock"]),
                "end_clock": int(row["end_clock"]),
                "duration_ticks": ticks,
                "reused": bool(reused),
            }
        )
    for direction in totals:
        totals[direction]["groups"] = len(spill_groups[direction])

    groups: list[dict[str, Any]] = []
    seen_groups: set[str] = set()
    for row in group_rows:
        group_hash_value = str(row["group_hash"])
        seen_groups.add(group_hash_value)
        failure = failure_by_group.get(group_hash_value)
        groups.append(
            {
                "group": strict_json_loads(row["group_key_json"]),
                "group_hash": group_hash_value,
                "version": int(row["version"]),
                "status": "failed" if failure is not None else str(row["status"]),
                "location": (
                    {"type": "spill", "spill_id": row["spill_id"]}
                    if row["spill_id"] is not None
                    else {"type": "sqlite_state"}
                ),
                "state_digest": row["state_digest"],
                "last_touch": int(row["last_touch"]),
                "spill": events_by_group[group_hash_value],
                "failure": _failure_payload(failure) if failure is not None else None,
            }
        )
    for row in failure_rows:
        group_hash_value = str(row["group_hash"])
        if group_hash_value in seen_groups:
            continue
        groups.append(
            {
                "group": strict_json_loads(row["group_key_json"]),
                "group_hash": group_hash_value,
                "version": 0,
                "status": "failed",
                "location": {"type": "none"},
                "state_digest": None,
                "last_touch": 0,
                "spill": events_by_group[group_hash_value],
                "failure": _failure_payload(row),
            }
        )
    groups.sort(key=lambda item: (str(item["group"]), item["group_hash"]))

    spills = store.conn.execute(
        """
        SELECT spill_id, group_hash, group_version, rel_path, sha256, size_bytes,
               created_clock, status
        FROM spill_files WHERE mode = ? ORDER BY group_hash, group_version, spill_id
        """,
        (mode,),
    ).fetchall()
    failures = store.conn.execute(
        """
        SELECT batch_seq, phase, error_code, column_name, group_hash,
               group_key_json, details_json, logical_time
        FROM failures WHERE mode = ? ORDER BY logical_time, batch_seq, error_code, group_hash
        """,
        (mode,),
    ).fetchall()
    audit = store.conn.execute(
        """
        SELECT COUNT(*) AS conversions,
               SUM(CASE WHEN direction = 'up' THEN 1 ELSE 0 END) AS rounded_up,
               SUM(CASE WHEN direction = 'down' THEN 1 ELSE 0 END) AS rounded_down,
               SUM(CASE WHEN nonfinite = 'nan' THEN 1 ELSE 0 END) AS nan,
               SUM(CASE WHEN nonfinite IN ('positive_infinity', 'negative_infinity')
                        THEN 1 ELSE 0 END) AS infinities
        FROM conversion_audit WHERE mode = ?
        """,
        (mode,),
    ).fetchone()
    result_count = store.conn.execute(
        "SELECT COUNT(*) AS n FROM results WHERE mode = ?",
        (mode,),
    ).fetchone()

    return {
        "schema_version": int(store.get_meta("schema_version", "1") or 1),
        "mode": mode,
        "plan": {
            "query_id": plan.query.query_id,
            "plan_digest": plan.plan_digest,
            "memory_budget_bytes": plan.config.memory_budget_bytes,
            "numeric_mode": mode,
            "fixed_scale": plan.config.fixed_scale,
            "fixed_int_bits": plan.config.fixed_int_bits,
            "float_tolerance": plan.config.float_tolerance,
            "logical_clock_step": plan.config.logical_clock_step,
        },
        "clock": store.clock(),
        "watermark": int(store.get_meta("watermark", "0") or 0),
        "peak_resident_bytes": int(store.get_meta("peak_resident_bytes", "0") or 0),
        "state_digest": store.state_digest(),
        "progress": {
            "loaded": len(batch_rows),
            "pending": sum(1 for row in batch_rows if row["status"] == "pending"),
            "committed": sum(1 for row in batch_rows if row["status"] == "committed"),
            "failed": sum(1 for row in batch_rows if row["status"] == "failed"),
            "results": int(result_count["n"]),
        },
        "batches": [
            {
                "batch_seq": int(row["batch_seq"]),
                "row_count": int(row["row_count"]),
                "digest": row["batch_digest"],
                "status": row["status"],
                "result_digest": row["result_digest"],
                "error_code": row["error_code"],
                "committed_clock": row["committed_clock"],
            }
            for row in batch_rows
        ],
        "groups": groups,
        "spill": {
            "totals": totals,
            "rounds": rounds,
            "files": [
                {
                    "spill_id": row["spill_id"],
                    "group_hash": row["group_hash"],
                    "group_version": int(row["group_version"]),
                    "path": row["rel_path"],
                    "sha256": row["sha256"],
                    "size_bytes": int(row["size_bytes"]),
                    "created_clock": int(row["created_clock"]),
                    "status": row["status"],
                }
                for row in spills
            ],
        },
        "conversion_audit": {
            "conversions": int(audit["conversions"] or 0),
            "rounded_up": int(audit["rounded_up"] or 0),
            "rounded_down": int(audit["rounded_down"] or 0),
            "nan": int(audit["nan"] or 0),
            "infinities": int(audit["infinities"] or 0),
        },
        "failures": [_failure_payload(row) for row in failures],
    }


def _failure_payload(row: Any) -> dict[str, Any]:
    return {
        "batch_seq": int(row["batch_seq"]),
        "phase": row["phase"],
        "error_code": row["error_code"],
        "column": row["column_name"] if "column_name" in row.keys() else None,
        "group_hash": row["group_hash"],
        "group": (
            strict_json_loads(row["group_key_json"])
            if row["group_key_json"] is not None
            else None
        ),
        "details": strict_json_loads(row["details_json"]),
        "logical_time": int(row["logical_time"]),
    }
