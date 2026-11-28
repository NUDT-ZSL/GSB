"""End-to-end normalization, animation synthesis, validation and export."""

from __future__ import annotations

import hashlib
import math
import shutil
from pathlib import Path
from typing import Any

from .catalog import CANONICAL_CHANNELS
from .cross_version import compare_versions, curve_sha256
from .errors import fail
from .export import build_export_payloads, keyframe_sha256, write_curve_artifacts
from .io_utils import (
    clean_nested,
    json_bytes,
    prepare_staging,
    replace_directory,
    write_json,
)
from .mapping import build_mapping_table, mapping_digest_payload
from .models import FrameCurves, TruthData, VersionData
from .parser import infer_ground_truth_path, parse_versions
from .smoothing import (
    EMOTION_ANCHOR_TOLERANCE_US,
    FRAME_LIP_SIGMA_SECONDS,
    build_smooth_track,
    resample_frames,
)
from .truth import fallback_truth, parse_truth
from .validation import validate_version


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_phoneme_identity(
    versions: list[VersionData], truth: TruthData
) -> None:
    baseline = versions[0]
    baseline_by_id = {item.id: item for item in baseline.phonemes()}
    truth_by_id = {item.id: item for item in truth.phonemes()}
    missing_in_truth = sorted(set(baseline_by_id) - set(truth_by_id))
    missing_in_version = sorted(set(truth_by_id) - set(baseline_by_id))
    if missing_in_truth:
        phoneme_id = missing_in_truth[0]
        fail(
            "E_MISSING_PHONEME",
            "ground truth omits a phoneme present in the project",
            version=baseline.version,
            phoneme_id=phoneme_id,
            missing_count=len(missing_in_truth),
        )
    if missing_in_version:
        phoneme_id = missing_in_version[0]
        fail(
            "E_MISSING_PHONEME",
            "project omits a phoneme present in ground truth",
            version=baseline.version,
            phoneme_id=phoneme_id,
            missing_count=len(missing_in_version),
        )
    truth_sentence_ids = {sentence.id for sentence in truth.sentences}
    for version in versions:
        version_sentence_ids = {sentence.id for sentence in version.sentences}
        if version_sentence_ids != truth_sentence_ids:
            missing_sentences = sorted(truth_sentence_ids - version_sentence_ids)
            extra_sentences = sorted(version_sentence_ids - truth_sentence_ids)
            fail(
                "E_SENTENCE_IDENTITY",
                "project sentence ids do not match ground truth",
                version=version.version,
                sentence_id=(missing_sentences or extra_sentences or [None])[0],
                missing_sentences=missing_sentences,
                extra_sentences=extra_sentences,
            )
    for version in versions[1:]:
        version_by_id = {item.id: item for item in version.phonemes()}
        missing = sorted(set(truth_by_id) - set(version_by_id))
        extra = sorted(set(version_by_id) - set(truth_by_id))
        if missing:
            phoneme_id = missing[0]
            sentence_id = None
            for sentence in truth.sentences:
                if any(item.id == phoneme_id for item in sentence.phonemes):
                    sentence_id = sentence.id
                    break
            fail(
                "E_MISSING_PHONEME",
                "project version omits a ground-truth phoneme",
                version=version.version,
                sentence_id=sentence_id,
                phoneme_id=phoneme_id,
                missing_count=len(missing),
            )
        if extra:
            fail(
                "E_EXTRA_PHONEME",
                "project version contains a phoneme absent from ground truth",
                version=version.version,
                phoneme_id=extra[0],
                extra_count=len(extra),
            )


