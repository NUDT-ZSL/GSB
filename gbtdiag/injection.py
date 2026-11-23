"""Four-stage anomaly injection framework.

Injections are pure in-memory data/parameter mutations selected by an
explicit :class:`InjectionSpec` list passed to the training entry point.
Source code is never modified.  Every stage detects its own injected
condition through normal validation and emits a record carrying the stage
name, business code and sample indices.

Stages and trigger conditions
------------------------------
``binning``    - ``category_oor``: overwrite categorical codes with values
                 outside the declared cardinality.
``gain``       - ``duplicate_threshold``: collapse a numeric column so two
                 quantile thresholds induce the identical partition.
``leaf``       - ``nonfinite_hessian``: inject NaN/Inf Hessians on rows.
``prediction`` - ``negative_learning_rate``: override the learning rate
                 with a negative value.

``raw_fault`` additionally raises an unexpected exception inside a chosen
stage to exercise the unattributed-exception reporting path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from .config import TrainConfig
from .errors import ErrorCode


INJECTION_KINDS = (
    "category_oor",
    "duplicate_threshold",
    "nonfinite_hessian",
    "negative_learning_rate",
    "raw_fault",
)

STAGE_OF_KIND = {
    "category_oor": "binning",
    "duplicate_threshold": "gain",
    "nonfinite_hessian": "leaf",
    "negative_learning_rate": "prediction",
}


@dataclass(frozen=True)
class InjectionSpec:
    kind: str
    stage: str
    round_index: Optional[int] = None
    feature_index: Optional[int] = None
    sample_indices: Optional[List[int]] = None
    value: float = -1.0
    n_samples: int = 8
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "stage": self.stage,
            "round_index": self.round_index,
            "feature_index": self.feature_index,
            "sample_indices": list(self.sample_indices)
            if self.sample_indices
            else [],
            "value": float(self.value),
            "n_samples": int(self.n_samples),
            "details": dict(self.details),
        }


def make_injection(kind: str, **kwargs: Any) -> InjectionSpec:
    if kind not in INJECTION_KINDS:
        raise ValueError(f"unknown injection kind {kind!r}")
    stage = STAGE_OF_KIND.get(kind, kwargs.pop("stage", "unknown"))
    return InjectionSpec(kind=kind, stage=stage, **kwargs)


class InjectionController:
    """Applies injections to in-memory copies without mutating the dataset."""

    def __init__(
        self,
        specs: Sequence[InjectionSpec],
        config: TrainConfig,
        n_rows: int,
    ) -> None:
        self.specs = list(specs)
        self.config = config
        rng = config.rng("injection")
        self._default_rows = rng.choice(
            n_rows, size=min(8, n_rows), replace=False
        ).tolist()

    def rows_for(self, spec: InjectionSpec) -> List[int]:
        if spec.sample_indices:
            return [int(i) for i in spec.sample_indices]
        return list(self._default_rows[: spec.n_samples])

    def active(self, kind: str, round_index: Optional[int]) -> List[InjectionSpec]:
        result = []
        for spec in self.specs:
            if spec.kind != kind:
                continue
            if spec.round_index is None or spec.round_index == round_index:
                result.append(spec)
        return result

    def apply_binning_injections(
        self,
        X: np.ndarray,
        categorical_features: Sequence[int],
    ) -> np.ndarray:
        work = X.copy()
        for spec in self.active("category_oor", None):
            feature = (
                spec.feature_index
                if spec.feature_index is not None
                else categorical_features[0]
            )
            rows = self.rows_for(spec)
            work[np.asarray(rows), feature] = float(spec.value)
        return work

    def apply_gain_injections(
        self, X: np.ndarray, numeric_features: Sequence[int], round_index: int
    ) -> np.ndarray:
        work = X
        for spec in self.active("duplicate_threshold", round_index):
            work = X.copy() if work is X else work
            feature = (
                spec.feature_index
                if spec.feature_index is not None
                else numeric_features[0]
            )
            # Collapse the column for the selected rows so a quantile edge
            # duplicates an existing partition boundary.
            rows = np.asarray(self.rows_for(spec))
            reference = float(work[:, feature].min())
            work[rows, feature] = reference
        return work

    def duplicate_active(self, round_index: int) -> bool:
        """Whether the duplicate-threshold injection fires this round."""
        return bool(self.active("duplicate_threshold", round_index))

    def apply_leaf_injections(
        self,
        hessians: np.ndarray,
        round_index: int,
    ) -> np.ndarray:
        work = hessians
        for spec in self.active("nonfinite_hessian", round_index):
            work = hessians.copy() if work is hessians else work
            rows = np.asarray(self.rows_for(spec))
            work[rows] = float(spec.value) if np.isfinite(spec.value) else np.nan
        return work

    def effective_learning_rate(
        self, round_index: int
    ) -> tuple[float, List[InjectionSpec]]:
        specs = self.active("negative_learning_rate", round_index)
        if not specs:
            return float(self.config.learning_rate), []
        return float(specs[0].value if specs[0].value < 0 else -1.0), specs

    def raw_fault_active(self, stage: str, round_index: int) -> Optional[InjectionSpec]:
        for spec in self.specs:
            if (
                spec.kind == "raw_fault"
                and spec.stage == stage
                and (spec.round_index is None or spec.round_index == round_index)
            ):
                return spec
        return None


def injection_summary(specs: Sequence[InjectionSpec]) -> List[Dict[str, Any]]:
    return [spec.to_dict() for spec in specs]


__all__ = [
    "INJECTION_KINDS",
    "InjectionController",
    "InjectionSpec",
    "ErrorCode",
    "injection_summary",
    "make_injection",
]
