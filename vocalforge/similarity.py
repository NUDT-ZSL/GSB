"""Clone similarity scoring, stable threshold decisions, and path arbitration."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .errors import AppError
from .model import (
    AlignmentFrame,
    FeatureFrame,
    PrecisionConfig,
    Utterance,
)
from .precision import NumericOps, stable_quantize, stable_threshold_decision
from .util import digest_payload, quantile_summary, stable_round


def frame_embedding(
    frames: tuple[FeatureFrame, ...],
    ops: NumericOps,
    sampled_indices: tuple[int, ...] | None = None,
) -> tuple[float, ...]:
    selected = (
        frames
        if sampled_indices is None
        else tuple(frames[index] for index in sampled_indices if 0 <= index < len(frames))
    )
    if not selected:
        return tuple(float(v) for v in ops.normalize([0.0] * 12))
    matrix = np.asarray([frame.timbre_vector for frame in selected], dtype=np.float64)
    mean = np.mean(matrix, axis=0)
    std_selected = np.std(matrix[:, :4], axis=0)
    vector = ops.cast(np.concatenate([mean, std_selected]))
    return tuple(float(v) for v in vector)


def clone_similarity(
    utterance_frames: tuple[FeatureFrame, ...],
    reference_normalized: tuple[float, ...],
    ops: NumericOps,
    sampled_indices: tuple[int, ...] | None = None,
) -> float:
    utterance_vector = ops.cast(frame_embedding(utterance_frames, ops, sampled_indices))
    utterance_normalized = ops.normalize(utterance_vector)
    reference = ops.normalize(reference_normalized)
    similarity = ops.dot(utterance_normalized, reference)
    if not math.isfinite(similarity):
        raise AppError(
            "E_PATH_DRIFT",
            "Similarity became non-finite during accumulation.",
            stage="similarity.score",
            field="similarity",
        )
    return stable_round(max(-1.0, min(1.0, similarity)), 12)


def score_utterances(
    utterances: tuple[Utterance, ...],
    frames_by_utterance: dict[str, tuple[FeatureFrame, ...]],
    reference_vectors: dict[int, tuple[float, ...]],
    ops: NumericOps,
    sampled_indices_by_utterance: dict[str, tuple[int, ...]] | None = None,
) -> dict[str, float]:
    scores: dict[str, float] = {}
    for utterance in sorted(utterances, key=lambda item: item.utterance_id):
        reference = reference_vectors.get(utterance.speaker_id)
        if reference is None:
            raise AppError(
                "E_REF_MISSING",
                "No reference embedding is available for similarity scoring.",
                stage="similarity.score",
                field="ref_id",
                details={
                    "utterance_id": utterance.utterance_id,
                    "speaker_id": utterance.speaker_id,
                },
            )
        sampled = (
            sampled_indices_by_utterance.get(utterance.utterance_id)
            if sampled_indices_by_utterance is not None
            else None
        )
        scores[utterance.utterance_id] = clone_similarity(
            frames_by_utterance[utterance.utterance_id],
            reference,
            ops,
            sampled,
        )
    return scores


def topk_and_threshold(
    scores: dict[str, float],
    utterance_by_id: dict[str, Utterance],
    config: PrecisionConfig,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for utterance_id, similarity in scores.items():
        utterance = utterance_by_id[utterance_id]
        decision = stable_threshold_decision(similarity, config)
        rows.append(
            {
                "utterance_id": utterance_id,
                "speaker_id": utterance.speaker_id,
                "ref_id": utterance.ref_id,
                "similarity": stable_round(similarity, 12),
                "similarity_quantized": decision["similarity_quantized"],
                "accepted": decision["accepted"],
                "relation": decision["relation"],
                "threshold_distance": decision["distance"],
                "within_tolerance": decision["within_tolerance"],
                "strategy": decision["strategy"],
            }
        )
    rows.sort(
        key=lambda row: (
            -float(row["similarity_quantized"]),
            row["utterance_id"],
            row["speaker_id"],
        )
    )
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    top_k = rows[: config.top_k]
    decisions = {row["utterance_id"]: bool(row["accepted"]) for row in rows}
    payload = {
        "threshold": config.threshold,
        "top_k_requested": config.top_k,
        "strategy": config.comparison_strategy,
        "decision_quantum": config.decision_quantum,
        "tolerance": config.tolerance,
        "rows": rows,
        "top_k": top_k,
        "accepted_utterance_ids": [
            row["utterance_id"] for row in rows if row["accepted"]
        ],
        "decisions": decisions,
    }
    payload["topk_digest"] = digest_payload(top_k)
    return payload


def compare_paths(
    exact_alignment: dict[str, tuple[AlignmentFrame, ...]],
    approximate_alignment: dict[str, tuple[AlignmentFrame, ...]],
    exact_scores: dict[str, float],
    approximate_scores: dict[str, float],
    config: PrecisionConfig,
) -> dict[str, Any]:
    common_ids = sorted(set(exact_scores) & set(approximate_scores))
    if not common_ids:
        raise AppError(
            "E_PATH_DRIFT",
            "Exact and approximate paths have no comparable utterances.",
            stage="similarity.path_compare",
            details={"source": "empty_intersection"},
        )
    differences: list[float] = []
    decisions_agree = 0
    comparable = 0
    for utterance_id in common_ids:
        exact = exact_scores[utterance_id]
        approximate = approximate_scores[utterance_id]
        if not (math.isfinite(exact) and math.isfinite(approximate)):
            raise AppError(
                "E_PATH_DRIFT",
                "Similarity is non-finite in one path.",
                stage="similarity.path_compare",
                details={
                    "source": "non_finite_similarity",
                    "utterance_id": utterance_id,
                },
            )
        difference = abs(exact - approximate)
        differences.append(difference)
        if difference <= max(
            config.path_similarity_tolerance, config.decision_quantum
        ):
            comparable += 1
        exact_decision = stable_threshold_decision(exact, config)["accepted"]
        approximate_decision = stable_threshold_decision(approximate, config)["accepted"]
        if exact_decision == approximate_decision:
            decisions_agree += 1
    agreement_rate = comparable / len(common_ids)
    decision_agreement_rate = decisions_agree / len(common_ids)

    label_mismatches = 0
    total_frames = 0
    offsets: list[int] = []
    per_utterance: list[dict[str, Any]] = []
    for utterance_id in common_ids:
        exact_rows = exact_alignment.get(utterance_id, ())
        approx_rows = approximate_alignment.get(utterance_id, ())
        frame_count = min(len(exact_rows), len(approx_rows))
        mismatch = 0
        numeric_offsets: list[int] = []
        for index in range(frame_count):
            exact_label = exact_rows[index].phoneme_id
            approx_label = approx_rows[index].phoneme_id
            if exact_label != approx_label:
                mismatch += 1
                numeric_offsets.append(abs(exact_label - approx_label))
                offsets.append(abs(exact_label - approx_label))
        label_mismatches += mismatch
        total_frames += frame_count
        per_utterance.append(
            {
                "utterance_id": utterance_id,
                "frame_count": frame_count,
                "mismatch_frames": mismatch,
                "mismatch_rate": stable_round(
                    mismatch / max(1, frame_count), 12
                ),
                "max_label_offset": max(numeric_offsets) if numeric_offsets else 0,
            }
        )
    alignment_mismatch_rate = label_mismatches / max(1, total_frames)
    confidence_deviation = quantile_summary(differences)
    if agreement_rate < config.min_similarity_agreement:
        raise AppError(
            "E_PATH_DRIFT",
            "Exact and approximate paths are not comparable under the configured policy.",
            stage="similarity.path_compare",
            details={
                "source": "similarity_agreement_below_minimum",
                "agreement_rate": stable_round(agreement_rate, 12),
                "minimum": config.min_similarity_agreement,
                "confidence_deviation": confidence_deviation,
                "alignment_mismatch_rate": stable_round(alignment_mismatch_rate, 12),
            },
        )
    payload = {
        "comparable_utterances": len(common_ids),
        "similarity_agreement_rate": stable_round(agreement_rate, 12),
        "threshold_decision_agreement_rate": stable_round(
            decision_agreement_rate, 12
        ),
        "alignment_offset": {
            "mismatch_frames": label_mismatches,
            "total_frames": total_frames,
            "mismatch_rate": stable_round(alignment_mismatch_rate, 12),
            "mean_label_offset": stable_round(
                float(np.mean(offsets)) if offsets else 0.0, 12
            ),
            "max_label_offset": max(offsets) if offsets else 0,
            "per_utterance": per_utterance,
        },
        "confidence_deviation_spectrum": confidence_deviation,
        "per_utterance_similarity_delta": [
            {
                "utterance_id": utterance_id,
                "exact": stable_round(exact_scores[utterance_id], 12),
                "approximate": stable_round(approximate_scores[utterance_id], 12),
                "delta": stable_round(
                    abs(exact_scores[utterance_id] - approximate_scores[utterance_id]),
                    12,
                ),
            }
            for utterance_id in common_ids
        ],
        "verdict": "comparable",
    }
    payload["consistency_digest"] = digest_payload(payload)
    return payload


def exact_topk_signature(topk_payload: dict[str, Any]) -> tuple[str, ...]:
    return tuple(row["utterance_id"] for row in topk_payload["top_k"])