def _normalized_timeline(version: VersionData) -> dict[str, Any]:
    sentences = []
    for sentence in version.sentences:
        phonemes = []
        for phoneme in sentence.phonemes:
            row = phoneme.to_dict()
            if version.time_base == "frames" and version.source_fps:
                row["start_frame"] = round(
                    phoneme.start_us * version.source_fps / 1_000_000.0, 6
                )
                row["end_frame"] = round(
                    phoneme.end_us * version.source_fps / 1_000_000.0, 6
                )
            phonemes.append(row)
        sentences.append(
            {
                "id": sentence.id,
                "text": sentence.text,
                "start_us": sentence.start_us,
                "end_us": sentence.end_us,
                "start_ms": round(sentence.start_us / 1000.0, 6),
                "end_ms": round(sentence.end_us / 1000.0, 6),
                "emotion": {
                    "label": sentence.emotion_label,
                    "intensity": sentence.emotion_intensity,
                    "anchor_phoneme_id": sentence.emotion_anchor_id,
                },
                "stress": [
                    {"phoneme_id": item.phoneme_id, "level": item.level}
                    for item in sentence.stress
                ],
                "phonemes": phonemes,
            }
        )
    return {
        "format": "museum-lipsync-normalized-timeline/v1",
        "version": version.version,
        "source_annotation": str(version.annotation_path),
        "source_bindings": str(version.bindings_path),
        "source_time_base": version.time_base,
        "source_fps": version.source_fps,
        "canonical_time_base": "microseconds",
        "audio_sample_rate_hz": version.audio_sample_rate,
        "audio_duration_ms": round(version.audio_duration_us / 1000.0, 6),
        "duration_ms": round(version.duration_us / 1000.0, 6),
        "channel_sources": dict(version.channel_sources),
        "channels": list(CANONICAL_CHANNELS),
        "sentences": sentences,
    }


def _truth_comparison_issues(
    versions: list[VersionData], truth: TruthData
) -> list[dict[str, Any]]:
    truth_by_id = {item.id: item for item in truth.phonemes()}
    issues: list[dict[str, Any]] = []
    for version in versions:
        for phoneme in version.phonemes():
            truth_phoneme = truth_by_id.get(phoneme.id)
            if truth_phoneme is None:
                continue
            comparisons = {
                "canonical": (phoneme.canonical, truth_phoneme.canonical),
                "viseme": (phoneme.viseme, truth_phoneme.viseme),
                "targets": (phoneme.targets, truth_phoneme.targets),
            }
            for field, (left, right) in comparisons.items():
                if left != right:
                    issues.append(
                        {
                            "code": "TRUTH_MAPPING_MISMATCH",
                            "version": version.version,
                            "phoneme_id": phoneme.id,
                            "field": field,
                            "normalized_value": left,
                            "truth_value": right,
                        }
                    )
    return issues


def _keyframe_differences(
    baseline: dict[str, Any], payload: dict[str, Any], version: str
) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    if baseline["frame_count"] != payload["frame_count"]:
        differences.append(
            {
                "kind": "keyframe_count",
                "version": version,
                "baseline_value": baseline["frame_count"],
                "version_value": payload["frame_count"],
            }
        )
    if baseline["channel_order"] != payload["channel_order"]:
        differences.append(
            {
                "kind": "keyframe_channel_order",
                "version": version,
                "baseline_value": baseline["channel_order"],
                "version_value": payload["channel_order"],
            }
        )
    frame_count = min(len(baseline["keyframes"]), len(payload["keyframes"]))
    for index in range(frame_count):
        left = baseline["keyframes"][index]
        right = payload["keyframes"][index]
        if left["values"] != right["values"]:
            for channel_index, channel in enumerate(baseline["channel_order"]):
                if left["values"][channel_index] != right["values"][channel_index]:
                    differences.append(
                        {
                            "kind": "keyframe",
                            "version": version,
                            "channel": channel,
                            "frame": index,
                            "time_seconds": left["time"],
                            "baseline_value": left["values"][channel_index],
                            "version_value": right["values"][channel_index],
                            "delta": right["values"][channel_index]
                            - left["values"][channel_index],
                        }
                    )
    return differences


