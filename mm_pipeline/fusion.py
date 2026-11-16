"""Per-round weighted evidence fusion, conflict discount and accumulation."""
from __future__ import annotations

import numpy as np

from .config import (RELATION_CONFLICT, RELATION_SUPPLEMENT,
                     PipelineConfig)
from .features import FORCE_FAILURE, attribute_compatibility


class FusionError(RuntimeError):
    """Raised when fusion fails for a sample."""


def _softmax_weighted(visual_evidence: float, text_evidence: float,
                      cfg: PipelineConfig) -> tuple[float, float, float]:
    # Temperature sharpens the softmax distribution over the two modalities.
    logits = np.array([visual_evidence, text_evidence],
                      dtype=np.float64) / cfg.fusion_temperature
    logits = logits - logits.max()
    probs = np.exp(logits)
    probs = probs / probs.sum()
    text_weight = float(np.clip(cfg.text_evidence_weight, 1e-6, 1 - 1e-6))
    base_weights = np.array([1.0 - text_weight, text_weight],
                            dtype=np.float64)
    weights = base_weights * probs
    weights = weights / weights.sum()
    fused = float(weights[0] * visual_evidence +
                  weights[1] * text_evidence)
    return fused, float(weights[0]), float(weights[1])


def fuse_round(sample_name: str, round_index: int, aligned: list[dict],
               cfg: PipelineConfig) -> dict:
    """Compute one deterministic evidence round from aligned relations."""
    if FORCE_FAILURE.get(sample_name) == "fusion":
        raise FusionError(f"forced fusion failure for {sample_name}")

    contributions = []
    text_terms = []
    visual_terms = []
    supplement_bonus = 0.0
    conflict_penalty = 0.0
    conflict_count = 0

    supplement_index = 0
    for relation in aligned:
        assertion = relation["assertion"]
        region = relation["region"]
        vector_text = assertion["vector"]
        vector_region = region["quantized"]
        denominator = (np.linalg.norm(vector_text) *
                       np.linalg.norm(vector_region) + 1e-12)
        cosine = float(np.dot(vector_text, vector_region) / denominator)
        compatibility = attribute_compatibility(assertion, region)
        weight = relation["weight"]

        if relation["type"] == RELATION_SUPPLEMENT:
            supplement_index += 1
            active = round_index >= supplement_index
            term = 0.0
            if active:
                term = weight * 0.1 * (1.0 + compatibility)
                supplement_bonus += term
            contributions.append({
                "relation_id": relation["relation_id"],
                "source_order": relation["source_order"],
                "type": relation["type"],
                "assertion_id": relation["assertion_id"],
                "region_id": relation["region_id"],
                "weight": weight,
                "cosine": round(cosine, 10),
                "compatibility": compatibility,
                "visual_component": 0.0,
                "text_component": 0.0,
                "supplement_component": round(term, 10),
                "active": active,
            })
            continue

        # Attribute compatibility is the primary signal; vector cosine is a
        # bounded secondary visual signal, combined into per-modality evidence.
        text_term = weight * (compatibility +
                              0.25 * assertion["confidence"] * cosine)
        visual_term = weight * (compatibility + 0.25 * cosine)
        if relation["type"] == RELATION_CONFLICT:
            penalty = cfg.conflict_penalty * weight * (
                1.0 + abs(compatibility))
            conflict_penalty += penalty
            conflict_count += 1
            text_component = -abs(text_term)
            visual_component = -abs(visual_term)
        else:
            text_terms.append(text_term)
            visual_terms.append(visual_term)
            text_component = text_term
            visual_component = visual_term
        contributions.append({
            "relation_id": relation["relation_id"],
            "source_order": relation["source_order"],
            "type": relation["type"],
            "assertion_id": relation["assertion_id"],
            "region_id": relation["region_id"],
            "weight": weight,
            "cosine": round(cosine, 10),
            "compatibility": compatibility,
            "visual_component": round(visual_component, 10),
            "text_component": round(text_component, 10),
            "supplement_component": 0.0,
            "active": True,
        })

    text_evidence = (sum(text_terms) / len(text_terms)
                     if text_terms else 0.0)
    visual_evidence = (sum(visual_terms) / len(visual_terms)
                       if visual_terms else 0.0)
    fused, visual_weight, text_weight = _softmax_weighted(
        visual_evidence, text_evidence, cfg)

    damping = 1.0 / (1.0 + 0.05 * (round_index - 1))
    round_score = fused * damping + supplement_bonus - conflict_penalty
    round_score = float(np.clip(round_score, -1.0, 1.0))
    confidence = float(0.5 + 0.5 * round_score)

    return {
        "round": round_index,
        "round_score": round(round_score, 10),
        "confidence": round(confidence, 10),
        "visual_evidence": round(visual_evidence, 10),
        "text_evidence": round(text_evidence, 10),
        "visual_weight": round(visual_weight, 10),
        "text_weight": round(text_weight, 10),
        "supplement_bonus": round(supplement_bonus, 10),
        "conflict_penalty": round(conflict_penalty, 10),
        "conflict_count": conflict_count,
        "threshold": cfg.confidence_threshold,
        "converged": bool(confidence >= cfg.confidence_threshold),
        "evidence_contributions": contributions,
    }
