"""Deterministic feature binning and split candidate generation.

Numeric features are split with quantile thresholds and categorical
features use a fixed integer-code encoding.  All outputs are plain lists of
Python floats so that identical inputs/configs serialise to identical
bytes.  Ties are broken by feature index ascending, then threshold
ascending (see :func:`ordered_candidates`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

from .config import TrainConfig
from .errors import ErrorCode
from .records import attach_degradation, stage_record


@dataclass(frozen=True)
class FeatureBinning:
    feature_index: int
    is_categorical: bool
    edges: Tuple[float, ...]
    candidates: Tuple[float, ...]
    cardinality: int | None = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature_index": self.feature_index,
            "is_categorical": self.is_categorical,
            "edges": list(self.edges),
            "candidates": list(self.candidates),
            "cardinality": self.cardinality,
        }


@dataclass
class BinningResult:
    feature_binnings: List[FeatureBinning]
    ordered_candidates: List[Tuple[int, float]]
    excluded_rows: List[int] = field(default_factory=list)
    records: List[Dict[str, Any]] = field(default_factory=list)

    def candidate_lookup(self) -> Dict[Tuple[int, float], int]:
        return {
            (feature_index, threshold): rank
            for rank, (feature_index, threshold) in enumerate(
                self.ordered_candidates
            )
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature_binnings": [fb.to_dict() for fb in self.feature_binnings],
            "ordered_candidates": [
                {"feature_index": fi, "threshold": float(t), "rank": rank}
                for rank, (fi, t) in enumerate(self.ordered_candidates)
            ],
            "excluded_rows": list(self.excluded_rows),
            "records": self.records,
        }


def _numeric_binning(column: np.ndarray, n_thresholds: int) -> FeatureBinning:
    unique_values = np.unique(column)
    if unique_values.size <= 1:
        # Constant column: no split is possible.
        edges = (float(unique_values[0]),)
        return FeatureBinning(
            feature_index=-1,
            is_categorical=False,
            edges=edges,
            candidates=(),
        )

    quantiles = np.linspace(0.0, 1.0, n_thresholds + 1)[1:-1]
    raw_edges = np.quantile(unique_values, quantiles, method="linear")
    thresholds = np.unique(raw_edges)
    candidates = tuple(float(edge) for edge in thresholds)
    # Bin boundaries extend to the infinite ends so every value maps to a bin.
    edges = (
        (float("-inf"),)
        + tuple(float(edge) for edge in thresholds)
        + (float("inf"),)
    )
    return FeatureBinning(
        feature_index=-1,
        is_categorical=False,
        edges=edges,
        candidates=candidates,
    )


def _categorical_binning(
    feature_index: int,
    column: np.ndarray,
    cardinality: int,
) -> Tuple[FeatureBinning, List[int]]:
    """Encode categories by fixed integer code and detect out-of-range rows."""
    codes = np.rint(column).astype(np.int64)
    invalid = np.nonzero((codes < 0) | (codes >= cardinality))[0]
    excluded = [int(i) for i in invalid]
    # Candidates are "code <= t" split points over valid codes.
    candidates = tuple(float(t) for t in range(cardinality - 1))
    edges = tuple(float(t) for t in range(cardinality))
    binning = FeatureBinning(
        feature_index=feature_index,
        is_categorical=True,
        edges=edges,
        candidates=candidates,
        cardinality=cardinality,
    )
    return binning, excluded


def build_binnings(
    X: np.ndarray,
    categorical_mask: Sequence[bool],
    cardinalities: Sequence[int],
    config: TrainConfig,
) -> BinningResult:
    """Produce deterministic per-feature binnings and ordered candidates."""
    config.validate_basic()
    n_features = X.shape[1]
    binnings: List[FeatureBinning] = []
    excluded_rows: set[int] = set()
    records: List[Dict[str, Any]] = []

    categorical_cardinalities = list(cardinalities)
    cat_counter = 0
    for j in range(n_features):
        column = X[:, j]
        if categorical_mask[j]:
            cardinality = int(categorical_cardinalities[cat_counter])
            cat_counter += 1
            binning, excluded = _categorical_binning(
                j, column, cardinality
            )
            if excluded:
                record = stage_record(
                    "binning",
                    ErrorCode.CATEGORY_OUT_OF_RANGE,
                    f"feature {j} contains {len(excluded)} categorical "
                    "values outside the declared cardinality",
                    sample_indices=excluded,
                    feature_index=j,
                    details={"cardinality": cardinality},
                )
                attach_degradation(
                    record,
                    category="row_quarantine",
                    trigger_code=ErrorCode.CATEGORY_OUT_OF_RANGE,
                    action="exclude offending rows from every tree fitting pool",
                    details={"cardinality": cardinality},
                )
                records.append(record)
                excluded_rows.update(excluded)
        else:
            binning = _numeric_binning(column, config.n_thresholds)
            binning = FeatureBinning(
                feature_index=j,
                is_categorical=False,
                edges=binning.edges,
                candidates=binning.candidates,
            )
        binnings.append(binning)

    ordered = ordered_candidates(binnings)
    return BinningResult(
        feature_binnings=binnings,
        ordered_candidates=ordered,
        excluded_rows=sorted(excluded_rows),
        records=records,
    )


def ordered_candidates(
    binnings: Sequence[FeatureBinning],
) -> List[Tuple[int, float]]:
    """Flatten candidates with the mandated tie-break ordering.

    Primary ordering is feature index ascending; within a feature the
    thresholds are ascending.  The resulting list defines a stable global
    rank for every candidate.
    """
    flattened: List[Tuple[int, float]] = []
    for binning in sorted(binnings, key=lambda b: b.feature_index):
        for threshold in sorted(binning.candidates):
            flattened.append((binning.feature_index, float(threshold)))
    return flattened


def assign_bins(
    X: np.ndarray, binning: FeatureBinning
) -> np.ndarray:
    """Return the zero-based bin id of every row for one feature."""
    column = X[:, binning.feature_index]
    if binning.is_categorical:
        return np.clip(np.rint(column).astype(np.int64), 0, binning.cardinality - 1)
    # Edges are (-inf, t1, ..., tk, +inf); searchsorted yields the bin id.
    edges = np.asarray(binning.edges, dtype=np.float64)
    return np.searchsorted(edges, column, side="left") - 1