def run_pipeline(
    version_dirs: list[str | Path],
    *,
    fps: float = 30.0,
    threshold: float = 0.05,
    output_dir: str | Path = "outputs/face_animation",
    ground_truth_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run the full local pipeline and return a concise execution summary."""
    if not math.isfinite(float(fps)) or float(fps) <= 0.0:
        fail("E_SAMPLE_RATE_MISMATCH", "target fps must be finite and positive", fps=fps)
    if not math.isfinite(float(threshold)) or float(threshold) < 0.0:
        fail("E_INVALID_THRESHOLD", "threshold must be finite and non-negative", threshold=threshold)
    destination = Path(output_dir).expanduser().resolve()

    versions = parse_versions(list(version_dirs))
    if ground_truth_path is not None:
        truth = parse_truth(ground_truth_path)
    else:
        inferred = infer_ground_truth_path(list(version_dirs))
        truth = parse_truth(inferred) if inferred is not None else fallback_truth(versions[0])
    _check_phoneme_identity(versions, truth)

    curve_by_version: dict[str, FrameCurves] = {}
    track_by_version: dict[str, Any] = {}
    for version in versions:
        track = build_smooth_track(version)
        track_by_version[version.version] = track
        curve_by_version[version.version] = resample_frames(track, float(fps))

    baseline = versions[0]
    baseline_curves = curve_by_version[baseline.version]
    reports: dict[str, dict[str, Any]] = {}
    ledgers: dict[str, dict[str, Any]] = {}
    for version in versions:
        report, ledger = validate_version(
            version,
            curve_by_version[version.version],
            truth,
            versions,
            threshold=float(threshold),
        )
        reports[version.version] = report
        ledgers[version.version] = ledger

    mapping = build_mapping_table(versions)
    mapping_issues = [
        {
            "code": "NORMALIZATION_TARGET_MISMATCH",
            "canonical": entry["canonical"],
            "viseme": entry["viseme"],
        }
        for entry in mapping["phonemes"]
        if not entry["normalization_consistent"]
    ]
    mapping_issues.extend(_truth_comparison_issues(versions, truth))
    mapping_sha = hashlib.sha256(
        json_bytes(mapping_digest_payload(mapping))
    ).hexdigest()

    cross_version = compare_versions(versions, curve_by_version, fps=float(fps))
    export_payloads = {
        version: build_export_payloads(curve_by_version[version])
        for version in curve_by_version
    }
    baseline_keyframes = export_payloads[baseline.version][1]
    keyframe_differences: list[dict[str, Any]] = []
    for version in versions:
        payload = export_payloads[version.version][1]
        if version.version != baseline.version:
            keyframe_differences.extend(
                _keyframe_differences(
                    baseline_keyframes, payload, version.version
                )
            )
        for row in cross_version["versions"]:
            if row["version"] == version.version:
                row["keyframe_sha256"] = keyframe_sha256(payload)
                row["mapping_sha256"] = mapping_sha
    cross_version["keyframe_differences"] = keyframe_differences
    cross_version["identical"] = bool(
        cross_version["identical"]
        and not keyframe_differences
        and not mapping_issues
    )
    cross_version["mapping_issues"] = mapping_issues
    cross_version["mapping_sha256"] = mapping_sha

    baseline_report = dict(reports[baseline.version])
    baseline_report["all_versions"] = {
        version: {
            "passed": reports[version]["passed"],
            "max_channel_deviation": reports[version]["max_channel_deviation"],
            "first_exceeded_sentence_id": (
                reports[version]["first_exceeded"]["sentence_id"]
                if reports[version]["first_exceeded"]
                else None
            ),
        }
        for version in reports
    }
    emotion_conflicts = [
        row for row in cross_version["emotion_conflicts"] if row["version"] == baseline.version
    ]
    emotion_payload = {
        "format": "museum-lipsync-emotion-conflicts/v1",
        "policy": "stress_over_emotion",
        "conflict_count": len(emotion_conflicts),
        "conflicts": emotion_conflicts,
        "by_version": {
            version: [
                row
                for row in cross_version["emotion_conflicts"]
                if row["version"] == version
            ]
            for version in curve_by_version
        },
    }
    difference_list = {
        "format": "museum-lipsync-difference-list/v1",
        "cross_version": {
            "identical": cross_version["identical"],
            "max_abs_deviation": cross_version["max_abs_deviation"],
            "curve_difference_count": len(cross_version["curve_differences"]),
            "keyframe_difference_count": len(cross_version["keyframe_differences"]),
            "normalization_difference_count": len(
                cross_version["normalization_differences"]
            ),
            "curve_differences": cross_version["curve_differences"],
            "keyframe_differences": cross_version["keyframe_differences"],
            "normalization_differences": cross_version[
                "normalization_differences"
            ],
            "report": "cross_version_diff.json",
        },
        "emotion_conflicts": emotion_conflicts,
        "mapping_issues": mapping_issues,
    }

    normalization = {
        version.version: clean_nested(_normalized_timeline(version))
        for version in versions
    }
    overall_status = (
        "pass"
        if baseline_report["passed"] and cross_version["identical"]
        else "review_required"
    )
    summary = {
        "format": "museum-lipsync-summary/v1",
        "status": overall_status,
        "fps": float(fps),
        "threshold": float(threshold),
        "versions": [version.version for version in versions],
        "baseline_version": baseline.version,
        "frame_count": baseline_curves.frame_count,
        "sentence_count": len(truth.sentences),
        "phoneme_count": len(truth.phonemes()),
        "target_count": sum(
            1
            for phoneme in truth.phonemes()
            if phoneme.viseme != "SIL"
        ),
        "sync_passed": baseline_report["passed"],
        "first_exceeded_sentence": (
            baseline_report["first_exceeded"]["sentence_id"]
            if baseline_report["first_exceeded"]
            else None
        ),
        "cross_version_identical": cross_version["identical"],
        "curve_sha256": curve_sha256(baseline_curves),
        "keyframe_sha256": keyframe_sha256(baseline_keyframes),
        "mapping_sha256": mapping_sha,
        "truth_source": truth.source,
        "algorithm": {
            "internal_sample_rate_hz": 1000,
            "coarticulation_half_width_ratio": 0.20,
            "coarticulation_max_duration_ratio_per_side": 0.35,
            "frame_lip_lowpass_sigma_seconds": FRAME_LIP_SIGMA_SECONDS,
            "frame_lip_lowpass_channels": ["jaw_open", "lip_width", "mouth_corner"],
            "emotion_conflict_policy": "stress_over_emotion",
            "conflict_tolerance_ms": EMOTION_ANCHOR_TOLERANCE_US // 1000,
        },
    }

    staging = prepare_staging(destination)
    try:
        write_json(staging / "normalized_timeline.json", normalization[baseline.version])
        write_json(staging / "phoneme_mapping.json", mapping)
        write_json(staging / "sentence_ledger.json", ledgers[baseline.version])
        write_json(staging / "sync_report.json", baseline_report)
        write_json(staging / "emotion_conflicts.json", emotion_payload)
        write_json(staging / "cross_version_diff.json", cross_version)
        write_json(staging / "difference_list.json", difference_list)
        write_json(staging / "summary.json", summary)

        root_artifacts = write_curve_artifacts(staging, baseline_curves)
        per_version_artifacts: dict[str, Any] = {}
        for version in versions:
            directory = staging / "versions" / version.version
            write_json(directory / "normalized_timeline.json", normalization[version.version])
            write_json(directory / "sentence_ledger.json", ledgers[version.version])
            write_json(directory / "sync_report.json", reports[version.version])
            artifacts = write_curve_artifacts(directory, curve_by_version[version.version])
            per_version_artifacts[version.version] = artifacts

        files = {
            "normalized_timeline": "normalized_timeline.json",
            "phoneme_mapping": "phoneme_mapping.json",
            "sentence_ledger": "sentence_ledger.json",
            "sync_report": "sync_report.json",
            "emotion_conflicts": "emotion_conflicts.json",
            "cross_version_diff": "cross_version_diff.json",
            "difference_list": "difference_list.json",
            "summary": "summary.json",
            **{key: Path(value).name for key, value in root_artifacts.items()},
        }
        output_manifest = {
            "format": "museum-lipsync-output-manifest/v1",
            "status": overall_status,
            "fps": float(fps),
            "threshold": float(threshold),
            "versions": [version.version for version in versions],
            "files": files,
            "file_sha256": {
                name: _file_sha256(staging / name) for name in files.values()
            },
            "player_entry": "face_animation.json",
            "curve_entry": "channel_curves.json",
            "per_version": {
                version: {
                    "normalized_timeline": f"versions/{version}/normalized_timeline.json",
                    "sentence_ledger": f"versions/{version}/sentence_ledger.json",
                    "sync_report": f"versions/{version}/sync_report.json",
                    "face_animation": f"versions/{version}/face_animation.json",
                    "channel_curves": f"versions/{version}/channel_curves.json",
                }
                for version in curve_by_version
            },
        }
        write_json(staging / "manifest.json", output_manifest)
        replace_directory(staging, destination)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return {
        **summary,
        "output_dir": str(destination),
        "first_exceeded": baseline_report["first_exceeded"],
        "emotion_conflict_count": len(emotion_conflicts),
        "mapping_issue_count": len(mapping_issues),
    }
