"""Diagnostic report assembly and atomic JSON export."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from .checkpoint import atomic_write_json
from .fixedpoint import FIXED_SCALE, FIXED_SCALE_VERSION, REPORT_SCHEMA_VERSION
from .fragmentation import plan_all
from .model import ParseResult


def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "to_dict"):
        return json_safe(value.to_dict())
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def build_report(
    parsed: ParseResult,
    state: dict[str, Any],
    *,
    sample_path: Optional[str | Path] = None,
    sample_sha256: Optional[str] = None,
    checkpoint_verification: Optional[dict[str, Any]] = None,
    rebuild_info: Optional[dict[str, Any]] = None,
    run_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    plans, plan_errors, marker_reports = plan_all(parsed.peers, parsed.policy)
    peer_results = []
    completed = stalled = mtu_infeasible = 0
    total_acked = 0
    total_fragments = 0
    for peer in parsed.peers:
        peer_state = state["peers"].get(peer.name, {})
        status = peer_state.get("status")
        if status == "completed":
            completed += 1
        elif status == "stalled":
            stalled += 1
        elif status == "mtu_infeasible":
            mtu_infeasible += 1
        total_acked += int(peer_state.get("acknowledged_fragments", 0))
        total_fragments += int(peer_state.get("total_fragments", 0))
        peer_results.append({
            "config": peer.to_dict(),
            "profile": peer_state.get("profile"),
            "progress": {
                key: peer_state.get(key)
                for key in (
                    "status",
                    "fragment_order",
                    "inflight",
                    "total_fragments",
                    "acknowledged_fragments",
                    "retransmit_used",
                    "send_operations",
                    "last_valid_progress",
                    "stall",
                    "closures",
                    "round_codes",
                    "completed_at_round",
                    "completed_at_clock_ms",
                )
            },
            "sync_markers": marker_reports.get(peer.name),
            "fragments": [
                state["fragments"][key]
                for key in peer_state.get("fragment_order", [])
                if key in state.get("fragments", {})
            ],
        })

    ambiguous = state.get("tolerance_ambiguities", [])
    deviations = state.get("observation_deviations", [])
    report: dict[str, Any] = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "sample": {
            "path": str(sample_path) if sample_path is not None else None,
            "sha256": sample_sha256,
            "seed": parsed.seed,
            "schema_version": parsed.schema_version,
            "clock_origin_ms": parsed.clock_origin_ms,
        },
        "run_meta": run_meta or {},
        "summary": {
            "accepted_peers": len(parsed.peers),
            "rejected_records": len(parsed.rejections),
            "peers_completed": completed,
            "peers_stalled": stalled,
            "peers_mtu_infeasible": mtu_infeasible,
            "rounds_completed": state.get("round_watermark"),
            "final_logical_clock_ms": int(state.get("logical_clock_q", 0)) / 1000,
            "fragments_total": total_fragments,
            "fragments_acknowledged": total_acked,
            "divergence_count": len(state.get("divergences", [])),
            "tolerance_ambiguity_count": len(ambiguous),
            "observation_deviation_count": len(deviations),
            "profile_closure_count": len(state.get("closures", [])),
            "termination": state.get("termination"),
        },
        "validation": parsed.to_summary(),
        "events": [event.to_dict() for event in parsed.events],
        "fragment_plan_errors": plan_errors,
        "sync_marker_reports": marker_reports,
        "rounds": state.get("rounds", []),
        "peers": peer_results,
        "divergences": state.get("divergences", []),
        "tolerance_ambiguities": ambiguous,
        "cumulative_residual_ms": state.get("cumulative_residual_ms", 0.0),
        "observation_deviations": deviations,
        "profile_switch_closures": state.get("closures", []),
        "business_codes": state.get("codes", []),
        "reference_numeric_path": {
            "type": "python_float",
            "ieee754": "binary64",
            "fixed_scale": FIXED_SCALE,
            "fixed_point_version": FIXED_SCALE_VERSION,
        },
        "checkpoint": checkpoint_verification,
        "checkpoint_rebuild": rebuild_info,
    }
    return json_safe(report)


def write_report(path: str | Path, report: dict[str, Any]) -> Path:
    return atomic_write_json(path, report)
