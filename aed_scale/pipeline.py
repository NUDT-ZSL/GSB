"""End-to-end dataset execution joining all AED stages."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Sequence

from .adaptive import ResourceConstraints, ScaleObservation, decide_path
from .codes import BusinessCode
from .config import (
    DetectionConfig,
    FeatureConfig,
    ms_to_samples,
    validate_detection_config,
    validate_feature_config,
)
from .detection import detect_candidates
from .errors import AedError
from .features import extract_features, frame_count_for_samples
from .io import (
    canonical_json_bytes,
    load_manifest,
    load_truth_frames,
    load_wav_pcm16_mono,
    sha256_file,
)
from .metrics import (
    aggregate_metrics,
    compute_dialog_metrics,
    flatten_frame_decisions,
)
from .paths import path_profiles
from .postprocess import postprocess_candidates
from .types import TimelineSegment

REPORT_SCHEMA_VERSION = "1.0"


def _frame_indices_sha(indices: Sequence[int]) -> str:
    return hashlib.sha256(canonical_json_bytes([int(index) for index in indices])).hexdigest()


def _dialog_paths(dataset_root: Path, row: dict[str, Any]) -> tuple[Path, Path]:
    dialog_id = row["dialog_id"]
    if "wav_path" in row:
        wav_path = Path(row["wav_path"])
        if not wav_path.is_absolute():
            wav_path = dataset_root / wav_path
    else:
        wav_path = dataset_root / "dialogs" / dialog_id / "audio.wav"
    if "truth_path" in row:
        truth_path = Path(row["truth_path"])
        if not truth_path.is_absolute():
            truth_path = dataset_root / truth_path
    else:
        truth_path = dataset_root / "dialogs" / dialog_id / "frames.jsonl"
    return wav_path, truth_path


def _segment_dicts(segments: Sequence[TimelineSegment]) -> list[dict[str, Any]]:
    return [segment.to_dict() for segment in segments]


def run_dataset(
    dataset_dir: str | Path,
    *,
    config: FeatureConfig | None = None,
    detection_config: DetectionConfig | None = None,
    constraints: ResourceConstraints | None = None,
    recall_gain_estimate: float = 0.05,
    path_override: str | None = None,
) -> dict[str, Any]:
    """Run feature extraction, detection, post-processing, metrics and checks."""

    root = Path(dataset_dir)
    config = config or FeatureConfig(feature_mode="full")
    detection_config = detection_config or DetectionConfig()
    constraints = constraints or ResourceConstraints()
    validate_feature_config(config)
    validate_detection_config(detection_config)
    if path_override not in {None, "fine", "approximate"}:
        raise AedError(
            BusinessCode.E_CONFIG_INVALID,
            "path_override must be 'fine', 'approximate' or null",
            field="path_override",
        )
    base_config = config.with_mode("full")
    profiles = path_profiles(detection_config)
    manifest_path = root / "manifest.jsonl"
    manifest = load_manifest(manifest_path)

    contexts: list[dict[str, Any]] = []
    total_frames = 0
    total_preflight_candidate_frames = 0
    speaker_ids: set[str] = set()
    for row in manifest:
        dialog_id = row["dialog_id"]
        wav_path, truth_path = _dialog_paths(root, row)
        pcm, sample_rate = load_wav_pcm16_mono(wav_path, dialog_id=dialog_id)
        if sample_rate != base_config.sample_rate:
            raise AedError(
                BusinessCode.E_CONFIG_INVALID,
                "audio sample rate does not match configuration",
                dialog_id=dialog_id,
                field="sample_rate",
            )
        expected_samples = ms_to_samples(
            float(row["duration_ms"]),
            sample_rate,
            field="duration_ms",
            dialog_id=dialog_id,
        )
        if pcm.size != expected_samples:
            raise AedError(
                BusinessCode.E_TIMELINE_INVALID,
                f"WAV has {pcm.size} samples but duration_ms implies {expected_samples}",
                dialog_id=dialog_id,
                field="duration_ms",
                details={
                    "pcm_samples": int(pcm.size),
                    "expected_samples": expected_samples,
                },
            )
        truth_frames = load_truth_frames(truth_path, dialog_id=dialog_id)
        expected_frames = frame_count_for_samples(pcm.size, base_config.hop_samples)
        if len(truth_frames) != expected_frames:
            raise AedError(
                BusinessCode.E_FRAME_MISMATCH,
                f"truth frame count {len(truth_frames)} does not match feature frame count "
                f"{expected_frames}",
                dialog_id=dialog_id,
                field="frame_count",
                details={
                    "truth_frame_count": len(truth_frames),
                    "actual_frame_count": expected_frames,
                },
            )
        feature_result = extract_features(
            pcm,
            base_config,
            dialog_id=dialog_id,
            truth_frame_count=len(truth_frames),
        )
        for truth_row in truth_frames:
            if truth_row["speaker_id"] is not None:
                speaker_ids.add(truth_row["speaker_id"])
        preflight = detect_candidates(
            feature_result,
            profiles["fine"],
            detection_config=detection_config,
        )
        total_frames += feature_result.frame_count
        total_preflight_candidate_frames += sum(
            candidate.end_frame - candidate.start_frame + 1
            for candidate in preflight.candidates
        )
        contexts.append(
            {
                "row": row,
                "dialog_id": dialog_id,
                "wav_path": wav_path,
                "truth_path": truth_path,
                "pcm": pcm,
                "truth_frames": truth_frames,
                "feature_result": feature_result,
                "preflight_detection": preflight,
            }
        )

    manifest_speaker_counts = [
        int(row["speaker_count"])
        for row in manifest
        if isinstance(row.get("speaker_count"), int) and row["speaker_count"] > 0
    ]
    speaker_count = max(
        len(speaker_ids),
        max(manifest_speaker_counts) if manifest_speaker_counts else 0,
        1,
    )
    observation = ScaleObservation(
        dialog_count=len(contexts),
        total_frames=total_frames,
        avg_candidates_per_frame=round(
            total_preflight_candidate_frames / total_frames if total_frames else 0.0,
            6,
        ),
        speaker_count=speaker_count,
        feature_dimension=base_config.feature_dimension,
        dialog_ids=tuple(context["dialog_id"] for context in contexts),
        recall_gain_estimate=float(recall_gain_estimate),
    )
    decision = decide_path(
        observation,
        constraints,
        detection_config=detection_config,
    )
    executed_path = path_override or decision["selected_path"]
    execution_profile = profiles[executed_path]

    dialog_reports: list[dict[str, Any]] = []
    dialog_metric_records: list[dict[str, Any]] = []
    for context in contexts:
        dialog_id = context["dialog_id"]
        row = context["row"]
        feature_result = context["feature_result"]
        truth_frames = context["truth_frames"]
        pcm = context["pcm"]
        detection = (
            context["preflight_detection"]
            if executed_path == "fine"
            else detect_candidates(
                feature_result,
                execution_profile,
                detection_config=detection_config,
            )
        )
        segments = postprocess_candidates(
            detection.candidates,
            frame_count=feature_result.frame_count,
            duration_samples=pcm.size,
            config=base_config,
            profile=execution_profile,
            min_event_ms=detection_config.min_event_ms,
            min_gap_ms=detection_config.min_gap_ms,
            dialog_id=dialog_id,
        )
        frame_decisions = flatten_frame_decisions(
            dialog_id,
            truth_frames,
            segments,
        )
        dialog_metrics = compute_dialog_metrics(
            dialog_id=dialog_id,
            truth_frames=truth_frames,
            segments=segments,
            frame_decisions=frame_decisions,
            hop_samples=base_config.hop_samples,
            sample_rate=base_config.sample_rate,
            duration_samples=pcm.size,
            overlap_threshold=detection_config.overlap_threshold,
        )
        dialog_metric_records.append(dialog_metrics)

        truth_indices = [int(truth_row["frame_index"]) for truth_row in truth_frames]
        feature_indices = [int(index) for index in feature_result.frame_indices]
        detection_indices = list(range(feature_result.frame_count))
        postprocess_indices = [
            frame_index
            for segment in segments
            for frame_index in range(segment.start_frame, segment.end_frame + 1)
        ]
        segment_dicts = _segment_dicts(segments)
        event_dicts = [segment.to_dict() for segment in segments if segment.is_event]
        dialog_report = {
            "dialog_id": dialog_id,
            "path": executed_path,
            "duration_ms": float(row["duration_ms"]),
            "duration_samples": int(pcm.size),
            "frame_count": feature_result.frame_count,
            "feature": feature_result.metadata(),
            "detection": detection.to_dict(),
            "segments": segment_dicts,
            "events": event_dicts,
            "frame_decisions": frame_decisions,
            "metrics": {
                "frame": dialog_metrics["frame"],
                "event": dialog_metrics["event"],
            },
            "frame_index_checks": {
                "truth_sha256": _frame_indices_sha(truth_indices),
                "feature_sha256": _frame_indices_sha(feature_indices),
                "detection_sha256": _frame_indices_sha(detection_indices),
                "postprocess_sha256": _frame_indices_sha(postprocess_indices),
                "truth_count": len(truth_indices),
                "feature_count": len(feature_indices),
                "detection_count": len(detection_indices),
                "postprocess_count": len(postprocess_indices),
            },
        }
        dialog_reports.append(dialog_report)

    overall_metrics = aggregate_metrics(dialog_metric_records)
    metrics_by_path: dict[str, Any] = {}
    for path_name in ("fine", "approximate"):
        path_records = [
            record for record in dialog_metric_records if record["dialog_id"] in {
                report["dialog_id"]
                for report in dialog_reports
                if report["path"] == path_name
            }
        ]
        if path_records:
            metrics_by_path[path_name] = aggregate_metrics(path_records)

    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "business_code": BusinessCode.OK.value,
        "degraded": executed_path == "approximate",
        "dataset": {
            "directory": str(root),
            "manifest_path": str(manifest_path),
            "manifest_sha256": sha256_file(manifest_path),
            "dialog_count": len(manifest),
        },
        "configuration": {
            "feature": base_config.to_dict(),
            "detection": detection_config.to_dict(),
            "path_profiles": {
                name: profile.to_dict() for name, profile in profiles.items()
            },
        },
        "scale_observation": observation.to_dict(),
        "tradeoff_decision": decision,
        "selected_path": executed_path,
        "auto_selected_path": decision["selected_path"],
        "path_override": path_override,
        "path_by_dialog": {
            report["dialog_id"]: report["path"] for report in dialog_reports
        },
        "metrics": overall_metrics,
        "metrics_by_path": metrics_by_path,
        "dialogs": dialog_reports,
        "errors": [],
    }
    from .consistency import run_self_check

    report["consistency"] = run_self_check(report)
    return report
