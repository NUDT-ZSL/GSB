"""Cross-stage consistency self-checks and independent recomputation."""

from __future__ import annotations

import numpy as np

from .errors import BusinessError, ErrorCode


def check_frame_index_sets(feature_result, gt: np.ndarray, labels: np.ndarray,
                           dialog_id: str) -> None:
    gt_idx = gt["frame_index"].astype(np.int64)
    if not np.array_equal(feature_result.frame_indices, gt_idx):
        raise BusinessError(
            ErrorCode.E_INCONSISTENT,
            "feature frame indices differ from ground truth",
            dialog_id=dialog_id,
            details={"category": "frame_index_set"},
        )
    if labels.shape[0] != gt_idx.shape[0]:
        raise BusinessError(
            ErrorCode.E_INCONSISTENT,
            "diarization label length differs from frame count",
            dialog_id=dialog_id,
            details={"category": "frame_index_set"},
        )


def check_speaker_contract(labels: np.ndarray, gt: np.ndarray,
                           expected_speakers: list, dialog_id: str) -> None:
    speech = gt["is_speech"].astype(bool)
    gt_speakers = set(
        int(x) for x in np.unique(gt["speaker_id"][speech]).tolist()
    )
    pred_speakers = set(
        int(x) for x in np.unique(labels[speech]).tolist() if int(x) >= 0
    )
    expected = set(int(s) for s in expected_speakers)
    if pred_speakers - expected:
        raise BusinessError(
            ErrorCode.E_SPEAKER_MISMATCH,
            f"phantom speakers {sorted(pred_speakers - expected)}",
            dialog_id=dialog_id,
        )
    if expected - pred_speakers:
        raise BusinessError(
            ErrorCode.E_SPEAKER_UNASSIGNED,
            f"speakers never assigned {sorted(expected - pred_speakers)}",
            dialog_id=dialog_id,
        )
    if gt_speakers != expected:
        raise BusinessError(
            ErrorCode.E_INCONSISTENT,
            "ground-truth speaker set differs from manifest registration",
            dialog_id=dialog_id,
            details={"category": "speaker_set"},
        )


def check_segment_tiling(segments: list, n_frames: int, dialog_id: str) -> None:
    covered = np.zeros(n_frames, dtype=bool)
    prev_end = 0
    for seg in segments:
        start, end = seg["start_frame"], seg["end_frame"]
        if start < prev_end:
            raise BusinessError(
                ErrorCode.E_INCONSISTENT,
                "segment starts before the previous segment ended",
                dialog_id=dialog_id,
                details={
                    "category": "segment_boundary",
                    "start": start,
                    "expected": prev_end,
                },
            )
        if covered[start:end].any():
            raise BusinessError(
                ErrorCode.E_INCONSISTENT,
                "overlapping segments detected",
                dialog_id=dialog_id,
                details={"category": "segment_overlap"},
            )
        covered[start:end] = True
        prev_end = end


def recompute_aggregation(segments_scores: list, aggregation: dict,
                          dialog_id: str, tol: float = 1e-12) -> None:
    """Independently recompute dialogue aggregates from segment details."""
    total = sum(s.duration_frames for s in segments_scores)
    valence = sum(s.valence * s.duration_frames for s in segments_scores) / total
    arousal = sum(s.arousal * s.duration_frames for s in segments_scores) / total
    v_low = sum(s.valence_ci[0] * s.duration_frames for s in segments_scores) / total
    v_high = sum(s.valence_ci[1] * s.duration_frames for s in segments_scores) / total
    a_low = sum(s.arousal_ci[0] * s.duration_frames for s in segments_scores) / total
    a_high = sum(s.arousal_ci[1] * s.duration_frames for s in segments_scores) / total
    checks = {
        "valence": (valence, aggregation["valence"]),
        "arousal": (arousal, aggregation["arousal"]),
        "valence_ci_low": (v_low, aggregation["valence_ci"][0]),
        "valence_ci_high": (v_high, aggregation["valence_ci"][1]),
        "arousal_ci_low": (a_low, aggregation["arousal_ci"][0]),
        "arousal_ci_high": (a_high, aggregation["arousal_ci"][1]),
    }
    for name, (recomputed, stored) in checks.items():
        if abs(recomputed - stored) > tol:
            raise BusinessError(
                ErrorCode.E_INCONSISTENT,
                f"aggregation {name} cannot be recomputed from segments",
                dialog_id=dialog_id,
                details={
                    "category": "aggregation_recompute",
                    "metric": name,
                    "recomputed": recomputed,
                    "stored": stored,
                },
            )
