"""Stable status ledger rendering."""
from __future__ import annotations

from typing import Any

from .engine import loads_json
from .store import Store


def build_status(store: Store, instance_id: str | None = None) -> dict[str, Any]:
    conn = store.read_connection()
    try:
        if instance_id:
            instance_rows = conn.execute(
                "SELECT * FROM instances WHERE instance_id=? ORDER BY instance_id", (instance_id,)
            ).fetchall()
            if not instance_rows:
                return {"ok": False, "error": {"code": "HSM-1404", "message": f"unknown instance {instance_id}"}}
        else:
            instance_rows = conn.execute("SELECT * FROM instances ORDER BY instance_id").fetchall()

        instances: list[dict[str, Any]] = []
        for instance_row in instance_rows:
            iid = instance_row["instance_id"]
            state_row = conn.execute("SELECT * FROM instance_state WHERE instance_id=?", (iid,)).fetchone()
            pointer = conn.execute("SELECT * FROM unified_pointer WHERE instance_id=?", (iid,)).fetchone()
            latest_window = conn.execute(
                "SELECT * FROM window_summaries WHERE instance_id=? ORDER BY window_index DESC LIMIT 1",
                (iid,),
            ).fetchone()
            latest_score_rows = conn.execute(
                """
                SELECT s.* FROM window_level_scores s
                JOIN (SELECT level, MAX(window_index) AS window_index
                      FROM window_level_scores WHERE instance_id=? GROUP BY level) m
                  ON s.level=m.level AND s.window_index=m.window_index
                WHERE s.instance_id=? ORDER BY s.level
                """,
                (iid, iid),
            ).fetchall()
            transition_count = conn.execute(
                "SELECT COUNT(*) AS count FROM transition_log WHERE instance_id=?", (iid,)
            ).fetchone()["count"]
            chain_head = conn.execute(
                "SELECT entry_hash FROM transition_log WHERE instance_id=? ORDER BY transition_seq DESC LIMIT 1",
                (iid,),
            ).fetchone()
            arcs = conn.execute(
                "SELECT arc_seq,status,started_clock_ms,ended_clock_ms,duration_ms,required_windows,required_duration_ms,window_indices_json,result_state FROM recovery_arcs WHERE instance_id=? ORDER BY arc_seq",
                (iid,),
            ).fetchall()
            transition_rows = conn.execute(
                """
                SELECT transition_seq, from_state, to_state, window_index, at_clock_ms, trigger_level,
                       trigger_kind, trigger_threshold_json, evidence_json, reason, prev_hash, entry_hash, patch_id
                FROM transition_log WHERE instance_id=? ORDER BY transition_seq
                """,
                (iid,),
            ).fetchall()

            state = dict(state_row) if state_row else {}
            committed_seqs = loads_json(state.pop("committed_seqs_json", "{}"), {})
            current_arc = loads_json(state.pop("recovery_arc_json", "null"), None)
            latest_scores = []
            for row in latest_score_rows:
                latest_scores.append(
                    {
                        "level": row["level"],
                        "window_index": row["window_index"],
                        "score_bp": row["score_bp"],
                        "availability_bp": row["availability_bp"],
                        "latency_score_bp": row["latency_score_bp"],
                        "weight": row["weight"],
                        "successes": row["successes"],
                        "failures": row["failures"],
                        "breached": bool(row["breached"]),
                        "timeout_breached": bool(row["timeout_breached"]),
                    }
                )
            excluded = loads_json(latest_window["excluded_thresholds_json"], []) if latest_window else []
            instances.append(
                {
                    "instance_id": iid,
                    "service_name": instance_row["service_name"],
                    "endpoint": instance_row["endpoint"],
                    "state_generation": instance_row["state_generation"],
                    "state": state.get("state", "uninitialized"),
                    "failure_streak": state.get("failure_streak", 0),
                    "recovery_streak": state.get("recovery_streak", 0),
                    "recovery_duration_ms": state.get("recovery_duration_ms", 0),
                    "recovery_arc": current_arc,
                    "recovery_arcs": [
                        {
                            "arc_seq": row["arc_seq"],
                            "status": row["status"],
                            "started_clock_ms": row["started_clock_ms"],
                            "ended_clock_ms": row["ended_clock_ms"],
                            "duration_ms": row["duration_ms"],
                            "required_windows": row["required_windows"],
                            "required_duration_ms": row["required_duration_ms"],
                            "window_indices": loads_json(row["window_indices_json"], []),
                            "result_state": row["result_state"],
                        }
                        for row in arcs
                    ],
                    "committed_seqs": {key: committed_seqs[key] for key in sorted(committed_seqs)},
                    "last_committed_window_index": state.get("last_window_index"),
                    "logical_clock_watermark_ms": state.get("last_clock_ms", 0),
                    "threshold_generation": pointer["threshold_generation"] if pointer else state.get("threshold_generation"),
                    "threshold_view_revision": pointer["view_revision"] if pointer else state.get("threshold_revision"),
                    "threshold_view_hash": pointer["view_hash"] if pointer else state.get("threshold_hash"),
                    "latest_window_index": latest_window["window_index"] if latest_window else None,
                    "latest_window_score_bp": latest_window["score_bp"] if latest_window else None,
                    "latest_scores_by_level": latest_scores,
                    "excluded_thresholds": excluded,
                    "transition_count": transition_count,
                    "transitions": [
                        {
                            "transition_seq": row["transition_seq"],
                            "from_state": row["from_state"],
                            "to_state": row["to_state"],
                            "window_index": row["window_index"],
                            "at_clock_ms": row["at_clock_ms"],
                            "trigger_level": row["trigger_level"],
                            "trigger_kind": row["trigger_kind"],
                            "trigger_threshold": loads_json(row["trigger_threshold_json"], {}),
                            "evidence": loads_json(row["evidence_json"], {}),
                            "reason": row["reason"],
                            "prev_hash": row["prev_hash"],
                            "entry_hash": row["entry_hash"],
                            "patch_id": row["patch_id"],
                        }
                        for row in transition_rows
                    ],
                    "transition_chain_head": chain_head["entry_hash"] if chain_head else None,
                    "state_version": state.get("state_version", 0),
                    "state_hash": state.get("state_hash"),
                }
            )
        return {"ok": True, "instance_count": len(instances), "instances": instances}
    finally:
        conn.close()
