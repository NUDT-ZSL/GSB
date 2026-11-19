"""Cross-module consistency checks for snapshots, segments, and reports."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .aggregation import recompute_dialog_from_details, report_hash
from .canonical import canonical_json_bytes, checksum_value, round_float
from .errors import BusinessError, E_INCONSISTENT
from .snapshot import STAGE_ORDER, SnapshotStore


def run_selfcheck(
    store: SnapshotStore,
    *,
    expected_dialog_ids: list[str] | None = None,
    full_report_bytes: bytes | None = None,
    recovered_report_bytes: bytes | None = None,
    require_complete: bool = True,
) -> dict[str, Any]:
    status = store.status()
    checks: list[dict[str, Any]] = []
    committed = [
        stage for stage in STAGE_ORDER if status["stages"].get(stage, {}).get("status") == "committed"
    ]
    if require_complete and len(committed) != len(STAGE_ORDER):
        checks.append(
            _failed(
                "stage_completeness",
                {
                    "committed_stages": committed,
                    "missing_stages": [stage for stage in STAGE_ORDER if stage not in committed],
                },
            )
        )
    else:
        checks.append(_passed("stage_completeness", {"committed_stages": committed}))

    payloads = {stage: store.load_stage(stage) for stage in committed}
    expected_ids = (
        sorted(expected_dialog_ids)
        if expected_dialog_ids is not None
        else sorted(str(item) for item in payloads[committed[0]]["dialog_ids"])
        if committed
        else []
    )
    stage_sets: dict[str, list[str]] = {}
    for stage, payload in payloads.items():
        actual = sorted(str(item) for item in payload.get("dialog_ids", []))
        stage_sets[stage] = actual
        if actual != expected_ids:
            checks.append(
                _failed(
                    "dialog_set_consistency",
                    {
                        "stage": stage,
                        "expected": expected_ids,
                        "actual": actual,
                    },
                )
            )
            break
    else:
        checks.append(
            _passed(
                "dialog_set_consistency",
                {"dialog_ids": expected_ids, "stages": stage_sets},
            )
        )

    versions = [
        (stage, int(status["stages"][stage]["version"]))
        for stage in committed
        if stage in status["stages"]
    ]
    monotonic = all(left[1] < right[1] for left, right in zip(versions, versions[1:]))
    checks.append(
        _passed("monotonic_snapshot_versions", {"versions": versions})
        if monotonic
        else _failed("monotonic_snapshot_versions", {"versions": versions})
    )

    segmentation_ok = True
    segmentation_details: list[dict[str, Any]] = []
    regression_by_id: dict[str, dict] = {}
    if "regression" in payloads:
        regression_by_id = {
            item["dialog_id"]: item for item in payloads["regression"]["dialogs"]
        }
    if "segmentation" in payloads:
        frame_ms_by_dialog = {
            item["dialog_id"]: float(item["frame_ms"])
            for item in payloads.get("framing", {}).get("dialogs", [])
        }
        for dialog in payloads["segmentation"]["dialogs"]:
            dialog_id = dialog["dialog_id"]
            labels = dialog["speaker_ids"]
            num_frames = int(dialog["num_frames"])
            frame_ms = frame_ms_by_dialog.get(dialog_id)
            cursor = 0
            problem: dict[str, Any] | None = None
            if len(labels) != num_frames:
                problem = {
                    "reason": "label_count",
                    "dialog_id": dialog_id,
                    "expected": num_frames,
                    "actual": len(labels),
                }
            if problem is None:
                for segment_index, segment in enumerate(dialog["segments"]):
                    start = int(segment["start_frame"])
                    end = int(segment["end_frame"])
                    if start != cursor or end < start:
                        problem = {
                            "reason": "segment_gap_or_overlap",
                            "dialog_id": dialog_id,
                            "segment_index": segment_index,
                            "expected_start": cursor,
                            "actual_start": start,
                            "end": end,
                        }
                        break
                    expected_label = int(segment["speaker_id"])
                    if any(int(label) != expected_label for label in labels[start : end + 1]):
                        problem = {
                            "reason": "segment_label_mismatch",
                            "dialog_id": dialog_id,
                            "segment_index": segment_index,
                            "start": start,
                            "end": end,
                        }
                        break
                    expected_duration = (
                        (end - start + 1) * frame_ms
                        if frame_ms is not None
                        else float(segment["duration_ms"])
                    )
                    if abs(float(segment["duration_ms"]) - expected_duration) > 1e-9:
                        problem = {
                            "reason": "segment_duration_mismatch",
                            "dialog_id": dialog_id,
                            "segment_index": segment_index,
                        }
                        break
                    cursor = end + 1
                if problem is None and cursor != num_frames:
                    problem = {
                        "reason": "segment_coverage",
                        "dialog_id": dialog_id,
                        "covered_until": cursor,
                        "num_frames": num_frames,
                    }
            if problem is not None:
                segmentation_ok = False
                segmentation_details.append(problem)
        checks.append(
            _passed("segmentation_contiguous", {"dialog_count": len(payloads["segmentation"]["dialogs"])})
            if segmentation_ok
            else _failed("segmentation_contiguous", {"problems": segmentation_details})
        )
    else:
        checks.append(_failed("segmentation_contiguous", {"reason": "stage_not_committed"}))

    if "segmentation" in payloads and "regression" in payloads:
        regression_links_ok = True
        problems: list[dict[str, Any]] = []
        for dialog in payloads["segmentation"]["dialogs"]:
            regression = regression_by_id.get(dialog["dialog_id"])
            if regression is None:
                regression_links_ok = False
                problems.append({"reason": "missing_dialog", "dialog_id": dialog["dialog_id"]})
                continue
            source_segments = dialog["segments"]
            target_segments = regression["segments"]
            if len(source_segments) != len(target_segments):
                regression_links_ok = False
                problems.append(
                    {
                        "reason": "segment_count",
                        "dialog_id": dialog["dialog_id"],
                        "segmentation": len(source_segments),
                        "regression": len(target_segments),
                    }
                )
                continue
            for source, target in zip(source_segments, target_segments):
                if any(
                    source[key] != target[key]
                    for key in ("speaker_id", "start_frame", "end_frame", "duration_ms")
                ):
                    regression_links_ok = False
                    problems.append(
                        {
                            "reason": "segment_bounds_mismatch",
                            "dialog_id": dialog["dialog_id"],
                            "start_frame": source["start_frame"],
                        }
                    )
                    break
        checks.append(
            _passed("regression_segment_links")
            if regression_links_ok
            else _failed("regression_segment_links", {"problems": problems})
        )
    else:
        checks.append(_failed("regression_segment_links", {"reason": "stage_not_committed"}))

    aggregation_ok = True
    aggregate_problems: list[dict[str, Any]] = []
    report: dict[str, Any] | None = None
    if "aggregation" in payloads:
        report = payloads["aggregation"]["report"]
        actual_hash = report.get("report_hash")
        recomputed_hash = report_hash(report)
        if actual_hash != recomputed_hash:
            aggregation_ok = False
            aggregate_problems.append(
                {"reason": "report_hash", "expected": recomputed_hash, "actual": actual_hash}
            )
        expected_regression_ids = sorted(regression_by_id) if regression_by_id else expected_ids
        report_ids = sorted(item["dialog_id"] for item in report.get("dialogs", []))
        if report_ids != expected_regression_ids:
            aggregation_ok = False
            aggregate_problems.append(
                {"reason": "report_dialog_set", "expected": expected_regression_ids, "actual": report_ids}
            )
        score_places = _score_places(report)
        for dialog in report.get("dialogs", []):
            recomputed_valence, recomputed_arousal = recompute_dialog_from_details(dialog)
            if (
                round_float(recomputed_valence, score_places) != float(dialog["weighted_valence"])
                or round_float(recomputed_arousal, score_places) != float(dialog["weighted_arousal"])
            ):
                aggregation_ok = False
                aggregate_problems.append(
                    {
                        "reason": "weighted_score",
                        "dialog_id": dialog.get("dialog_id"),
                        "recomputed_valence": round_float(recomputed_valence, score_places),
                        "stored_valence": dialog.get("weighted_valence"),
                        "recomputed_arousal": round_float(recomputed_arousal, score_places),
                        "stored_arousal": dialog.get("weighted_arousal"),
                    }
                )
                break
        checks.append(
            _passed("aggregation_recomputable", {"dialog_count": len(report.get("dialogs", []))})
            if aggregation_ok
            else _failed("aggregation_recomputable", {"problems": aggregate_problems})
        )
    else:
        checks.append(_failed("aggregation_recomputable", {"reason": "stage_not_committed"}))

    if full_report_bytes is not None and recovered_report_bytes is not None:
        full_hash = checksum_value_bytes(full_report_bytes)
        recovered_hash = checksum_value_bytes(recovered_report_bytes)
        checks.append(
            _passed(
                "recovery_byte_identity",
                {"full_report_checksum": full_hash, "recovered_report_checksum": recovered_hash},
            )
            if full_report_bytes == recovered_report_bytes
            else _failed(
                "recovery_byte_identity",
                {"full_report_checksum": full_hash, "recovered_report_checksum": recovered_hash},
            )
        )
    else:
        checks.append(_passed("recovery_byte_identity", {"status": "not_requested"}))

    failed = [item for item in checks if not item["ok"]]
    summary = {
        "ok": not failed,
        "checks": checks,
        "checksum": checksum_value(checks),
        "report_hash": report.get("report_hash") if report else None,
        "status": status,
    }
    if failed:
        raise BusinessError(
            E_INCONSISTENT,
            "Cross-module consistency self-check failed",
            {"failed_checks": failed, "checksum": summary["checksum"]},
        )
    return summary


def compare_report_files(
    full_path: Path,
    recovered_path: Path,
    store: SnapshotStore,
    *,
    expected_dialog_ids: list[str] | None = None,
) -> dict[str, Any]:
    full_bytes = full_path.read_bytes()
    recovered_bytes = recovered_path.read_bytes()
    return run_selfcheck(
        store,
        expected_dialog_ids=expected_dialog_ids,
        full_report_bytes=full_bytes,
        recovered_report_bytes=recovered_bytes,
    )


def checksum_value_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _scale_places_from_segments(report: dict[str, Any]) -> int:
    decimals = 0
    for dialog in report.get("dialogs", []):
        for segment in dialog.get("segments", []):
            for key in ("valence", "arousal", "duration_ms"):
                text = repr(segment.get(key, 0.0))
                if "." in text and "e" not in text.lower():
                    decimals = max(decimals, len(text.split(".", 1)[1].rstrip("0")))
    return min(15, max(2, decimals))


def _score_places(report: dict[str, Any]) -> int:
    return _scale_places_from_segments(report)


def _passed(name: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"name": name, "ok": True, "details": details or {}}


def _failed(name: str, details: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "ok": False, "details": details}

