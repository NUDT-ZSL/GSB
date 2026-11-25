"""Stable sorted JSON status reporting."""

from __future__ import annotations

import json
from typing import Any

from .store import Store


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def build_status(store: Store) -> dict[str, Any]:
    pointer = store.current_snapshot()
    watermark = store.watermark()
    sessions: list[dict[str, Any]] = []
    counts = {"success": 0, "failed": 0, "skipped": 0, "pending": 0, "claimed": 0}
    for row in store.all_status_rows():
        state = _row_to_dict(row)
        result = json.loads(state["result_json"]) if state.get("result_json") else None
        basis = json.loads(state["stage_basis_json"]) if state.get("stage_basis_json") else {}
        conclusion = state.get("conclusion")
        attempt_status = state.get("attempt_status")
        if conclusion in counts:
            counts[conclusion] += 1
        elif attempt_status in {"pending", "claimed"}:
            counts[attempt_status] += 1
        version_basis = {
            "snapshot_version": state["snapshot_version"],
            "bound_snapshot_version": state["bound_snapshot_version"],
            "generation": state["generation"],
            "state_table_version": state["table_version"],
            "attempt_table_version": state.get("attempt_table_version"),
            "attempt_snapshot_version": state.get("attempt_snapshot_version"),
            "evidence_hash": state.get("evidence_hash"),
            "committed_clock_ms": state.get("committed_clock_ms"),
            "adjudication": basis.get("adjudication"),
            "table_version_comparison": basis.get("table_version_comparison"),
        }
        sessions.append(
            {
                "connection_id": state["connection_id"],
                "generation": state["generation"],
                "lifecycle_stage": state["stage"],
                "claim_status": state["claim_status"],
                "attempt_status": attempt_status,
                "forwarding_conclusion": conclusion,
                "call_count": state.get("call_count", 0),
                "route_keys": json.loads(state["route_keys_json"]),
                "logical_clock_ms": state["logical_clock_ms"],
                "version_basis": version_basis,
                "forwarding_result": result,
            }
        )
    sessions.sort(key=lambda item: item["connection_id"])
    return {
        "ok": True,
        "format": "proxy-workbench-status",
        "snapshot_pointer": _row_to_dict(pointer) if pointer else None,
        "logical_clock_watermark_ms": watermark,
        "conclusion_counts": counts,
        "session_count": len(sessions),
        "sessions": sessions,
    }
