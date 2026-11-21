from __future__ import annotations

from typing import Any

import numpy as np

from .canonical import sha256_json
from .models import Candidate, Turn


def score_value(confidence: float, extractor_version: str, config: dict[str, Any]) -> float:
    version_weight = float(config["extractor_versions"][extractor_version])
    value = np.round(
        np.float64(confidence) * np.float64(version_weight),
        decimals=int(config["score_decimals"]),
    )
    return float(value)


def score_turn(turn: Turn, config: dict[str, Any]) -> dict[str, Any]:
    """Parse one user frame and score every candidate deterministically."""

    if turn.role != "user":
        raise ValueError("only user turns carry scoreable candidates")

    confidences = np.asarray(
        [candidate.confidence for candidate in turn.candidates],
        dtype=np.float64,
    )
    weights = np.asarray(
        [
            float(config["extractor_versions"][candidate.extractor_version])
            for candidate in turn.candidates
        ],
        dtype=np.float64,
    )
    scores = np.round(
        confidences * weights,
        decimals=int(config["score_decimals"]),
    )

    scored: list[dict[str, Any]] = []
    for candidate, score in zip(turn.candidates, scores):
        item = candidate.to_dict()
        item["score"] = float(score)
        item["candidate_fingerprint"] = sha256_json(item)
        scored.append(item)

    scored.sort(
        key=lambda item: (
            -item["score"],
            item["source_turn"],
            item["key"],
            item["value"],
            item["extractor_version"],
            -item["confidence"],
            item["candidate_fingerprint"],
        )
    )
    for rank, item in enumerate(scored, start=1):
        item["rank"] = rank

    return {
        "schema_version": 1,
        "scoring_algorithm": config["scoring_algorithm"],
        "score_decimals": config["score_decimals"],
        "turn_id": turn.turn_id,
        "turn_index": turn.index,
        "candidates": scored,
    }


def score_candidate_payload(candidate: Candidate, config: dict[str, Any]) -> float:
    return score_value(candidate.confidence, candidate.extractor_version, config)
