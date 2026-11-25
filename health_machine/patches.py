"""Incremental threshold/ID/tombstone patches and dependency closure."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .engine import _commit_window, initial_state
from .errors import InvalidPatch
from .store import Store
from .util import canonical_json, is_finite_number, is_real_int, read_json, sha256_json


INSTANCE_TABLES = [
    "observation_inbox",
    "threshold_candidates",
    "threshold_view",
    "unified_pointer",
    "window_summaries",
    "window_level_scores",
    "instance_state",
    "transition_log",
    "recovery_arcs",
]


def load_patch(path: str | Path) -> dict[str, Any]:
    patch = read_json(path)
    if not isinstance(patch, dict):
        raise InvalidPatch("patch must be a JSON object", location={"path": str(path)})
    patch_id = patch.get("patch_id")
    op = patch.get("op")
    if not isinstance(patch_id, str) or not patch_id:
        raise InvalidPatch("patch_id must be a non-empty string", location={"path": "patch_id"})
    if op not in {"upsert_threshold", "rename_instance", "tombstone"}:
        raise InvalidPatch(f"unsupported patch op {op!r}", details={"supported": ["upsert_threshold", "rename_instance", "tombstone"]})
    return patch


def _pointer_hash(conn: Any, instance_id: str) -> str:
    rows = conn.execute("SELECT content_hash FROM threshold_view WHERE instance_id=? ORDER BY level", (instance_id,)).fetchall()
    return sha256_json([row["content_hash"] for row in rows])


def _update_pointer(conn: Any, instance_id: str, patch_id: str, clock_ms: int) -> None:
    row = conn.execute("SELECT * FROM unified_pointer WHERE instance_id=?", (instance_id,)).fetchone()
    max_generation = conn.execute(
        "SELECT MAX(generation) AS generation FROM threshold_view WHERE instance_id=?",
        (instance_id,),
    ).fetchone()["generation"]
    view_hash = _pointer_hash(conn, instance_id)
    if row is None:
        conn.execute(
            "INSERT INTO unified_pointer(instance_id, view_revision, view_hash, threshold_generation, updated_patch_id, updated_clock_ms) VALUES (?, 1, ?, ?, ?, ?)",
            (instance_id, view_hash, max_generation or 0, patch_id, clock_ms),
        )
    else:
        conn.execute(
            """
            UPDATE unified_pointer SET view_revision=view_revision+1, view_hash=?, threshold_generation=?,
                updated_patch_id=?, updated_clock_ms=?
            WHERE instance_id=?
            """,
            (view_hash, max_generation or 0, patch_id, clock_ms, instance_id),
        )


def _recompute_instance_conn(conn: Any, store: Store, instance_id: str, patch_id: str) -> dict[str, Any]:
    conn.execute("DELETE FROM window_level_scores WHERE instance_id=?", (instance_id,))
    conn.execute("DELETE FROM window_summaries WHERE instance_id=?", (instance_id,))
    conn.execute("DELETE FROM transition_log WHERE instance_id=?", (instance_id,))
    conn.execute("DELETE FROM recovery_arcs WHERE instance_id=?", (instance_id,))
    state = initial_state(instance_id)
    state["updated_patch_id"] = patch_id
    conn.execute(
        """
        UPDATE instance_state SET state=?, failure_streak=?, recovery_streak=?, recovery_arc_json=?,
            recovery_start_clock_ms=?, recovery_last_clock_ms=?, recovery_duration_ms=?,
            last_window_index=?, last_clock_ms=?, committed_seqs_json=?, committed_stream_seq=?,
            threshold_revision=?, threshold_hash=?, threshold_generation=?, state_version=?,
            state_hash=?, updated_patch_id=?
        WHERE instance_id=?
        """,
        (
            state["state"], state["failure_streak"], state["recovery_streak"], canonical_json(None),
            None, None, 0, None, 0, canonical_json({}), 0, None, None, None, 0,
            state["state_hash"], patch_id, instance_id,
        ),
    )
    windows = [
        row["window_index"]
        for row in conn.execute(
            "SELECT DISTINCT window_index FROM observation_inbox WHERE instance_id=? ORDER BY window_index",
            (instance_id,),
        ).fetchall()
    ]
    results = [_commit_window(conn, store, instance_id, window_index) for window_index in windows]
    order_row = conn.execute("SELECT COALESCE(MAX(created_order), 0) + 1 AS next_order FROM recompute_audit").fetchone()
    conn.execute(
        "INSERT INTO recompute_audit(patch_id, run_reason, instance_id, scope, window_count, created_order) VALUES (?, ?, ?, ?, ?, ?)",
        (patch_id, "dependency_closure", instance_id, "score,state,recovery", len(windows), order_row["next_order"]),
    )
    conn.execute("UPDATE patch_closures SET recomputed=1 WHERE patch_id=? AND instance_id=?", (patch_id, instance_id))
    return {"instance_id": instance_id, "windows_recomputed": len(windows), "final_state": results[-1]["state"] if results else "healthy"}


def apply_patch(store: Store, patch: dict[str, Any]) -> dict[str, Any]:
    patch_id = patch["patch_id"]
    op = patch["op"]
    patch_hash = sha256_json(patch)

    with store.transaction("_patch") as conn:
        existing = conn.execute("SELECT patch_hash, result_json FROM patches WHERE patch_id=?", (patch_id,)).fetchone()
        if existing is not None:
            if existing["patch_hash"] != patch_hash:
                raise InvalidPatch(
                    "patch_id was already used with different content",
                    details={"patch_id": patch_id},
                    code="HSM-6002",
                )
            return {"ok": True, "idempotent": True, **__import__("json").loads(existing["result_json"])}

        affected: list[dict[str, Any]] = []
        recomputed: list[dict[str, Any]] = []
        clock_ms = patch.get("logical_clock_ms", 0)
        if not is_real_int(clock_ms) or clock_ms < 0:
            raise InvalidPatch("logical_clock_ms must be a non-negative integer", location={"path": "logical_clock_ms"})

        if op in {"upsert_threshold", "tombstone"}:
            instance_id = patch.get("instance_id")
            level = patch.get("level")
            instance = conn.execute("SELECT instance_id FROM instances WHERE instance_id=?", (instance_id,)).fetchone()
            if instance is None:
                raise InvalidPatch("patch targets unknown instance", location={"instance_id": instance_id if isinstance(instance_id, str) else None})
            if not is_real_int(level) or level <= 0:
                raise InvalidPatch("patch level must be a positive integer", location={"instance_id": instance_id, "level": level if is_real_int(level) else None})
            declared = conn.execute("SELECT levels_json FROM instances WHERE instance_id=?", (instance_id,)).fetchone()
            import json

            if level not in json.loads(declared["levels_json"]):
                raise InvalidPatch("patch level is not declared by registry", location={"instance_id": instance_id, "level": level})
            current = conn.execute("SELECT * FROM threshold_view WHERE instance_id=? AND level=?", (instance_id, level)).fetchone()
            generation = patch.get("generation")
            if not is_real_int(generation) or generation < 1:
                raise InvalidPatch("patch generation must be a positive integer", location={"instance_id": instance_id, "level": level})
            if current is not None and generation <= current["generation"]:
                raise InvalidPatch(
                    "patch threshold generation must be strictly newer than current view generation",
                    location={"instance_id": instance_id, "level": level},
                    details={"current_generation": current["generation"], "patch_generation": generation},
                )

            closure_scopes = ["level_score", "window_score", "state_transition", "recovery_arc"]
            for scope in closure_scopes:
                conn.execute(
                    "INSERT INTO patch_closures(patch_id, instance_id, scope_level, scope, reason) VALUES (?, ?, ?, ?, ?)",
                    (patch_id, instance_id, level, scope, op),
                )
            affected.append({"instance_id": instance_id, "level": level, "scopes": closure_scopes})

            if op == "upsert_threshold":
                weight = patch.get("weight", 1)
                failure_limit = patch.get("failure_limit", 0)
                min_score_bp = patch.get("min_score_bp", 7000)
                latency_budget = patch.get("latency_budget_ms", 100)
                failure_counts = patch.get("failure_counts", [failure_limit])
                if not is_real_int(weight) or weight <= 0:
                    raise InvalidPatch("weight must be a positive integer", location={"instance_id": instance_id, "level": level})
                if not is_real_int(failure_limit) or failure_limit < 0:
                    raise InvalidPatch("failure_limit must be a non-negative integer", location={"instance_id": instance_id, "level": level})
                if not is_real_int(min_score_bp) or not (0 <= min_score_bp <= 10000):
                    raise InvalidPatch("min_score_bp must be in 0..10000", location={"instance_id": instance_id, "level": level})
                if not is_finite_number(latency_budget):
                    raise InvalidPatch("latency_budget_ms must be finite", location={"instance_id": instance_id, "level": level})
                if not isinstance(failure_counts, list) or any(not is_real_int(item) or item < 0 for item in failure_counts):
                    raise InvalidPatch("failure_counts must be non-negative integer arrays", location={"instance_id": instance_id, "level": level})
                effective_at_ms = patch.get("effective_at_ms", clock_ms)
                time_meta = {
                    "raw_value": str(effective_at_ms),
                    "raw_unit": "milliseconds",
                    "scale_to_ms": 1,
                    "rounding": "identity",
                    "absolute_effective_at_ms": effective_at_ms,
                }
                decision = {"reason": "incremental_patch_upsert", "patch_id": patch_id}
                content_hash = sha256_json({"patch_id": patch_id, "instance_id": instance_id, "level": level, "generation": generation, "failure_limit": failure_limit})
                conn.execute(
                    """
                    INSERT OR REPLACE INTO threshold_view
                    (instance_id, level, winner_format, winner_snapshot_id, generation, logical_clock_ms,
                     effective_at_ms, weight, failure_limit, min_score_bp, latency_budget_ms, active,
                     exclusion_reason, excluded_generation, raw_failure_counts_json, time_conversion_json,
                     provenance_json, decision_json, view_revision, content_hash, updated_patch_id)
                    VALUES (?, ?, 'patch', ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, NULL, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        instance_id, level, patch_id, generation, clock_ms, effective_at_ms, weight,
                        failure_limit, min_score_bp, str(latency_budget), canonical_json(failure_counts),
                        canonical_json(time_meta), canonical_json({"patch": patch}), canonical_json(decision),
                        content_hash, patch_id,
                    ),
                )
            else:
                reason = patch.get("reason", "patched tombstone")
                conn.execute(
                    """
                    UPDATE threshold_view SET active=0, exclusion_reason='tombstoned', excluded_generation=?,
                        generation=?, logical_clock_ms=?, updated_patch_id=?
                    WHERE instance_id=? AND level=?
                    """,
                    (generation, generation, clock_ms, patch_id, instance_id, level),
                )
                if conn.total_changes == 0:
                    raise InvalidPatch("cannot tombstone missing threshold view; run merge first", location={"instance_id": instance_id, "level": level})
            _update_pointer(conn, instance_id, patch_id, clock_ms)
            recomputed.append(_recompute_instance_conn(conn, store, instance_id, patch_id))

        elif op == "rename_instance":
            old_id = patch.get("old_instance_id")
            new_id = patch.get("new_instance_id")
            if not isinstance(old_id, str) or not isinstance(new_id, str) or not old_id or not new_id:
                raise InvalidPatch("rename requires non-empty old/new instance ids")
            if conn.execute("SELECT 1 FROM instances WHERE instance_id=?", (old_id,)).fetchone() is None:
                raise InvalidPatch("rename source instance does not exist", location={"instance_id": old_id})
            if conn.execute("SELECT 1 FROM instances WHERE instance_id=?", (new_id,)).fetchone() is not None:
                raise InvalidPatch("rename target instance already exists", location={"instance_id": new_id})
            closure_scopes = ["registry_id", "level_score", "window_score", "state_transition", "recovery_arc"]
            for scope in closure_scopes:
                conn.execute(
                    "INSERT INTO patch_closures(patch_id, instance_id, scope_level, scope, reason) VALUES (?, ?, NULL, ?, 'rename_instance')",
                    (patch_id, old_id, scope),
                )
            affected.append({"old_instance_id": old_id, "new_instance_id": new_id, "scopes": closure_scopes})
            for table in INSTANCE_TABLES:
                if table == "unified_pointer":
                    conn.execute(f"UPDATE {table} SET instance_id=? WHERE instance_id=?", (new_id, old_id))
                else:
                    conn.execute(f"UPDATE {table} SET instance_id=? WHERE instance_id=?", (new_id, old_id))
            conn.execute("UPDATE instances SET instance_id=? WHERE instance_id=?", (new_id, old_id))
            _update_pointer(conn, new_id, patch_id, clock_ms)
            recomputed.append(_recompute_instance_conn(conn, store, new_id, patch_id))

        result = {
            "patch_id": patch_id,
            "op": op,
            "affected_closure": affected,
            "recomputed": recomputed,
            "reusable_instances": _reusable_instances(conn, [item.get("instance_id") or item.get("new_instance_id") for item in affected]),
        }
        conn.execute(
            "INSERT INTO patches(patch_id, patch_hash, patch_type, payload_json, result_json, applied_clock_ms) VALUES (?, ?, ?, ?, ?, ?)",
            (patch_id, patch_hash, op, canonical_json(patch), canonical_json(result), clock_ms),
        )
        return {"ok": True, "idempotent": False, **result}


def _reusable_instances(conn: Any, affected_ids: list[str | None]) -> list[str]:
    affected = {item for item in affected_ids if item}
    rows = conn.execute("SELECT instance_id FROM instances ORDER BY instance_id").fetchall()
    return [row["instance_id"] for row in rows if row["instance_id"] not in affected]


def apply_patch_file(store: Store, path: str | Path) -> dict[str, Any]:
    return apply_patch(store, load_patch(path))
