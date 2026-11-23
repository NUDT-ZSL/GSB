"""Gain computation, split scanning and min-gain selection strategies.

Each candidate is evaluated on the current node sample with the standard
second-order gain formula.  Every comparison against the running best
candidate (and the no-split baseline) is recorded with both operands and
their difference, so selection decisions are fully explainable and
re-runnable.  Two selection strategies are supported:

* ``strict``   - a candidate must satisfy ``gain > min_gain`` exactly;
* ``tolerance`` - a candidate is also accepted when it is within
  ``gain_tol`` of ``min_gain``, which keeps candidates sitting right on the
  threshold stable against rounding flips.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .binning import FeatureBinning
from .config import TrainConfig
from .errors import ErrorCode
from .precision import quantize
from .records import attach_degradation, stage_record


@dataclass
class CandidateEval:
    feature_index: int
    threshold: float
    gain: float
    left_count: int
    right_count: int
    rank: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature_index": self.feature_index,
            "threshold": float(self.threshold),
            "gain": float(self.gain),
            "left_count": int(self.left_count),
            "right_count": int(self.right_count),
            "rank": int(self.rank),
        }


@dataclass
class SplitDecision:
    accepted: bool
    feature_index: Optional[int]
    threshold: Optional[float]
    best_gain: float
    strategy: str
    evaluations: List[CandidateEval] = field(default_factory=list)
    comparisons: List[Dict[str, Any]] = field(default_factory=list)
    records: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "accepted": bool(self.accepted),
            "feature_index": self.feature_index,
            "threshold": (
                None if self.threshold is None else float(self.threshold)
            ),
            "best_gain": float(self.best_gain),
            "strategy": self.strategy,
            "evaluations": [ev.to_dict() for ev in self.evaluations],
            "comparisons": self.comparisons,
            "records": self.records,
        }


def _gain_formula(
    grad: np.ndarray,
    hess: np.ndarray,
    left_mask: np.ndarray,
    floor: float,
) -> Tuple[float, int, int]:
    left_g = float(np.sum(grad[left_mask], dtype=np.float64))
    left_h = float(np.sum(hess[left_mask], dtype=np.float64))
    right_g = float(np.sum(grad[~left_mask], dtype=np.float64))
    right_h = float(np.sum(hess[~left_mask], dtype=np.float64))
    parent_g = left_g + right_g
    parent_h = left_h + right_h

    left_h = max(left_h, floor)
    right_h = max(right_h, floor)
    parent_h = max(parent_h, floor)
    gain = (
        (left_g * left_g) / left_h
        + (right_g * right_g) / right_h
        - (parent_g * parent_g) / parent_h
    )
    return gain, int(np.count_nonzero(left_mask)), int(
        np.count_nonzero(~left_mask)
    )


def _candidate_masks(
    X_node: np.ndarray,
    binnings: Sequence[FeatureBinning],
    active_features: Sequence[int],
) -> List[Tuple[int, float, np.ndarray, bool]]:
    masks: List[Tuple[int, float, np.ndarray, bool]] = []
    for feature_index in active_features:
        binning = binnings[feature_index]
        column = X_node[:, feature_index]
        for threshold in binning.candidates:
            left_mask = column <= threshold
            masks.append(
                (feature_index, float(threshold), left_mask, binning.is_categorical)
            )
    # Stable ordering: feature index ascending, then threshold ascending.
    masks.sort(key=lambda item: (item[0], item[1]))
    return masks


def _duplicate_partition(
    masks: Sequence[Tuple[int, float, np.ndarray, bool]],
) -> Optional[Dict[str, Any]]:
    """Detect two numeric candidates inducing the identical row partition."""
    seen: Dict[bytes, Tuple[int, float]] = {}
    for feature_index, threshold, left_mask, is_categorical in masks:
        if is_categorical:
            continue
        key = left_mask.astype(np.uint8).tobytes()
        if key in seen:
            prev_feature, prev_threshold = seen[key]
            return {
                "feature_index": feature_index,
                "threshold": threshold,
                "previous_feature": prev_feature,
                "previous_threshold": prev_threshold,
            }
        seen[key] = (feature_index, threshold)
    return None


def scan_splits(
    X_node: np.ndarray,
    gradients: np.ndarray,
    hessians: np.ndarray,
    sample_indices: np.ndarray,
    binnings: Sequence[FeatureBinning],
    active_features: Sequence[int],
    config: TrainConfig,
    node_path: str,
    *,
    detect_duplicate_threshold: bool = False,
) -> SplitDecision:
    """Evaluate every candidate and apply the configured gain strategy."""
    comparisons: List[Dict[str, Any]] = []
    evaluations: List[CandidateEval] = []
    records: List[Dict[str, Any]] = []

    masks = _candidate_masks(X_node, binnings, active_features)
    duplicate = (
        _duplicate_partition(masks) if detect_duplicate_threshold else None
    )
    if duplicate is not None:
        offending = [
            int(i)
            for i in sample_indices[
                np.nonzero(
                    X_node[:, duplicate["feature_index"]]
                    <= duplicate["threshold"]
                )[0]
            ]
        ]
        record = stage_record(
            "gain",
            ErrorCode.DUPLICATE_THRESHOLD,
            "two numeric thresholds induce the identical split partition",
            sample_indices=offending,
            feature_index=duplicate["feature_index"],
            node_path=node_path,
            details=duplicate,
        )
        attach_degradation(
            record,
            category="skip_round_split",
            trigger_code=ErrorCode.DUPLICATE_THRESHOLD,
            action="keep the node as a leaf and reuse the previous prediction",
        )
        records.append(record)
        return SplitDecision(
            accepted=False,
            feature_index=None,
            threshold=None,
            best_gain=0.0,
            strategy=config.gain_strategy,
            evaluations=[],
            comparisons=[],
            records=records,
        )

    grad_work = quantize(gradients, config)
    hess_work = quantize(hessians, config)

    best: Optional[CandidateEval] = None
    for rank, (feature_index, threshold, left_mask, _) in enumerate(masks):
        gain, left_count, right_count = _gain_formula(
            grad_work,
            hess_work,
            left_mask,
            max(config.hessian_floor, 1e-12),
        )
        if config.precision == "float32":
            gain = float(quantize(np.asarray([gain]), config)[0])
        evaluation = CandidateEval(
            feature_index=feature_index,
            threshold=threshold,
            gain=float(gain),
            left_count=left_count,
            right_count=right_count,
            rank=rank,
        )
        evaluations.append(evaluation)

        # Compare with the no-split baseline (gain == 0).
        comparisons.append(
            {
                "node_path": node_path,
                "left": {"candidate": evaluation.to_dict()},
                "right": {"candidate": "no_split", "gain": 0.0},
                "difference": float(gain - 0.0),
            }
        )
        if best is None:
            best = evaluation
            continue
        difference = gain - best.gain
        comparisons.append(
            {
                "node_path": node_path,
                "left": {"candidate": evaluation.to_dict()},
                "right": {"candidate": best.to_dict()},
                "difference": float(difference),
            }
        )
        # Stable tie-break: only a strictly greater gain replaces the best,
        # so equal gains keep the lower (feature, threshold) candidate.
        if gain > best.gain:
            best = evaluation

    best_gain = best.gain if best is not None else 0.0
    accepted = _apply_gain_strategy(best_gain, config, comparisons, node_path)

    # Respect the minimum leaf sample count.
    if best is not None and (
        best.left_count < config.min_leaf_samples
        or best.right_count < config.min_leaf_samples
    ):
        accepted = False

    return SplitDecision(
        accepted=accepted and best is not None,
        feature_index=best.feature_index if best else None,
        threshold=best.threshold if best else None,
        best_gain=float(best_gain),
        strategy=config.gain_strategy,
        evaluations=evaluations,
        comparisons=comparisons,
        records=records,
    )


def _apply_gain_strategy(
    best_gain: float,
    config: TrainConfig,
    comparisons: List[Dict[str, Any]],
    node_path: str,
) -> bool:
    min_gain = float(config.min_gain)
    if config.gain_strategy == "strict":
        decision = best_gain > min_gain
        comparisons.append(
            {
                "node_path": node_path,
                "left": {"candidate": "best", "gain": float(best_gain)},
                "right": {"candidate": "min_gain", "gain": min_gain},
                "difference": float(best_gain - min_gain),
                "strategy": "strict",
                "accepted": bool(decision),
            }
        )
        return decision

    tol = float(config.gain_tol)
    decision = (best_gain + tol) >= min_gain
    comparisons.append(
        {
            "node_path": node_path,
            "left": {"candidate": "best_plus_tol", "gain": float(best_gain + tol)},
            "right": {"candidate": "min_gain", "gain": min_gain},
            "difference": float((best_gain + tol) - min_gain),
            "strategy": "tolerance",
            "tolerance": tol,
            "accepted": bool(decision),
        }
    )
    return decision
