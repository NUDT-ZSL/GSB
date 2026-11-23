"""Stage-wise error propagation and dominant-source attribution.

A full precision (float64) single-round reference run provides the ground
truth.  For every boosting round we capture five instrumented stages:

1. ``quantization`` - quantised gradients/Hessians entering the stage;
2. ``gain``         - best split gain and selected candidate;
3. ``leaf_division`` - solved leaf weights;
4. ``lr_scale``     - learning-rate scaled tree contributions;
5. ``accumulation`` - running predictions after the additive update.

For each stage we report absolute deviation, relative deviation, an error
interval ``[min, max]`` across rows, and the amplification ratio relative
to the previous stage.  The dominant stage is the one with the largest
relative deviation; tolerance violations produce
``E_TOLERANCE_EXCEEDED`` records carrying the offending location.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from .config import TrainConfig
from .errors import ErrorCode
from .records import stage_record

STAGE_NAMES = (
    "quantization",
    "gain",
    "leaf_division",
    "lr_scale",
    "accumulation",
)


@dataclass
class StageDeviation:
    stage: str
    round_index: int
    abs_deviation: float
    rel_deviation: float
    interval: tuple[float, float]
    amplification: float
    reference_norm: float
    candidate_norm: float
    violations: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "round_index": int(self.round_index),
            "abs_deviation": float(self.abs_deviation),
            "rel_deviation": float(self.rel_deviation),
            "interval": [float(self.interval[0]), float(self.interval[1])],
            "amplification": float(self.amplification),
            "reference_norm": float(self.reference_norm),
            "candidate_norm": float(self.candidate_norm),
            "violations": list(self.violations),
        }


def _vector_metrics(
    reference: np.ndarray, candidate: np.ndarray
) -> tuple[float, float, tuple[float, float], float, float]:
    reference = np.asarray(reference, dtype=np.float64)
    candidate = np.asarray(candidate, dtype=np.float64)
    diff = candidate - reference
    abs_dev = float(np.linalg.norm(diff, ord=2))
    ref_norm = float(np.linalg.norm(reference, ord=2))
    cand_norm = float(np.linalg.norm(candidate, ord=2))
    scale = max(ref_norm, 1e-300)
    rel_dev = abs_dev / scale
    if diff.size == 0:
        interval = (0.0, 0.0)
    else:
        interval = (float(np.min(diff)), float(np.max(diff)))
    return abs_dev, rel_dev, interval, ref_norm, cand_norm


def analyse_round(
    round_index: int,
    reference_checkpoints: Dict[str, np.ndarray],
    candidate_checkpoints: Dict[str, np.ndarray],
    config: TrainConfig,
    previous_rel: Optional[float],
) -> tuple[List[StageDeviation], List[Dict[str, Any]], Optional[str]]:
    """Compute per-stage deviations for one round and detect violations."""
    deviations: List[StageDeviation] = []
    records: List[Dict[str, Any]] = []
    dominant: Optional[str] = None
    dominant_rel = -1.0

    for position, stage in enumerate(STAGE_NAMES):
        ref = reference_checkpoints[stage]
        cand = candidate_checkpoints[stage]
        abs_dev, rel_dev, interval, ref_norm, cand_norm = _vector_metrics(
            ref, cand
        )
        if previous_rel is None or position == 0:
            amplification = 1.0
        else:
            amplification = rel_dev / max(previous_rel, 1e-300)

        tolerance = float(config.stage_tolerances[position])
        violations: List[Dict[str, Any]] = []
        if rel_dev > tolerance:
            location = _locate_violation(ref, cand)
            violation = {
                "stage": stage,
                "round_index": int(round_index),
                "sample_index": int(location),
                "reference_value": float(ref[location]),
                "candidate_value": float(cand[location]),
                "abs_deviation": float(
                    abs(float(cand[location]) - float(ref[location]))
                ),
                "tolerance": tolerance,
                "rel_deviation": float(rel_dev),
            }
            violations.append(violation)
            records.append(
                stage_record(
                    stage,
                    ErrorCode.TOLERANCE_EXCEEDED,
                    f"stage '{stage}' round {round_index} exceeded tolerance "
                    f"{tolerance:g} (rel={rel_dev:.3e})",
                    sample_indices=[int(location)],
                    details=violation,
                )
            )

        deviation = StageDeviation(
            stage=stage,
            round_index=round_index,
            abs_deviation=abs_dev,
            rel_deviation=rel_dev,
            interval=interval,
            amplification=amplification,
            reference_norm=ref_norm,
            candidate_norm=cand_norm,
            violations=violations,
        )
        deviations.append(deviation)
        if rel_dev > dominant_rel:
            dominant_rel = rel_dev
            dominant = stage
        previous_rel = rel_dev

    return deviations, records, dominant


def _locate_violation(reference: np.ndarray, candidate: np.ndarray) -> int:
    return int(np.argmax(np.abs(candidate - reference)))


def build_error_report(
    per_round: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Aggregate round-level analyses into the final error attribution report."""
    dominant_votes: Dict[str, float] = {}
    all_violations: List[Dict[str, Any]] = []
    for round_block in per_round:
        dominant = round_block["dominant_stage"]
        if dominant is not None:
            dominant_votes[dominant] = (
                dominant_votes.get(dominant, 0.0)
                + float(round_block["max_rel_deviation"])
            )
        for deviation in round_block["deviations"]:
            all_violations.extend(deviation["violations"])

    dominant_stage = (
        max(dominant_votes.items(), key=lambda item: item[1])[0]
        if dominant_votes
        else None
    )
    return {
        "stages": list(STAGE_NAMES),
        "dominant_stage": dominant_stage,
        "dominant_votes": dominant_votes,
        "violations": all_violations,
        "rounds": list(per_round),
    }
