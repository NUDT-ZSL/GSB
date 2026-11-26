from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .errors import Code, KvError
from .ledger import read_frame
from .storage import Database
from .util import canonical_hash


def build_status(db: Database) -> Dict[str, Any]:
    config = db.conn.execute("SELECT * FROM config WHERE id=1").fetchone()
    if config is None:
        raise KvError(Code.STATE_NOT_INITIALIZED, "workspace has no loaded state")

    active_policy = db.get_meta("active_policy_version")
    clock_watermark = int(db.get_meta("logical_clock_watermark_ms", "0"))
    unfinished = db.conn.execute(
        "SELECT COUNT(*) n FROM eviction_attempts WHERE status IN ('planned','writing')"
    ).fetchone()["n"]
    recovery_verdict = "recovery_required" if unfinished else "consistent"

    layer_rows = db.conn.execute("SELECT * FROM layer_state ORDER BY layer_id ASC").fetchall()
    layers: List[Dict[str, Any]] = []
    for row in layer_rows:
        used = int(row["hbm_used_bytes"])
        capacity = int(row["capacity_bytes"])
        layers.append(
            {
                "layer_id": row["layer_id"],
                "capacity_bytes": capacity,
                "hbm_used_bytes": used,
                "evicted_bytes": int(row["evicted_bytes"]),
                "free_in_layer_bytes": max(0, capacity - used),
                "over_capacity_bytes": max(0, used - capacity),
                "within_capacity": used <= capacity,
                "utilization": used / capacity if capacity else None,
            }
        )

    request_rows = db.conn.execute("SELECT * FROM requests ORDER BY seq_id ASC").fetchall()
    requests: List[Dict[str, Any]] = []
    for req in request_rows:
        layer_ledger_rows = db.conn.execute(
            "SELECT * FROM request_layers WHERE seq_id=? ORDER BY layer_id ASC",
            (req["seq_id"],),
        ).fetchall()
        req_layers = []
        hbm_total = 0
        evicted_total = 0
        fixed_total = 0
        remainder_total = 0
        error_bound_total = 0
        for item in layer_ledger_rows:
            hbm_total += int(item["hbm_used_bytes"])
            evicted_total += int(item["evicted_bytes"])
            fixed_total += int(item["fixed_units_cumulative"])
            remainder_total += int(item["carry_bytes"])
            error_bound_total += int(item["error_bound_bytes"])
            req_layers.append(
                {
                    "layer_id": item["layer_id"],
                    "total_bytes": int(item["total_bytes"]),
                    "hbm_used_bytes": int(item["hbm_used_bytes"]),
                    "evicted_bytes": int(item["evicted_bytes"]),
                    "fixed_units_cumulative": int(item["fixed_units_cumulative"]),
                    "remainder_bytes": int(item["carry_bytes"]),
                    "error_upper_bound_bytes": int(item["error_bound_bytes"]),
                }
            )
        pending = db.conn.execute(
            """
            SELECT
              SUM(CASE WHEN status='planned' THEN 1 ELSE 0 END) planned,
              SUM(CASE WHEN status='writing' THEN 1 ELSE 0 END) writing,
              SUM(CASE WHEN status='committed' THEN 1 ELSE 0 END) committed,
              SUM(CASE WHEN status='abandoned' THEN 1 ELSE 0 END) abandoned,
              SUM(CASE WHEN status='failed_started' THEN 1 ELSE 0 END) failed
            FROM eviction_attempts WHERE seq_id=?
            """,
            (req["seq_id"],),
        ).fetchone()
        requests.append(
            {
                "seq_id": req["seq_id"],
                "priority": req["priority"],
                "generation": req["request_generation"],
                "status": req["status"],
                "watermark_token": req["watermark_token"],
                "generated_tokens": req["generated_tokens"],
                "trajectory_complete": int(req["watermark_token"]) == int(req["generated_tokens"]),
                "policy_version": req["bound_policy_version"],
                "hbm_used_bytes": hbm_total,
                "evicted_bytes": evicted_total,
                "fixed_units_total": fixed_total,
                "remainder_bytes": remainder_total,
                "error_upper_bound_bytes": error_bound_total,
                "outcome_counts": {
                    "planned": int(pending["planned"] or 0),
                    "writing": int(pending["writing"] or 0),
                    "committed": int(pending["committed"] or 0),
                    "abandoned": int(pending["abandoned"] or 0),
                    "failed_started": int(pending["failed"] or 0),
                },
                "recovery_state": "unfinished" if (pending["planned"] or pending["writing"]) else "settled",
                "recovery_verdict": "recovery_required" if (pending["planned"] or pending["writing"]) else "consistent",
                "layers": req_layers,
            }
        )

    rounds = [
        dict(row)
        for row in db.conn.execute(
            "SELECT round_id, status, budget_bytes, max_concurrent, policy_version, abandon_reason, clock_ms FROM rounds ORDER BY round_id ASC"
        ).fetchall()
    ]
    totals = db.conn.execute(
        """
        SELECT
          COALESCE(SUM(CASE WHEN status='committed' THEN actual_bytes ELSE 0 END),0) committed_bytes,
          COALESCE(SUM(CASE WHEN status='committed' THEN 1 ELSE 0 END),0) committed_count,
          COALESCE(SUM(CASE WHEN status='abandoned' THEN 1 ELSE 0 END),0) abandoned_count,
          COALESCE(SUM(CASE WHEN status='failed_started' THEN 1 ELSE 0 END),0) failed_count
        FROM eviction_attempts
        """
    ).fetchone()

    return {
        "ok": True,
        "command": "status",
        "workspace": str(db.workspace),
        "db_path": str(db.db_path),
        "logical_clock_watermark_ms": clock_watermark,
        "active_policy_version": active_policy,
        "recovery_verdict": recovery_verdict,
        "unfinished_attempts": unfinished,
        "memory": {
            "total_memory_bytes": int(config["total_memory_bytes"]),
            "scale_bytes_per_fixed_unit": int(config["scale"]),
            "total_hbm_used_bytes": sum(layer["hbm_used_bytes"] for layer in layers),
            "total_layer_capacity_bytes": sum(layer["capacity_bytes"] for layer in layers),
            "total_evicted_bytes": sum(layer["evicted_bytes"] for layer in layers),
        },
        "totals": {k: totals[k] for k in totals.keys()},
        "layers": layers,
        "requests": requests,
        "rounds": rounds,
    }


