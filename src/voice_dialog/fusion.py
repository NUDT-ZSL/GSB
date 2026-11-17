"""Dialogue-level causal score smoothing and joint intent-emotion output."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .util import canonicalize, stable_float


def _normalize(values: dict[str, float]) -> dict[str, float]:
    categories = sorted(values)
    ppm = [max(0, int(math.floor(float(values[name]) * 1_000_000.0 + 0.5))) for name in categories]
    residual = 1_000_000 - sum(ppm)
    if residual:
        best = max(range(len(ppm)), key=lambda index: (ppm[index], -index))
        ppm[best] += residual
    return {name: index / 1_000_000.0 for name, index in zip(categories, ppm)}


def _uniform(categories: list[str]) -> dict[str, float]:
    count = len(categories)
    ppm_base = 1_000_000 // count
    residual = 1_000_000 - ppm_base * count
    values = {}
    for index, category in enumerate(sorted(categories)):
        values[category] = (ppm_base + (residual if index == 0 else 0)) / 1_000_000.0
    return values


def fuse_turn(
    turn: dict[str, Any],
    context_entries: list[dict[str, Any]],
    *,
    decay: float,
    same_speaker_weight: float,
    cross_speaker_weight: float,
    fallback_source: str,
    boundary_margin: float,
) -> dict[str, Any]:
    categories = sorted(context_entries[0]["score"]["categories"]) if context_entries else []
    weighted_sums = {name: 0.0 for name in categories}
    total_weight = 0.0
    contributions: list[dict[str, Any]] = []

    for distance, entry in enumerate(context_entries):
        context_turn = entry["turn"]
        score = entry.get("score")
        if distance == 0:
            base_weight = 1.0
            speaker_relation = "current"
            speaker_weight = 1.0
        else:
            base_weight = float(decay) ** distance
            if context_turn["speaker"] == turn["speaker"]:
                speaker_relation = "same_speaker"
                speaker_weight = float(same_speaker_weight)
            else:
                speaker_relation = "cross_speaker"
                speaker_weight = float(cross_speaker_weight)
        raw_weight = base_weight * speaker_weight
        if score is None:
            score_values = _uniform(categories)
            score_available = False
            emotion = "indeterminate"
            score_origin = "uniform_fallback"
        else:
            score_values = score["scores"]
            score_available = True
            emotion = score["predicted_emotion"]
            score_origin = "model"
        for category in categories:
            weighted_sums[category] += raw_weight * float(score_values[category])
        total_weight += raw_weight
        contributions.append(
            {
                "turn_id": context_turn["turn_id"],
                "distance": distance,
                "speaker": context_turn["speaker"],
                "speaker_relation": speaker_relation,
                "decay_factor": stable_float(base_weight),
                "speaker_weight": stable_float(speaker_weight),
                "raw_weight": stable_float(raw_weight),
                "normalized_share": 0.0,
                "emotion": emotion,
                "score_available": score_available,
                "score_origin": score_origin,
            }
        )

    if total_weight <= 0:
        fused = _uniform(categories) if categories else {}
    else:
        fused = _normalize({name: weighted_sums[name] / total_weight for name in categories})
    for contribution in contributions:
        contribution["normalized_share"] = stable_float(contribution["raw_weight"] / total_weight)
    ranked = sorted(fused.items(), key=lambda item: (-item[1], item[0]))
    emotion = ranked[0][0] if ranked else "indeterminate"
    margin = round(ranked[0][1] - ranked[1][1], 6) if len(ranked) > 1 else 1.0
    current_available = bool(context_entries and context_entries[0].get("score") is not None)
    intent = str(turn.get("intent") or "unspecified")
    joint = {
        "intent": intent,
        "emotion": emotion,
        "label": f"{intent}:{emotion}",
        "current_score_available": current_available,
    }
    result = {
        "turn_id": turn["turn_id"],
        "order": turn["order"],
        "speaker": turn["speaker"],
        "filename": turn["audio"],
        "intent": intent,
        "emotion": emotion,
        "joint_output": joint,
        "fused_scores": fused,
        "score_margin": margin,
        "boundary": margin <= boundary_margin,
        "context_contributions": contributions,
        "predecessor_ids": [entry["turn"]["turn_id"] for entry in context_entries[1:]],
        "fallback_source": fallback_source or "none",
        "missing_predecessor": fallback_source not in {"", "none"},
        "current_score_available": current_available,
        "total_context_weight": stable_float(total_weight),
    }
    return canonicalize(result)
