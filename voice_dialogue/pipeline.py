"""End-to-end orchestration with deterministic JSON reporting."""

from __future__ import annotations

import json
import os

import numpy as np

from .config import ComponentConfig
from .consistency import (
    check_frame_index_sets,
    check_segment_tiling,
    check_speaker_contract,
    recompute_aggregation,
)
from .dataio import (
    load_ground_truth,
    load_manifest,
    load_pcm,
    validate_manifest_row,
    verify_audio_frames,
)
from .diarization import separate_speakers
from .emotion import aggregate_dialogue, score_segments
from .errors import BusinessError, ErrorCode
from .features import extract_features
from .selector import decide_path, observe_scale
from .snapshot import build_snapshot, load_snapshot, save_snapshot

PATH_DEFINITIONS = {
    "fine": {
        "feature_dim": 8,
        "frame_stride": 1,
        "separation": "iterative_lloyd",
        "regression": "coordinate_descent_ridge",
    },
    "approximate": {
        "feature_dim": 4,
        "frame_stride": "adaptive>=2",
        "separation": "single_pass_nearest_prototype",
        "regression": "closed_form_ridge",
    },
}


def _round_floats(obj, digits: int = 10):
    if isinstance(obj, dict):
        return {k: _round_floats(v, digits) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round_floats(v, digits) for v in obj]
    if isinstance(obj, (np.floating, float)):
        return round(float(obj), digits)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, np.ndarray):
        return _round_floats(obj.tolist(), digits)
    return obj


def _segment_to_dict(score) -> dict:
    return {
        "segment_index": score.segment_index,
        "speaker_id": score.speaker_id,
        "start_frame": score.start_frame,
        "end_frame": score.end_frame,
        "duration_frames": score.duration_frames,
        "valence": score.valence,
        "arousal": score.arousal,
        "valence_ci": score.valence_ci,
        "arousal_ci": score.arousal_ci,
    }


def process_dialog(row: dict, base_dir: str, config: ComponentConfig,
                   decision) -> dict:
    dialog_id = row["dialog_id"]
    pcm = load_pcm(os.path.join(base_dir, row["pcm_path"]), dialog_id)
    gt = load_ground_truth(os.path.join(base_dir, row["gt_path"]), dialog_id)
    verify_audio_frames(row, pcm, gt, config)

    feature_result = extract_features(
        pcm=pcm,
        sample_rate=config.sample_rate,
        frame_ms=config.frame_ms,
        hop_ms=config.hop_ms,
        dim=decision.feature_dim,
        expected_frames=int(row["num_frames"]),
        frame_stride=decision.frame_stride,
        dialog_id=dialog_id,
    )

    # Resident feature budget is enforced on the full reconstructed matrix.
    resident = feature_result.features.shape[0] * feature_result.features.shape[1]
    if resident > config.resource_limits.max_resident_frames:
        if config.resource_limits.on_exceed == "error":
            raise BusinessError(
                ErrorCode.E_RESOURCE_LIMIT,
                "resident feature matrix exceeds budget",
                dialog_id=dialog_id,
                field="max_resident_frames",
                details={"resident": resident,
                         "budget": config.resource_limits.max_resident_frames},
            )

    diarization = separate_speakers(
        features=feature_result.features,
        frame_indices=feature_result.frame_indices,
        expected_speaker_ids=row["speaker_ids"],
        path=decision.path,
        fine_iterations=config.fine_iterations,
        comparison_budget=config.resource_limits.max_separation_comparisons,
        dialog_id=dialog_id,
    )

    scores, regression_terms = score_segments(
        features=feature_result.features,
        diarization=diarization,
        gt_valence=gt["valence"].astype(np.float64),
        gt_arousal=gt["arousal"].astype(np.float64),
        path=decision.path,
        confidence_z=config.confidence_z,
        term_budget=config.resource_limits.max_regression_terms,
        iterations=config.fine_iterations,
        dialog_id=dialog_id,
    )
    aggregation = aggregate_dialogue(scores)

    # Cross-stage consistency self-checks.
    check_frame_index_sets(
        feature_result, gt, diarization.labels, dialog_id
    )
    check_speaker_contract(
        diarization.labels, gt, row["speaker_ids"], dialog_id
    )
    check_segment_tiling(
        diarization.segments, int(row["num_frames"]), dialog_id
    )
    recompute_aggregation(scores, aggregation, dialog_id)

    return {
        "dialog_id": dialog_id,
        "status": "ok",
        "business_code": "OK",
        "path": decision.path,
        "frame_count": int(row["num_frames"]),
        "feature": {
            "dim": feature_result.dim,
            "frame_stride": feature_result.frame_stride,
            "resident_peak_frames": feature_result.resident_frames,
            "resident_cells": int(resident),
            "names": list(feature_result.feature_names),
        },
        "diarization": {
            "iterations": diarization.iterations,
            "comparisons": diarization.comparisons,
            "cluster_count": diarization.cluster_count,
            "switch_timeline": diarization.switches,
            "frame_labels": diarization.labels.astype(int).tolist(),
        },
        "regression_terms": int(regression_terms),
        "degradation": list(decision.degradation),
        "segments": [_segment_to_dict(s) for s in scores],
        "aggregation": aggregation,
    }


def run_pipeline(
    manifest_path: str,
    config: ComponentConfig | None = None,
    report_path: str | None = None,
    snapshot_dir: str | None = None,
) -> dict:
    config = config or ComponentConfig()
    config.validate()
    base_dir = os.path.dirname(os.path.abspath(manifest_path))
    rows = load_manifest(manifest_path)
    for row in rows:
        validate_manifest_row(row, config)

    observation = observe_scale(rows, config)
    decision = decide_path(observation, config)

    dialogs = []
    for row in rows:
        dialogs.append(process_dialog(row, base_dir, config, decision))

    report = {
        "status": "ok",
        "business_code": "OK",
        "config": config.to_dict(),
        "scale_observation": observation.to_dict(),
        "path_decision": decision.to_dict(),
        "path_definitions": PATH_DEFINITIONS,
        "dialogs": dialogs,
    }
    report = _round_floats(report)

    if snapshot_dir is not None:
        snapshot = build_snapshot(
            version=config.snapshot_version,
            config_dict=config.to_dict(),
            path_definitions=PATH_DEFINITIONS,
            decision_dict=decision.to_dict(),
        )
        save_snapshot(snapshot_dir, snapshot)
        loaded = load_snapshot(snapshot_dir)
        report["snapshot"] = {
            "dir": os.path.abspath(snapshot_dir),
            "version": loaded["version"],
            "checksum": loaded["checksum"],
        }

    if report_path is not None:
        os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
        with open(report_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(report, fh, sort_keys=True, ensure_ascii=False, indent=2)
            fh.write("\n")
    return report
