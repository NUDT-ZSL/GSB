"""Fixed-point cosine similarity and threshold decisions."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .canonical import derived_key, round_float_half_away, round_significant, sha256_obj
from .config import Config
from .embedding import EmbeddingResult
from .fixedpoint import cosine_fixed, fixed_to_float, int64_overflow_risk
from .pairs import PairSpec


@dataclass(frozen=True)
class SimilarityResult:
    pair: PairSpec
    left_embedding_key: str
    right_embedding_key: str
    similarity_fixed: int
    similarity: float
    reference_similarity: float
    threshold_fixed: int
    decision: str
    reference_decision: str
    threshold_tie: bool
    boundary: bool
    dot: int
    left_norm_q16: int
    right_norm_q16: int
    overflow_risk: bool
    fingerprint: str

    @property
    def correct(self) -> bool:
        predicted_same = self.decision == "SAME_SPEAKER"
        expected_same = self.pair.relation == "same"
        return predicted_same == expected_same

    def as_payload(self) -> dict[str, Any]:
        return {
            "index": self.pair.index,
            "id": self.pair.pair_id,
            "left": self.pair.left,
            "right": self.pair.right,
            "relation": self.pair.relation,
            "pair": {
                "index": self.pair.index,
                "id": self.pair.pair_id,
                "left": self.pair.left,
                "right": self.pair.right,
                "relation": self.pair.relation,
            },
            "left_embedding_key": self.left_embedding_key,
            "right_embedding_key": self.right_embedding_key,
            "similarity_fixed": self.similarity_fixed,
            "similarity": self.similarity,
            "reference_similarity": self.reference_similarity,
            "threshold_fixed": self.threshold_fixed,
            "decision": self.decision,
            "reference_decision": self.reference_decision,
            "threshold_tie": self.threshold_tie,
            "boundary": self.boundary,
            "dot": self.dot,
            "left_norm_q16": self.left_norm_q16,
            "right_norm_q16": self.right_norm_q16,
            "overflow_risk": self.overflow_risk,
            "correct": self.correct,
            "similarity_fingerprint": self.fingerprint,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any], pair: PairSpec) -> "SimilarityResult":
        return cls(
            pair=pair,
            left_embedding_key=payload["left_embedding_key"],
            right_embedding_key=payload["right_embedding_key"],
            similarity_fixed=int(payload["similarity_fixed"]),
            similarity=float(payload["similarity"]),
            reference_similarity=float(payload["reference_similarity"]),
            threshold_fixed=int(payload["threshold_fixed"]),
            decision=payload["decision"],
            reference_decision=payload["reference_decision"],
            threshold_tie=bool(payload["threshold_tie"]),
            boundary=bool(payload["boundary"]),
            dot=int(payload["dot"]),
            left_norm_q16=int(payload["left_norm_q16"]),
            right_norm_q16=int(payload["right_norm_q16"]),
            overflow_risk=bool(payload["overflow_risk"]),
            fingerprint=payload["similarity_fingerprint"],
        )


def similarity_cache_key(
    pair: PairSpec,
    left_embedding_key: str,
    right_embedding_key: str,
    config: Config,
    bits: int,
) -> str:
    return derived_key(
        "similarity/v3",
        left_embedding_key,
        right_embedding_key,
        config.similarity_hash(),
        {"fractional_bits": bits},
        {
            "pair_id": pair.pair_id,
            "left": pair.left,
            "right": pair.right,
            "relation": pair.relation,
        },
    )


def _reference_cosine(left: EmbeddingResult, right: EmbeddingResult) -> float:
    left_values = left.float_vector()
    right_values = right.float_vector()
    dot = math.fsum(a * b for a, b in zip(left_values, right_values))
    left_norm = math.sqrt(math.fsum(item * item for item in left_values))
    right_norm = math.sqrt(math.fsum(item * item for item in right_values))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


def compute_similarity(
    pair: PairSpec,
    left: EmbeddingResult,
    right: EmbeddingResult,
    left_key: str,
    right_key: str,
    config: Config,
    *,
    threshold_tie: bool = False,
    duplicate_similarity: bool = False,
) -> SimilarityResult:
    similarity_fixed, diagnostics = cosine_fixed(
        left.vector_q, right.vector_q, config.similarity_scale
    )
    similarity = similarity_fixed / float(config.similarity_scale)
    threshold_fixed = round_float_half_away(
        float(config.similarity_threshold) * float(config.similarity_scale)
    )
    decision = "SAME_SPEAKER" if similarity_fixed >= threshold_fixed else "DIFFERENT_SPEAKER"
    reference_similarity = _reference_cosine(left, right)
    reference_decision = (
        "SAME_SPEAKER" if reference_similarity >= config.similarity_threshold else "DIFFERENT_SPEAKER"
    )
    threshold_tie = bool(threshold_tie or similarity_fixed == threshold_fixed)
    boundary = bool(threshold_tie or duplicate_similarity)
    fingerprint_core = {
        "left_key": left_key,
        "right_key": right_key,
        "similarity_fixed": similarity_fixed,
        "threshold_fixed": threshold_fixed,
        "decision": decision,
        "tie_rule": "greater_or_equal_accept",
    }
    return SimilarityResult(
        pair=pair,
        left_embedding_key=left_key,
        right_embedding_key=right_key,
        similarity_fixed=similarity_fixed,
        similarity=round_significant(similarity, config.feature_significant_digits),
        reference_similarity=round_significant(reference_similarity, config.feature_significant_digits),
        threshold_fixed=threshold_fixed,
        decision=decision,
        reference_decision=reference_decision,
        threshold_tie=threshold_tie,
        boundary=boundary,
        dot=diagnostics["dot"],
        left_norm_q16=diagnostics["left_norm_q16"],
        right_norm_q16=diagnostics["right_norm_q16"],
        overflow_risk=int64_overflow_risk(diagnostics["max_absolute_accumulator"]),
        fingerprint=sha256_obj(fingerprint_core),
    )
