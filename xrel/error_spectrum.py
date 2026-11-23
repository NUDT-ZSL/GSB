"""Stepwise error propagation with float64 as the reference truth."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .errors import ErrorSpectrumInvalid
from .numerics import FixedBackend, NumericBackend
from .scoring import Contribution, DocScore


STAGES = (
    "confidence_weighting",
    "partition_reduction",
    "quantization",
    "topk_threshold",
)


@dataclass
class StageError:
    stage: str
    abs_deviation: float
    rel_deviation: float
    interval_low: float
    interval_high: float
    amplification: float
    reference_value: float
    observed_value: float

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "abs_deviation": self.abs_deviation,
            "rel_deviation": self.rel_deviation,
            "interval_low": self.interval_low,
            "interval_high": self.interval_high,
            "amplification": self.amplification,
            "reference_value": self.reference_value,
            "observed_value": self.observed_value,
        }


@dataclass
class ErrorSpectrum:
    precision: str
    per_doc: Dict[str, List[StageError]] = field(default_factory=dict)
    dominant_stage: str = ""
    fixed_recompute_ok: Optional[bool] = None

    def to_dict(self) -> dict:
        return {
            "precision": self.precision,
            "dominant_stage": self.dominant_stage,
            "fixed_recompute_ok": self.fixed_recompute_ok,
            "per_doc": {
                doc_id: [stage.to_dict() for stage in stages]
                for doc_id, stages in sorted(self.per_doc.items())
            },
        }


def _safe_rel(delta: float, reference: float) -> float:
    if reference == 0.0:
        return 0.0 if delta == 0.0 else float("inf")
    return delta / abs(reference)


def _stage_error(stage: str, reference: float, observed: float, prev_abs: float) -> StageError:
    abs_dev = abs(observed - reference)
    rel_dev = _safe_rel(abs_dev, reference)
    # Error interval is symmetric around the observed value.
    interval_low = observed - abs_dev
    interval_high = observed + abs_dev
    amplification = (abs_dev / prev_abs) if prev_abs > 0.0 else (1.0 if abs_dev > 0.0 else 0.0)
    return StageError(
        stage=stage,
        abs_deviation=abs_dev,
        rel_deviation=rel_dev,
        interval_low=interval_low,
        interval_high=interval_high,
        amplification=amplification,
        reference_value=reference,
        observed_value=observed,
    )


def build_spectrum(
    precision: str,
    reference_scores: Dict[str, DocScore],
    observed_scores: Dict[str, DocScore],
    backend: NumericBackend,
    topk_gate_deviation: Optional[Dict[str, float]] = None,
) -> ErrorSpectrum:
    """Build the four-stage error spectrum per document.

    Stages:
      1. confidence_weighting - deviation of each weighted contribution;
      2. partition_reduction  - deviation of per-language partition subtotals;
      3. quantization         - deviation of the final document score;
      4. topk_threshold       - deviation at the gate/top-k boundary.
    """

    spectrum = ErrorSpectrum(precision=precision)
    dominant_totals = {stage: 0.0 for stage in STAGES}
    topk_gate_deviation = topk_gate_deviation or {}

    for doc_id in sorted(reference_scores):
        ref_doc = reference_scores[doc_id]
        obs_doc = observed_scores.get(doc_id)
        if obs_doc is None:
            # Hit sets must be identical across precision; a missing doc is fatal.
            raise ErrorSpectrumInvalid(
                "hit document set diverged across precision tiers",
                doc_id=doc_id,
                precision=precision,
            )
        stages: List[StageError] = []

        # Stage 1: per-contribution confidence weighting deviation.
        ref_weighted = [item.weighted_contribution for item in ref_doc.contributions]
        obs_weighted = [item.weighted_contribution for item in obs_doc.contributions]
        if len(ref_weighted) != len(obs_weighted):
            raise ErrorSpectrumInvalid(
                "contribution count diverged across precision tiers",
                doc_id=doc_id,
                precision=precision,
            )
        ref_weight_total = sum(ref_weighted)
        obs_weight_total = sum(obs_weighted)
        stage1 = _stage_error(
            STAGES[0], ref_weight_total, obs_weight_total, prev_abs=abs(ref_weight_total) or 1.0
        )
        stages.append(stage1)

        # Stage 2: partition reduction subtotal deviation.
        ref_partition = _partition_subtotals(ref_doc.contributions)
        obs_partition = _partition_subtotals(obs_doc.contributions)
        ref_part_total = sum(ref_partition.values())
        obs_part_total = sum(obs_partition.values())
        stage2 = _stage_error(
            STAGES[1], ref_part_total, obs_part_total, prev_abs=stage1.abs_deviation
        )
        stages.append(stage2)

        # Stage 3: final quantized score deviation.
        stage3 = _stage_error(
            STAGES[2], ref_doc.score, obs_doc.score, prev_abs=stage2.abs_deviation
        )
        stages.append(stage3)

        # Stage 4: top-k gate deviation (defaults to score deviation).
        gate_dev = topk_gate_deviation.get(doc_id, stage3.abs_deviation)
        gate_observed = ref_doc.score + (
            gate_dev if obs_doc.score >= ref_doc.score else -gate_dev
        )
        stage4 = _stage_error(
            STAGES[3], ref_doc.score, gate_observed, prev_abs=stage3.abs_deviation
        )
        stages.append(stage4)

        _validate_intervals(doc_id, stages)
        spectrum.per_doc[doc_id] = stages
        for stage in stages:
            dominant_totals[stage.stage] += stage.abs_deviation

    spectrum.dominant_stage = max(dominant_totals, key=lambda key: dominant_totals[key])
    if isinstance(backend, FixedBackend):
        spectrum.fixed_recompute_ok = _verify_fixed_recompute(backend, observed_scores)
        if not spectrum.fixed_recompute_ok:
            raise ErrorSpectrumInvalid(
                "fixed tier deviation cannot be reproduced from scale and rounding",
                precision=precision,
            )
    return spectrum


def _partition_subtotals(contributions: List[Contribution]) -> Dict[str, float]:
    subtotals: Dict[str, float] = {}
    for item in contributions:
        subtotals[item.target_lang] = subtotals.get(item.target_lang, 0.0) + item.weighted_contribution
    return subtotals


def _validate_intervals(doc_id: str, stages: List[StageError]) -> None:
    for stage in stages:
        if stage.interval_low > stage.interval_high:
            raise ErrorSpectrumInvalid(
                "error interval bounds are in the wrong order",
                doc_id=doc_id,
                stage=stage.stage,
                interval_low=stage.interval_low,
                interval_high=stage.interval_high,
            )


def _verify_fixed_recompute(
    backend: FixedBackend, observed_scores: Dict[str, DocScore]
) -> bool:
    """Reproduce fixed contributions using the fixed point operators.

    Recomputation starts from the quantized raw score and weight rather than
    from their float-rendered copies, which would round a second time and
    therefore disagree with the real integer arithmetic.
    """

    for doc_score in observed_scores.values():
        for item in doc_score.contributions:
            if item.backend_raw is None or item.backend_weight is None:
                return False
            expected_int = backend.mul(item.backend_raw, item.backend_weight)
            stored_int = item.backend_contribution
            if expected_int != stored_int:
                return False
        ordered = sorted(
            doc_score.contributions,
            key=lambda item: (item.target_lang, item.target_term, item.doc_id),
        )
        expected_score_int = backend.sum_sorted(
            item.backend_contribution for item in ordered
        )
        if expected_score_int != doc_score.backend_score:
            return False
    return True