def _add_check(checks: List[Dict[str, Any]], name: str, ok: bool, detail: Any) -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})


def verify_workspace(db: Database) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []
    config = db.conn.execute("SELECT * FROM config WHERE id=1").fetchone()
    if config is None:
        raise KvError(Code.STATE_NOT_INITIALIZED, "workspace has no loaded state")

    active_policy = db.get_meta("active_policy_version")
    policy_row = None
    if active_policy:
        policy_row = db.conn.execute(
            "SELECT COUNT(*) n FROM policy_entries WHERE policy_version=?",
            (active_policy,),
        ).fetchone()
    _add_check(
        checks,
        "active_policy_pointer",
        bool(active_policy and policy_row and policy_row["n"] > 0),
        {"active_policy_version": active_policy, "policy_count": policy_row["n"] if policy_row else 0},
    )

    unfinished = db.conn.execute(
        "SELECT COUNT(*) n FROM eviction_attempts WHERE status IN ('planned','writing')"
    ).fetchone()["n"]
    _add_check(checks, "no_unfinished_attempts", unfinished == 0, {"unfinished": unfinished})

    request_rows = db.conn.execute("SELECT * FROM requests ORDER BY seq_id").fetchall()
    request_failures: List[Dict[str, Any]] = []
    ledger_digest_records: List[Dict[str, Any]] = []
    for req in request_rows:
        seq_id = req["seq_id"]
        layer_count = db.conn.execute(
            "SELECT COUNT(*) n FROM request_layers WHERE seq_id=?",
            (seq_id,),
        ).fetchone()["n"]
        entry_count = db.conn.execute(
            "SELECT COUNT(*) n FROM kv_entries WHERE seq_id=?",
            (seq_id,),
        ).fetchone()["n"]
        expected_entries = int(req["watermark_token"]) * int(layer_count)
        if entry_count != expected_entries:
            request_failures.append({"seq_id": seq_id, "problem": "entry_count", "expected": expected_entries, "actual": entry_count})

        for layer in db.conn.execute("SELECT * FROM request_layers WHERE seq_id=? ORDER BY layer_id", (seq_id,)).fetchall():
            sums = db.conn.execute(
                """
                SELECT
                  COALESCE(SUM(size_bytes),0) total,
                  COALESCE(SUM(CASE WHEN state='hbm' THEN size_bytes ELSE 0 END),0) hbm,
                  COALESCE(SUM(CASE WHEN state='evicted' THEN size_bytes ELSE 0 END),0) evicted
                FROM kv_entries WHERE seq_id=? AND layer_id=?
                """,
                (seq_id, layer["layer_id"]),
            ).fetchone()
            conservation_ok = (
                int(sums["total"]) == int(layer["total_bytes"])
                and int(sums["hbm"]) == int(layer["hbm_used_bytes"])
                and int(sums["evicted"]) == int(layer["evicted_bytes"])
                and int(sums["total"]) == int(sums["hbm"]) + int(sums["evicted"])
            )
            fixed_identity = (
                int(layer["total_bytes"])
                == int(config["scale"]) * int(layer["fixed_units_cumulative"]) + int(layer["carry_bytes"])
            )
            if not conservation_ok or not fixed_identity:
                request_failures.append(
                    {
                        "seq_id": seq_id,
                        "layer_id": layer["layer_id"],
                        "problem": "layer_conservation_or_fixed_identity",
                        "conservation_ok": conservation_ok,
                        "fixed_identity_ok": fixed_identity,
                        "sums": dict(sums),
                        "ledger": {k: layer[k] for k in ("total_bytes", "hbm_used_bytes", "evicted_bytes", "fixed_units_cumulative", "carry_bytes")},
                    }
                )

        for attempt in db.conn.execute(
            "SELECT * FROM eviction_attempts WHERE seq_id=? AND status='committed' ORDER BY entry_id",
            (seq_id,),
        ).fetchall():
            ledger_digest_records.append(
                {
                    "ledger_key": f"ledger:{seq_id}:gen{req['request_generation']}:{attempt['entry_id']}",
                    "seq_id": seq_id,
                    "layer_id": attempt["layer_id"],
                    "entry_id": attempt["entry_id"],
                    "actual_bytes": attempt["actual_bytes"],
                    "fixed_value": attempt["fixed_value"],
                    "carry_in": attempt["carry_in"],
                    "carry_out": attempt["carry_out"],
                    "error_bound_bytes": attempt["error_bound_bytes"],
                    "policy_generation": attempt["policy_generation"],
                }
            )
    _add_check(checks, "request_byte_and_fixed_identity", not request_failures, {"failures": request_failures})

    layer_failures: List[Dict[str, Any]] = []
    capacity_failures: List[Dict[str, Any]] = []
    for layer in db.conn.execute("SELECT * FROM layer_state ORDER BY layer_id").fetchall():
        sums = db.conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN state='hbm' THEN size_bytes ELSE 0 END),0) hbm, COALESCE(SUM(CASE WHEN state='evicted' THEN size_bytes ELSE 0 END),0) evicted FROM kv_entries WHERE layer_id=?",
            (layer["layer_id"],),
        ).fetchone()
        if int(sums["hbm"]) != int(layer["hbm_used_bytes"]) or int(sums["evicted"]) != int(layer["evicted_bytes"]):
            layer_failures.append({"layer_id": layer["layer_id"], "state": dict(layer), "entries": dict(sums)})
        if int(layer["hbm_used_bytes"]) > int(layer["capacity_bytes"]):
            capacity_failures.append(
                {
                    "layer_id": layer["layer_id"],
                    "hbm_used_bytes": layer["hbm_used_bytes"],
                    "capacity_bytes": layer["capacity_bytes"],
                    "over_by": int(layer["hbm_used_bytes"]) - int(layer["capacity_bytes"]),
                }
            )
    _add_check(checks, "global_layer_ledger_matches_entries", not layer_failures, {"failures": layer_failures})
    _add_check(checks, "all_layers_within_capacity", not capacity_failures, {"over_capacity": capacity_failures})

    total_capacity = db.conn.execute("SELECT COALESCE(SUM(capacity_bytes),0) n FROM layer_state").fetchone()["n"]
    total_used = db.conn.execute("SELECT COALESCE(SUM(hbm_used_bytes),0) n FROM layer_state").fetchone()["n"]
    total_ok = int(config["total_memory_bytes"]) > int(total_capacity) and int(total_used) <= int(total_capacity)
    _add_check(
        checks,
        "total_memory_gt_layer_sum_and_used_conserved",
        total_ok,
        {
            "total_memory_bytes": config["total_memory_bytes"],
            "layer_capacity_sum": total_capacity,
            "hbm_used_bytes": total_used,
            "free_bytes": int(config["total_memory_bytes"]) - int(total_used),
        },
    )

    idem_count = db.conn.execute("SELECT COUNT(*) n FROM idempotent_ledger").fetchone()["n"]
    committed_count = db.conn.execute("SELECT COUNT(*) n FROM eviction_attempts WHERE status='committed'").fetchone()["n"]
    _add_check(checks, "idempotent_ledger_matches_committed", idem_count == committed_count, {"idempotent": idem_count, "committed": committed_count})

    frame_failures: List[Dict[str, Any]] = []
    intervals: List[Dict[str, Any]] = []
    nvme_path, _ = db.paths()
    for attempt in db.conn.execute(
        "SELECT * FROM eviction_attempts WHERE file_offset IS NOT NULL ORDER BY file_offset, attempt_id"
    ).fetchall():
        offset = int(attempt["file_offset"])
        length = int(attempt["frame_length"])
        intervals.append({"uid": attempt["uid"], "offset": offset, "length": length, "status": attempt["status"]})
        if attempt["status"] == "committed":
            result = read_frame(Path(attempt["file_path"]), offset, length)
            if not result.get("valid"):
                frame_failures.append({"uid": attempt["uid"], "problem": result.get("reason")})
            elif result["header"].get("payload_digest") != attempt["payload_digest"]:
                frame_failures.append({"uid": attempt["uid"], "problem": "digest_mismatch"})

    overlap_failures: List[Dict[str, Any]] = []
    sorted_intervals = sorted(intervals, key=lambda item: (item["offset"], item["length"]))
    for left, right in zip(sorted_intervals, sorted_intervals[1:]):
        if right["offset"] < left["offset"] + left["length"]:
            overlap_failures.append({"left": left, "right": right})
    _add_check(checks, "committed_frames_digest_valid", not frame_failures, {"failures": frame_failures, "nvme_file": str(nvme_path)})
    _add_check(checks, "nvme_offsets_do_not_overlap", not overlap_failures, {"overlaps": overlap_failures})

    ledger_digest_records.sort(key=lambda item: item["ledger_key"])
    ledger_digest = canonical_hash(ledger_digest_records)
    all_ok = all(check["ok"] for check in checks)
    return {
        "ok": all_ok,
        "command": "verify",
        "workspace": str(db.workspace),
        "ledger_digest": ledger_digest,
        "committed_evictions": committed_count,
        "checks": checks,
    }
