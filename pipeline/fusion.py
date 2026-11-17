"""Per-round evidence fusion with numerically stable softmax."""

from __future__ import annotations

import math

import numpy as np

from .config import FusionConfig
from .errors import (E_NON_FINITE_FEATURE, E_FUSION_ERROR, PipelineError,
                     STAGE_FUSION)
from .textrel import (RELATION_CONFLICT, RELATION_SUPPLEMENT,
                      Relation, region_index)

ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
RUNNING = "RUNNING"


def round_temperature(config: FusionConfig, round_index: int) -> float:
    """Deterministic per-round temperature schedule.

    Each evidence round widens the softmax temperature by 10 percent so that
    later rounds distribute attention more smoothly. The schedule depends only
    on the configured base temperature and the round index.
    """
    return config.fusion_temperature * (1.0 + 0.1 * round_index)


def _stable_softmax(logits: np.ndarray) -> np.ndarray:
    # Subtract the maximum before exponentiation (identical softmax, no
    # overflow even for large logits).
    shifted = logits - float(np.max(logits))
    exponentials = np.exp(shifted)
    total = float(exponentials.sum())
    if not math.isfinite(total) or total <= 0.0:
        raise PipelineError(E_FUSION_ERROR, STAGE_FUSION,
                            "softmax denominator is not finite")
    return exponentials / total


def fuse_round(visual: np.ndarray, text: dict[str, np.ndarray],
               relations: list[Relation], config: FusionConfig,
               round_index: int) -> dict:
    """Fuse one evidence round and return a serializable round record."""
    if not np.all(np.isfinite(visual)) or any(
            not np.all(np.isfinite(vector)) for vector in text.values()):
        raise PipelineError(E_NON_FINITE_FEATURE, STAGE_FUSION,
                            "non-finite feature reached the fusion stage")

    temperature = round_temperature(config, round_index)
    logits = np.zeros(len(relations), dtype=np.float64)
    bases = np.zeros(len(relations), dtype=np.float64)
    similarities = np.zeros(len(relations), dtype=np.float64)

    for index, relation in enumerate(relations):
        region_idx = region_index(relation.region_id)
        if not 0 <= region_idx < visual.shape[0]:
            raise PipelineError(E_FUSION_ERROR, STAGE_FUSION,
                                f"region index out of range: {region_idx}")
        similarity = float(np.dot(visual[region_idx],
                                  text[relation.assertion_id]))
        similarity = float(np.clip(similarity, -1.0, 1.0))
        similarities[index] = similarity
        if relation.kind == RELATION_CONFLICT:
            base = 0.5 - 0.5 * similarity
        else:
            base = 0.5 + 0.5 * similarity
        bases[index] = base
        logits[index] = base / temperature

    softmax_weights = _stable_softmax(logits)

    effective_weights = np.zeros(len(relations), dtype=np.float64)
    for index, relation in enumerate(relations):
        if relation.kind == RELATION_CONFLICT:
            effective_weights[index] = relation.weight * config.conflict_discount
        elif relation.kind == RELATION_SUPPLEMENT:
            effective_weights[index] = relation.weight + config.supplement_boost
        else:
            effective_weights[index] = relation.weight

    masses = softmax_weights * effective_weights
    mass_total = float(masses.sum())
    if not math.isfinite(mass_total) or mass_total <= 0.0:
        raise PipelineError(E_FUSION_ERROR, STAGE_FUSION,
                            "effective evidence mass is not finite")
    contributions = masses / mass_total
    score = float(np.sum(contributions * bases))
    confidence = float(np.max(contributions))
    entropy = float(-np.sum(
        contributions[contributions > 0]
        * np.log(contributions[contributions > 0])))
    if not math.isfinite(score) or not math.isfinite(confidence):
        raise PipelineError(E_NON_FINITE_FEATURE, STAGE_FUSION,
                            "fusion produced a non-finite score")

    decomposition = []
    for index, relation in enumerate(relations):
        decomposition.append({
            "relation_id": relation.relation_id,
            "kind": relation.kind,
            "assertion_id": relation.assertion_id,
            "region_id": relation.region_id,
            "similarity": similarities[index],
            "base_evidence": bases[index],
            "logit": logits[index],
            "softmax_weight": float(softmax_weights[index]),
            "effective_weight": float(effective_weights[index]),
            "contribution": float(contributions[index]),
        })

    if score < config.confidence_threshold:
        decision = REJECTED
        reason = "below_confidence_threshold"
    elif round_index == config.max_rounds - 1:
        decision = ACCEPTED
        reason = "rounds_exhausted"
    else:
        decision = RUNNING
        reason = "threshold_satisfied_continue"

    return {
        "round": round_index,
        "temperature": temperature,
        "score": score,
        "confidence": confidence,
        "entropy": entropy,
        "decision": decision,
        "reason": reason,
        "conflict_count": sum(
            1 for relation in relations if relation.kind == RELATION_CONFLICT),
        "supplement_count": sum(
            1 for relation in relations
            if relation.kind == RELATION_SUPPLEMENT),
        "contributions": decomposition,
    }
