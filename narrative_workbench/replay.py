from __future__ import annotations

from typing import Any

from .errors import ReplayError
from .models import Bundle
from .storyboard import build_frame_specs

REPLAY_FORMAT = "NarrativeWorkbench.ReplayReport/1"


def _marker_signature(marker: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": marker.get("record_id"),
        "event_id": marker.get("event_id"),
        "branch_id": marker.get("branch_id"),
        "trigger_id": marker.get("trigger_id"),
        "state_key": marker.get("state_key", marker.get("key")),
        "fixed_value": marker.get("fixed_value"),
        "value_kind": marker.get("value_kind"),
    }


def _first_mismatch(
    *,
    frame_index: int | None,
    scene_id: str | None,
    state_key: str | None,
    reason: str,
    expected: Any,
    actual: Any,
) -> dict[str, Any]:
    return {
        "frame_index": frame_index,
        "scene_id": scene_id,
        "state_key": state_key,
        "reason": reason,
        "expected": expected,
        "actual": actual,
    }


def verify_replay(
    bundle: Bundle,
    ledger: list[dict[str, Any]],
    frames: list[dict[str, Any]],
) -> dict[str, Any]:
    expected_frames = build_frame_specs(bundle, ledger)
    expected_by_index = {frame["frame_index"]: frame for frame in expected_frames}
    actual_by_index: dict[int, dict[str, Any]] = {}
    mismatches: list[dict[str, Any]] = []

    for frame in frames:
        frame_index = frame.get("frame_index")
        if not isinstance(frame_index, int):
            mismatches.append(
                _first_mismatch(
                    frame_index=None,
                    scene_id=frame.get("scene_id"),
                    state_key=None,
                    reason="frame_index_missing",
                    expected="non-negative integer",
                    actual=frame_index,
                )
            )
            continue
        if frame_index in actual_by_index:
            mismatches.append(
                _first_mismatch(
                    frame_index=frame_index,
                    scene_id=frame.get("scene_id"),
                    state_key=None,
                    reason="duplicate_frame",
                    expected=1,
                    actual=2,
                )
            )
            continue
        actual_by_index[frame_index] = frame

    all_indices = sorted(set(expected_by_index) | set(actual_by_index))
    for frame_index in all_indices:
        expected = expected_by_index.get(frame_index)
        actual = actual_by_index.get(frame_index)
        if expected is None:
            mismatches.append(
                _first_mismatch(
                    frame_index=frame_index,
                    scene_id=actual.get("scene_id") if actual else None,
                    state_key=None,
                    reason="unexpected_frame",
                    expected=None,
                    actual=actual,
                )
            )
            continue
        if actual is None:
            mismatches.append(
                _first_mismatch(
                    frame_index=frame_index,
                    scene_id=expected["scene_id"],
                    state_key=None,
                    reason="missing_frame",
                    expected=expected,
                    actual=None,
                )
            )
            continue
        for field in (
            "logical_time",
            "scene_id",
            "record_id",
            "kind",
            "event_id",
            "branch_id",
        ):
            if expected.get(field) != actual.get(field):
                mismatches.append(
                    _first_mismatch(
                        frame_index=frame_index,
                        scene_id=expected["scene_id"],
                        state_key=None,
                        reason=f"{field}_mismatch",
                        expected=expected.get(field),
                        actual=actual.get(field),
                    )
                )
        expected_markers = [
            _marker_signature(marker) for marker in expected["visible_markers"]
        ]
        actual_markers = [
            _marker_signature(marker)
            for marker in actual.get("visible_markers", [])
            if isinstance(marker, dict)
        ]
        if len(expected_markers) != len(actual_markers):
            mismatches.append(
                _first_mismatch(
                    frame_index=frame_index,
                    scene_id=expected["scene_id"],
                    state_key=(
                        expected_markers[0]["state_key"]
                        if expected_markers
                        else None
                    ),
                    reason="marker_count_mismatch",
                    expected=len(expected_markers),
                    actual=len(actual_markers),
                )
            )
        expected_signatures = {
            (
                marker["event_id"],
                marker["branch_id"],
                marker["state_key"],
                marker["fixed_value"],
                marker["value_kind"],
            )
            for marker in expected_markers
        }
        actual_signatures = {
            (
                marker["event_id"],
                marker["branch_id"],
                marker["state_key"],
                marker["fixed_value"],
                marker["value_kind"],
            )
            for marker in actual_markers
        }
        for signature in sorted(
            expected_signatures - actual_signatures, key=lambda item: tuple(map(str, item))
        ):
            mismatches.append(
                _first_mismatch(
                    frame_index=frame_index,
                    scene_id=expected["scene_id"],
                    state_key=signature[2],
                    reason="marker_mismatch",
                    expected={
                        "event_id": signature[0],
                        "branch_id": signature[1],
                        "state_key": signature[2],
                        "fixed_value": signature[3],
                        "value_kind": signature[4],
                    },
                    actual=None,
                )
            )
        for signature in sorted(
            actual_signatures - expected_signatures, key=lambda item: tuple(map(str, item))
        ):
            mismatches.append(
                _first_mismatch(
                    frame_index=frame_index,
                    scene_id=expected["scene_id"],
                    state_key=signature[2],
                    reason="unexpected_marker",
                    expected=None,
                    actual={
                        "event_id": signature[0],
                        "branch_id": signature[1],
                        "state_key": signature[2],
                        "fixed_value": signature[3],
                        "value_kind": signature[4],
                    },
                )
            )

    mismatches.sort(
        key=lambda item: (
            -1 if item["frame_index"] is None else item["frame_index"],
            str(item.get("state_key", "")),
            item["reason"],
        )
    )
    first = mismatches[0] if mismatches else None
    trace = []
    for frame in expected_frames:
        trace.append(
            {
                "frame_index": frame["frame_index"],
                "logical_time": frame["logical_time"],
                "scene_id": frame["scene_id"],
                "record_id": frame["record_id"],
                "event_id": frame["event_id"],
                "branch_id": frame["branch_id"],
                "markers": [
                    _marker_signature(marker)
                    for marker in frame["visible_markers"]
                ],
            }
        )
    report = {
        "format": REPLAY_FORMAT,
        "ok": first is None,
        "code": None if first is None else "E_REPLAY",
        "version_id": bundle.version_id,
        "run_id": bundle.run_id,
        "checked_frame_count": len(frames),
        "ledger_record_count": len(ledger),
        "first_mismatch": first,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:100],
        "trace": trace,
    }
    return report


def ensure_replay_ok(report: dict[str, Any]) -> None:
    if report["ok"]:
        return
    first = report.get("first_mismatch") or {}
    raise ReplayError(
        "frame replay is inconsistent with the causal ledger",
        {
            "frame_index": first.get("frame_index"),
            "scene_id": first.get("scene_id"),
            "state_key": first.get("state_key"),
            "reason": first.get("reason"),
            "expected": first.get("expected"),
            "actual": first.get("actual"),
            "mismatch_count": report.get("mismatch_count"),
        },
    )

