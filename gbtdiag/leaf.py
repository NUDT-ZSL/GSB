"""Leaf weight solving with numerical stability protection.

The leaf weight is ``-sum(grad) / max(sum(hess), hessian_floor)``.  The
solver reports whether the floor protection engaged, whether a subnormal
value was observed, and handles injected non-finite Hessians through an
explicit degradation path that falls back to the configured default floor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

import numpy as np

from .config import TrainConfig
from .errors import ErrorCode
from .precision import had_subnormal, quantize_scalar
from .records import attach_degradation, stage_record


@dataclass
class LeafResult:
    node_path: str
    weight: float
    grad_sum: float
    hess_sum: float
    floor_protected: bool
    subnormal_observed: bool
    nonfinite_hessian: bool
    records: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_path": self.node_path,
            "weight": float(self.weight),
            "grad_sum": float(self.grad_sum),
            "hess_sum": float(self.hess_sum),
            "floor_protected": bool(self.floor_protected),
            "subnormal_observed": bool(self.subnormal_observed),
            "nonfinite_hessian": bool(self.nonfinite_hessian),
            "records": self.records,
        }


def solve_leaf(
    gradients: np.ndarray,
    hessians: np.ndarray,
    sample_indices: np.ndarray,
    config: TrainConfig,
    node_path: str,
) -> LeafResult:
    """Solve one leaf and return the weight plus protection metadata."""
    config.validate_floor("hessian_floor", config.hessian_floor, node_path)
    config.validate_floor(
        "hessian_floor_default", config.hessian_floor_default, node_path
    )

    records: List[Dict[str, Any]] = []
    finite_mask = np.isfinite(gradients) & np.isfinite(hessians)
    nonfinite_rows = [
        int(sample_indices[pos])
        for pos in np.nonzero(~finite_mask)[0]
    ]
    nonfinite_observed = bool(nonfinite_rows)

    grad_work = np.array(gradients, dtype=np.float64, copy=True)
    hess_work = np.array(hessians, dtype=np.float64, copy=True)
    if nonfinite_observed:
        # Injected E_NONFINITE_HESSIAN: degrade to safe finite replacements
        # and the documented default floor, rather than aborting the round.
        record = stage_record(
            "leaf",
            ErrorCode.NONFINITE_HESSIAN,
            f"node '{node_path}' received non-finite Hessian values",
            sample_indices=nonfinite_rows,
            node_path=node_path,
        )
        attach_degradation(
            record,
            category="default_floor_fallback",
            trigger_code=ErrorCode.NONFINITE_HESSIAN,
            action="replace non-finite pairs with (grad=0, hess=floor_default)",
            details={
                "fallback_floor": float(config.hessian_floor_default),
            },
        )
        records.append(record)
        grad_work[~finite_mask] = 0.0
        hess_work[~finite_mask] = float(config.hessian_floor_default)

    grad_sum = float(np.sum(grad_work, dtype=np.float64))
    hess_sum_raw = float(np.sum(hess_work, dtype=np.float64))
    effective_hess = max(hess_sum_raw, float(config.hessian_floor))
    floor_protected = effective_hess != hess_sum_raw

    raw_weight = -grad_sum / effective_hess
    subnormal = had_subnormal([raw_weight], config.precision)
    weight = quantize_scalar(raw_weight, config)

    return LeafResult(
        node_path=node_path,
        weight=float(weight),
        grad_sum=grad_sum,
        hess_sum=effective_hess,
        floor_protected=floor_protected,
        subnormal_observed=subnormal,
        nonfinite_hessian=nonfinite_observed,
        records=records,
    )
