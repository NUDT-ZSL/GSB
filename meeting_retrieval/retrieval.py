"""Cross-modal alignment scoring and deterministic retrieval ranking."""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

from .canonical import b64_f64, digest, float_text
from .models import ALIGN_VERSION, RANK_VERSION, RetrievalConfig, natural_key


SIMILARITY_DECLARATION: Dict[str, Any] = {
    "metric": "cosine",
    "tile_reduce": "max",
    "tie_break": "block_index_ascending",
    "score_range": [-1.0, 1.0],
}


def cosine_score(text_vector: np.ndarray, visual_vector: np.ndarray) -> float:
    text_norm = float(np.linalg.norm(text_vector))
    visual_norm = float(np.linalg.norm(visual_vector))
    if text_norm == 0.0 or visual_norm == 0.0:
        return 0.0
    value = float(np.dot(text_vector, visual_vector) / (text_norm * visual_norm))
    return max(-1.0, min(1.0, value))


def align_from_artifacts(
    visual_artifact: Dict[str, Any],
    text_artifact: Dict[str, Any],
    feature_versions: Dict[str, Any],
    vocab_versions: Dict[str, Any],
) -> Dict[str, Any]:
    text_vector = b64_f64(text_artifact["vector_f64_b64"])
    tile_scores: List[Dict[str, Any]] = []
    best_index = -1
    best_score = -2.0
    for tile in visual_artifact["tiles"]:
        visual_vector = b64_f64(tile["vector_f64_b64"])
        score = cosine_score(text_vector, visual_vector)
        tile_scores.append(
            {
                "block_index": int(tile["block_index"]),
                "score": float_text(score),
            }
        )
        if score > best_score or (score == best_score and int(tile["block_index"]) < best_index):
            best_score = score
            best_index = int(tile["block_index"])
    if best_index < 0:
        best_score = 0.0
    artifact = {
        "algorithm": ALIGN_VERSION,
        "similarity": dict(SIMILARITY_DECLARATION),
        "score": float_text(best_score),
        "best_block_index": best_index,
        "tile_scores": tile_scores,
        "visual_artifact_hash": visual_artifact.get("artifact_hash"),
        "text_artifact_hash": text_artifact.get("artifact_hash"),
        "feature_versions": dict(feature_versions),
        "vocab_versions": dict(vocab_versions),
    }
    artifact["artifact_hash"] = digest(artifact)
    return artifact


def build_ranking(
    pair_scores: Sequence[Tuple[str, Dict[str, Any]]],
    retrieval_config: RetrievalConfig,
    versions: Dict[str, Any],
) -> Dict[str, Any]:
    sorted_candidates = sorted(pair_scores, key=lambda item: natural_key(item[0]))
    eligible: List[Dict[str, Any]] = []
    for pair_id, alignment in sorted_candidates:
        score = float(alignment["score"])
        if score >= retrieval_config.min_score:
            eligible.append(
                {
                    "pair_id": pair_id,
                    "score": float_text(score),
                    "alignment_artifact_hash": alignment["artifact_hash"],
                    "best_block_index": int(alignment["best_block_index"]),
                }
            )
    eligible.sort(
        key=lambda item: (
            -float(item["score"]),
            natural_key(item["pair_id"]),
            int(item["best_block_index"]),
            item["pair_id"],
        )
    )
    selected = eligible[: retrieval_config.top_k]
    for rank, item in enumerate(selected, start=1):
        item["rank"] = rank
    metrics = {
        "total_pairs": len(pair_scores),
        "eligible_pairs": len(eligible),
        "returned_hits": len(selected),
        "top_k": retrieval_config.top_k,
        "min_score": retrieval_config.min_score,
        "max_score": float_text(max((float(item["score"]) for item in eligible), default=0.0)),
        "mean_score": float_text(
            sum(float(item["score"]) for item in eligible) / len(eligible) if eligible else 0.0
        ),
    }
    artifact = {
        "algorithm": RANK_VERSION,
        "retrieval": retrieval_config.to_dict(),
        "tie_break": "score_desc,pair_id_natural_asc,block_index_asc,pair_id",
        "hits": selected,
        "eligible": eligible,
        "metrics": metrics,
        "versions": dict(versions),
    }
    artifact["artifact_hash"] = digest(artifact)
    return artifact
