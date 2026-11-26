from __future__ import annotations

from typing import Any

from .codes import (
    E_OVERLAP_TURN,
    E_QUERY_MISMATCH,
    E_SPURIOUS_TURN,
    E_IDENTITY_DRIFT,
    E_SNAPSHOT_MISMATCH,
)
from .config import AuditConfig, DEFAULT_CONFIG
from .query import check_query_batch_consistency


def check_identity_stability(result: dict[str, Any]) -> dict[str, Any]:
    assignments = result.get("assignments") or {}
    identity_frames = result.get("identity_frames") or []

    if identity_frames:
        seen_segment: dict[str, str] = {}
        for frame in identity_frames:
            segment_id = str(frame["segment_id"])
            speaker_id = str(frame["speaker_id"])
            expected = seen_segment.get(segment_id)
            if expected is None:
                seen_segment[segment_id] = speaker_id
            elif expected != speaker_id:
                return {
                    "ok": False,
                    "code": E_IDENTITY_DRIFT,
                    "first_violation_frame": frame.get("frame_index"),
                    "at_ms": frame.get("at_ms"),
                    "segment_id": segment_id,
                    "expected_speaker_id": expected,
                    "actual_speaker_id": speaker_id,
                    "business_code": frame.get("business_code", E_IDENTITY_DRIFT),
                }

    for segment in result.get("segments", []):
        assignment = assignments.get(segment["segment_id"])
        if not assignment or not assignment.get("speaker_id"):
            return {
                "ok": False,
                "code": E_IDENTITY_DRIFT,
                "segment_id": segment["segment_id"],
                "first_violation_frame": segment.get("frame_range", [None])[0],
                "business_code": E_IDENTITY_DRIFT,
                "message": "segment has no stable speaker assignment",
            }
    return {"ok": True, "code": None, "segments_checked": len(result.get("segments", []))}


def check_turn_order(
    result: dict[str, Any], config: AuditConfig = DEFAULT_CONFIG
) -> dict[str, Any]:
    turns = result.get("classified_turns") or result.get("turn_points") or []
    previous: dict[str, Any] | None = None

    for turn in turns:
        if previous is not None:
            interval = int(turn["at_ms"]) - int(previous["at_ms"])
            if interval <= 0:
                return {
                    "ok": False,
                    "code": E_OVERLAP_TURN,
                    "first_violation_frame": turn.get("frame_index"),
                    "at_ms": turn.get("at_ms"),
                    "previous_event_id": previous.get("event_id"),
                    "event_id": turn.get("event_id"),
                    "interval_ms": interval,
                    "business_code": turn.get("business_code", E_OVERLAP_TURN),
                }
            if interval <= config.min_stable_turn_ms:
                return {
                    "ok": False,
                    "code": E_SPURIOUS_TURN,
                    "first_violation_frame": turn.get("frame_index"),
                    "at_ms": turn.get("at_ms"),
                    "previous_event_id": previous.get("event_id"),
                    "event_id": turn.get("event_id"),
                    "interval_ms": interval,
                    "min_stable_turn_ms": config.min_stable_turn_ms,
                    "business_code": turn.get("business_code", E_SPURIOUS_TURN),
                }
        previous = turn

    segments = sorted(result.get("segments", []), key=lambda item: int(item["start_ms"]))
    previous_segment: dict[str, Any] | None = None
    for segment in segments:
        if previous_segment is not None and int(segment["start_ms"]) < int(
            previous_segment["end_ms"]
        ):
            return {
                "ok": False,
                "code": E_OVERLAP_TURN,
                "first_violation_frame": segment.get("frame_range", [None])[0],
                "segment_id": segment.get("segment_id"),
                "previous_segment_id": previous_segment.get("segment_id"),
                "business_code": E_OVERLAP_TURN,
            }
        previous_segment = segment

    return {
        "ok": True,
        "code": None,
        "turns_checked": len(turns),
        "segments_checked": len(segments),
    }


def check_snapshot_ledger(result: dict[str, Any]) -> dict[str, Any]:
    ledger_info = result.get("ledger") or {}
    snapshots = result.get("snapshots") or {}
    ledger_snapshot = snapshots.get("ledger_write") or {}
    keys = ledger_snapshot.get("artifact_keys") or {}
    if not ledger_snapshot:
        return {"ok": False, "code": E_SNAPSHOT_MISMATCH, "message": "missing ledger snapshot"}
    mismatches = []
    for field in ("v1_key", "v2_key", "common_key"):
        if keys.get(field) != ledger_info.get(field):
            mismatches.append(field)
    return {
        "ok": not mismatches,
        "code": None if not mismatches else E_SNAPSHOT_MISMATCH,
        "mismatches": mismatches,
    }


def run_self_checks(
    result: dict[str, Any],
    config: AuditConfig = DEFAULT_CONFIG,
    incremental_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    identity = check_identity_stability(result)
    order = check_turn_order(result, config)
    query = check_query_batch_consistency(result, config)
    snapshot = check_snapshot_ledger(result)
    rule_view = result.get("rule_view_check") or {"ok": True, "equivalent": True}
    incremental = incremental_report or result.get("incremental_report") or {"ok": True}

    checks = {
        "identity_stability": identity,
        "turn_order": order,
        "query_batch_consistency": query,
        "snapshot_ledger_consistency": snapshot,
        "rule_view_equivalence": rule_view,
        "incremental_equality": incremental,
    }
    errors = []
    for name, check in checks.items():
        if not check.get("ok", False):
            errors.append(
                {
                    "check": name,
                    "code": check.get("code") or "E_CHECK_FAILED",
                    "detail": check,
                }
            )
    return {
        "ok": not errors,
        "errors": errors,
        "checks": checks,
        "error_codes": [item["code"] for item in errors],
    }


def build_diagnostic_report(
    result: dict[str, Any], self_check: dict[str, Any]
) -> dict[str, Any]:
    rows = result.get("ledger_rows") or []
    recent_business_codes = [
        {
            "ledger_id": row["common"]["ledger_id"],
            "at_ms": row["common"]["at_ms"],
            "event_id": row["common"]["event_id"],
            "business_code": row["common"]["business_code"],
        }
        for row in rows[-8:]
    ]
    anomaly_rows = [
        row for row in rows if row["common"]["business_code"] != "BIZ_TURN_NORMAL"
    ]
    trace_subject = anomaly_rows[-1] if anomaly_rows else (rows[-1] if rows else None)
    trace_chain = trace_subject["common"]["source_chain"] if trace_subject else {}
    latest_error = None
    if self_check.get("errors"):
        latest_error = self_check["errors"][-1]
    return {
        "schema": "meeting-audit/diagnostic-report/v1",
        "ok": self_check.get("ok", False),
        "latest_error_code": latest_error["code"] if latest_error else None,
        "error_codes": self_check.get("error_codes", []),
        "recent_business_codes": recent_business_codes,
        "latest_trace_chain": trace_chain,
        "latest_error_detail": latest_error,
        "cache_report": result.get("cache_report"),
        "incremental_report": result.get("incremental_report"),
    }
