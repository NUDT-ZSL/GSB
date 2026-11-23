"""Deterministic exact-versus-approximate algorithm selection."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .model import Dataset
from .util import digest_payload, stable_round


@dataclass(frozen=True)
class ScaleObservation:
    speaker_count: int
    total_frames: int
    controlled_phoneme_count: int
    memory_budget_mb: float
    feature_bytes_per_frame: int = 256
    frame_hop_ms: float = 10.0


def choose_path(
    observation: ScaleObservation,
    max_exact_speakers: int = 8,
    max_exact_frames: int = 20_000,
    max_exact_phonemes: int = 32,
) -> dict[str, Any]:
    exact_bytes = observation.total_frames * observation.feature_bytes_per_frame
    exact_mb = exact_bytes / (1024.0 * 1024.0)
    memory_margin = observation.memory_budget_mb - exact_mb
    coarse_hop = max(1, int(math.ceil(2.0)))
    candidate_k = max(
        1,
        int(math.ceil(observation.controlled_phoneme_count * 0.60)),
    )
    coarse_frames = int(math.ceil(observation.total_frames / coarse_hop))
    exact_comparisons = (
        observation.total_frames * observation.controlled_phoneme_count
    )
    approx_comparisons = (
        coarse_frames * candidate_k
        + observation.controlled_phoneme_count
    )
    reasons: list[str] = []
    if observation.speaker_count > max_exact_speakers:
        reasons.append("speaker_count_exceeds_exact_budget")
    if observation.total_frames > max_exact_frames:
        reasons.append("total_frames_exceeds_exact_budget")
    if observation.controlled_phoneme_count > max_exact_phonemes:
        reasons.append("phoneme_count_exceeds_exact_budget")
    if memory_margin < 0:
        reasons.append("resident_feature_memory_exhausted")
    decision = "exact" if not reasons else "approximate"
    if decision == "exact":
        reasons.append("all_exact_budgets_satisfied")
    selected_comparisons = (
        exact_comparisons if decision == "exact" else approx_comparisons
    )
    payload = {
        "selector_version": "adaptive-v1",
        "observations": {
            "speaker_count": observation.speaker_count,
            "total_frames": observation.total_frames,
            "controlled_phoneme_count": observation.controlled_phoneme_count,
            "memory_budget_mb": stable_round(observation.memory_budget_mb, 6),
            "feature_bytes_per_frame": observation.feature_bytes_per_frame,
            "estimated_exact_resident_mb": stable_round(exact_mb, 6),
            "memory_margin_mb": stable_round(memory_margin, 6),
            "frame_hop_ms": observation.frame_hop_ms,
        },
        "thresholds": {
            "max_exact_speakers": max_exact_speakers,
            "max_exact_frames": max_exact_frames,
            "max_exact_phonemes": max_exact_phonemes,
        },
        "decision": decision,
        "reasons": reasons,
        "path_parameters": {
            "frame_hop_ms": observation.frame_hop_ms
            if decision == "exact"
            else observation.frame_hop_ms * 2.0,
            "coarse_hop_frames": 1 if decision == "exact" else coarse_hop,
            "candidate_count_per_anchor": observation.controlled_phoneme_count
            if decision == "exact"
            else candidate_k,
            "similarity_mode": "full_exact" if decision == "exact" else "candidate_pruned_block",
            "normalization_mode": "exact_l2" if decision == "exact" else "block_l2_estimate",
        },
        "comparison_estimate": {
            "exact": exact_comparisons,
            "approximate": approx_comparisons,
            "selected": selected_comparisons,
            "reduction_ratio": stable_round(
                1.0 - selected_comparisons / max(1, exact_comparisons), 12
            ),
        },
        "phoneme_coverage": {
            "required": observation.controlled_phoneme_count,
            "candidate_union_size": observation.controlled_phoneme_count,
            "missing": [],
            "guaranteed": True,
            "policy": "candidate_union_must_equal_controlled_set",
        },
    }
    payload["decision_digest"] = digest_payload(payload)
    return payload


def select_for_dataset(
    dataset: Dataset,
    memory_budget_mb: float = 64.0,
) -> dict[str, Any]:
    observation = ScaleObservation(
        speaker_count=len(dataset.speakers),
        total_frames=dataset.total_frames,
        controlled_phoneme_count=len(dataset.controlled_phonemes),
        memory_budget_mb=memory_budget_mb,
        frame_hop_ms=dataset.frame_hop_ms,
    )
    return choose_path(observation)
