"""Deterministic top-k selection and numerically stable relevance grading."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from .config import STRICT, TOLERANCE_BAND, ThresholdConfig
from .errors import XrelError, E_THRESHOLD_STRATEGY_INVALID, E_THRESHOLD_TOLERANCE_INVALID
from .numerics import NumericBackend
from .scoring import DocScore


@dataclass
class GradedDoc:
    doc_id: str
    doc_lang: str
    score: float
    grade: str
    rank: int
    within_tolerance_band: bool = False
    protected_subnormal: bool = False

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "doc_lang": self.doc_lang,
            "score": self.score,
            "grade": self.grade,
            "rank": self.rank,
            "within_tolerance_band": self.within_tolerance_band,
            "protected_subnormal": self.protected_subnormal,
        }


@dataclass
class TopKResult:
    query_id: str
    selected: List[GradedDoc] = field(default_factory=list)
    skipped: List[dict] = field(default_factory=list)
    protected: List[dict] = field(default_factory=list)
    strategy: str = ""
    tolerance: float = 0.0

    def to_dict(self) -> dict:
        return {
            "query_id": self.query_id,
            "strategy": self.strategy,
            "tolerance": self.tolerance,
            "selected": [item.to_dict() for item in self.selected],
            "skipped": list(self.skipped),
            "protected": list(self.protected),
        }


def _validate(config: ThresholdConfig) -> None:
    if config.strategy not in (STRICT, TOLERANCE_BAND):
        raise XrelError(
            E_THRESHOLD_STRATEGY_INVALID,
            "threshold comparison strategy is unknown",
            {"field": "threshold.strategy", "value": config.strategy},
        )
    tolerance = float(config.tolerance)
    if tolerance < 0.0 or tolerance != tolerance or tolerance in (float("inf"), float("-inf")):
        raise XrelError(
            E_THRESHOLD_TOLERANCE_INVALID,
            "threshold tolerance must be a finite non-negative number",
            {"field": "threshold.tolerance", "value": config.tolerance},
        )


def _grade_for_boundary(score: float, config: ThresholdConfig) -> str:
    for grade, boundary in config.grades:
        if score >= boundary:
            return grade
    return config.grades[-1][0]


def select_topk(
    query_id: str,
    doc_scores: Dict[str, DocScore],
    top_k: int,
    config: ThresholdConfig,
    backend: NumericBackend,
) -> TopKResult:
    _validate(config)
    result = TopKResult(
        query_id=query_id, strategy=config.strategy, tolerance=float(config.tolerance)
    )

    # Deterministic ranking: score descending, doc_id ascending on ties.
    ordered = sorted(doc_scores.values(), key=lambda item: (-item.score, item.doc_id))

    for doc_score in ordered:
        score = doc_score.score
        if hasattr(backend, "is_subnormal_real"):
            protected_subnormal = backend.is_subnormal_real(score)
        else:
            protected_subnormal = backend.is_subnormal(backend.quantize(score))
        if protected_subnormal:
            # Flush and remember the protected document so no tier can flip it.
            score = backend.to_float(backend.flush_subnormal(backend.quantize(score)))
            result.protected.append(
                {
                    "doc_id": doc_score.doc_id,
                    "reason": "subnormal_flush_to_zero",
                    "raw_score": doc_score.score,
                    "effective_score": score,
                }
            )

        grade, within_band = _stable_grade(doc_score.doc_id, score, config, result)
        record = GradedDoc(
            doc_id=doc_score.doc_id,
            doc_lang=doc_score.doc_lang,
            score=score,
            grade=grade,
            rank=0,
            within_tolerance_band=within_band,
            protected_subnormal=protected_subnormal,
        )
        if len(result.selected) < top_k:
            record.rank = len(result.selected) + 1
            result.selected.append(record)
        else:
            result.skipped.append(
                {
                    "doc_id": doc_score.doc_id,
                    "score": score,
                    "grade": grade,
                    "reason": "beyond_top_k",
                    "within_tolerance_band": within_band,
                }
            )
    return result


def _stable_grade(doc_id: str, score: float, config: ThresholdConfig, result: TopKResult):
    """Apply strict or tolerance-band grade comparison.

    Under tolerance-band mode a score inside ``tolerance`` *below* a boundary
    is rounded up to the boundary grade and the document is logged as
    protected.  This keeps boundary-adjacent documents from flipping grade
    purely because of precision tier rounding.
    """

    strict_grade = _grade_for_boundary(score, config)
    if config.strategy == STRICT:
        return strict_grade, False

    tolerance = float(config.tolerance)
    for grade, boundary in config.grades:
        if boundary == float("-inf"):
            continue
        gap = boundary - score
        if 0.0 < gap <= tolerance:
            result.protected.append(
                {
                    "doc_id": doc_id,
                    "reason": "tolerance_band_upgrade",
                    "boundary": boundary,
                    "grade": grade,
                    "score": score,
                    "gap": gap,
                }
            )
            return grade, True
    return strict_grade, False
