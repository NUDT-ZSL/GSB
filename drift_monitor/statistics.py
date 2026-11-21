"""Per-batch review and statistic accumulation.

Numeric and categorical columns use deliberately distinct calibers:

* numeric columns keep sufficient statistics (count / sum / sum of squares)
  plus a sorted copy of the observed values so quantiles are exact;
* categorical columns keep level frequency maps, a sorted level universe and
  an explicit missing count.

Window statistics are always derivable by folding the per-batch records in
ascending batch-id order, which makes "exact removal" verifiable: we both
subtract the leaving batch's contribution algebraically and independently
recompute from the surviving batch details.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from .config_model import CATEGORICAL, NUMERIC, FeatureSpec
from .data import Batch
from .errors import DriftError, ErrorCode
from .hashing import content_hash

STAT_SCHEMA_VERSION = "stat-caliber-v1"


@dataclass
class NumericStat:
    count: int
    total: int
    missing: int
    sum: float
    sum_squares: float
    sorted_values: List[float]

    @property
    def mean(self) -> float:
        return self.sum / self.count if self.count else 0.0

    @property
    def variance(self) -> float:
        if self.count < 2:
            return 0.0
        centered = self.sum_squares - (self.sum * self.sum) / self.count
        centered = max(centered, 0.0)
        return centered / (self.count - 1)

    def quantiles(self, qs: Sequence[float]) -> List[float]:
        if not self.sorted_values:
            return [0.0 for _ in qs]
        arr = np.asarray(self.sorted_values, dtype=np.float64)
        return [float(np.quantile(arr, q, method="linear")) for q in qs]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "caliber": "numeric",
            "count": self.count,
            "total": self.total,
            "missing": self.missing,
            "sum": self.sum,
            "sum_squares": self.sum_squares,
            "mean": self.mean,
            "variance": self.variance,
            "missing_rate": self.missing / self.total if self.total else 0.0,
        }


@dataclass
class CategoricalStat:
    total: int
    missing: int
    frequencies: Dict[str, int]

    @property
    def observed(self) -> int:
        return self.total - self.missing

    def proportions(self, levels: Optional[Sequence[str]] = None) -> Dict[str, float]:
        universe = list(levels) if levels is not None else sorted(self.frequencies)
        denominator = self.observed
        if denominator == 0:
            return {level: 0.0 for level in universe}
        return {
            level: self.frequencies.get(level, 0) / denominator for level in universe
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "caliber": "categorical",
            "total": self.total,
            "missing": self.missing,
            "missing_rate": self.missing / self.total if self.total else 0.0,
            "frequencies": {k: self.frequencies[k] for k in sorted(self.frequencies)},
            "proportions": self.proportions(),
        }


@dataclass
class BatchStat:
    batch_id: int
    sample_count: int
    numeric: Dict[str, NumericStat] = field(default_factory=dict)
    categorical: Dict[str, CategoricalStat] = field(default_factory=dict)
    batch_fingerprint: str = ""

    def to_cache_dict(self) -> Dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "sample_count": self.sample_count,
            "batch_fingerprint": self.batch_fingerprint,
            "numeric": {
                name: {
                    **self.numeric[name].to_dict(),
                    "sorted_values": list(self.numeric[name].sorted_values),
                }
                for name in sorted(self.numeric)
            },
            "categorical": {
                name: self.categorical[name].to_dict() for name in sorted(self.categorical)
            },
        }


def compute_batch_stat(batch: Batch, specs: Dict[str, FeatureSpec]) -> BatchStat:
    if batch.sample_count <= 0:
        raise DriftError(
            ErrorCode.EMPTY_BATCH,
            "cannot accumulate statistics for an empty batch",
            {"batch_id": batch.batch_id},
        )
    numeric_stats: Dict[str, NumericStat] = {}
    categorical_stats: Dict[str, CategoricalStat] = {}
    total = batch.sample_count

    for feature in sorted(specs):
        spec = specs[feature]
        values = batch.columns.get(feature)
        if values is None:
            raise DriftError(
                ErrorCode.ILLEGAL_VALUE,
                "configured feature is absent from the batch",
                {"batch_id": batch.batch_id, "feature": feature},
            )
        if spec.column_type == NUMERIC:
            numeric_stats[feature] = _numeric_stat(batch.batch_id, feature, values, total)
        elif spec.column_type == CATEGORICAL:
            categorical_stats[feature] = _categorical_stat(
                batch.batch_id, feature, values, total
            )
        else:
            raise DriftError(
                ErrorCode.TYPE_UNSUPPORTED,
                f"column type {spec.column_type!r} is not supported",
                {"batch_id": batch.batch_id, "feature": feature, "field": "column_type"},
            )

    stat = BatchStat(
        batch_id=batch.batch_id,
        sample_count=total,
        numeric=numeric_stats,
        categorical=categorical_stats,
        batch_fingerprint=batch.fingerprint(),
    )
    return stat


def _numeric_stat(batch_id: int, feature: str, values: List[Any], total: int) -> NumericStat:
    clean: List[float] = []
    missing = 0
    for position, value in enumerate(values):
        if value is None:
            missing += 1
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise DriftError(
                ErrorCode.ILLEGAL_VALUE,
                "numeric column contains an illegal marker",
                {"batch_id": batch_id, "feature": feature, "row": position, "field": "value"},
            )
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            raise DriftError(
                ErrorCode.ILLEGAL_VALUE,
                "numeric column contains NaN or infinity",
                {"batch_id": batch_id, "feature": feature, "row": position, "field": "value"},
            )
        clean.append(number)
    arr = np.asarray(clean, dtype=np.float64)
    return NumericStat(
        count=len(clean),
        total=total,
        missing=missing,
        sum=math.fsum(clean) if clean else 0.0,
        sum_squares=math.fsum(x * x for x in clean) if clean else 0.0,
        sorted_values=sorted(clean),
    )


def _categorical_stat(
    batch_id: int, feature: str, values: List[Any], total: int
) -> CategoricalStat:
    frequencies: Dict[str, int] = {}
    missing = 0
    for position, value in enumerate(values):
        if value is None:
            missing += 1
            continue
        if not isinstance(value, str):
            raise DriftError(
                ErrorCode.ILLEGAL_VALUE,
                "categorical column contains an illegal non-string marker",
                {"batch_id": batch_id, "feature": feature, "row": position, "field": "value"},
            )
        frequencies[value] = frequencies.get(value, 0) + 1
    return CategoricalStat(total=total, missing=missing, frequencies=dict(sorted(frequencies.items())))


def merge_numeric(parts: Sequence[NumericStat]) -> NumericStat:
    all_values: List[float] = []
    total = 0
    missing = 0
    accum_sum = 0.0
    accum_sq = 0.0
    count = 0
    for part in parts:
        all_values.extend(part.sorted_values)
        total += part.total
        missing += part.missing
        count += part.count
    ordered = sorted(all_values)
    return NumericStat(
        count=count,
        total=total,
        missing=missing,
        sum=math.fsum(ordered) if ordered else 0.0,
        sum_squares=math.fsum(x * x for x in ordered) if ordered else 0.0,
        sorted_values=ordered,
    )


def subtract_numeric(agg: NumericStat, leaving: NumericStat) -> NumericStat:
    """Algebraic removal of one batch contribution (exact sufficient stats)."""
    remaining_values = _subtract_sorted_multiset(
        agg.sorted_values, leaving.sorted_values
    )
    ordered = sorted(remaining_values)
    return NumericStat(
        count=agg.count - leaving.count,
        total=agg.total - leaving.total,
        missing=agg.missing - leaving.missing,
        sum=math.fsum(ordered) if ordered else 0.0,
        sum_squares=math.fsum(x * x for x in ordered) if ordered else 0.0,
        sorted_values=ordered,
    )


def _subtract_sorted_multiset(
    aggregate: List[float], leaving: List[float]
) -> List[float]:
    """Remove the sorted multiset ``leaving`` from ``aggregate`` exactly."""
    result: List[float] = []
    left_index = 0
    right_index = 0
    while left_index < len(aggregate):
        value = aggregate[left_index]
        left_end = left_index + 1
        while left_end < len(aggregate) and aggregate[left_end] == value:
            left_end += 1
        right_end = right_index
        while right_end < len(leaving) and leaving[right_end] == value:
            right_end += 1
        keep_count = (left_end - left_index) - (right_end - right_index)
        if keep_count < 0:
            raise ValueError("cannot remove a value absent from the aggregate multiset")
        result.extend([value] * keep_count)
        right_index = right_end
        left_index = left_end
    if right_index != len(leaving):
        raise ValueError("leaving multiset contains values absent from the aggregate")
    return result


def merge_categorical(parts: Sequence[CategoricalStat]) -> CategoricalStat:
    total = 0
    missing = 0
    frequencies: Dict[str, int] = {}
    for part in parts:
        total += part.total
        missing += part.missing
        for level, freq in part.frequencies.items():
            frequencies[level] = frequencies.get(level, 0) + freq
    return CategoricalStat(
        total=total, missing=missing, frequencies=dict(sorted(frequencies.items()))
    )


def subtract_categorical(
    agg: CategoricalStat, leaving: CategoricalStat
) -> CategoricalStat:
    frequencies = dict(agg.frequencies)
    for level, freq in leaving.frequencies.items():
        frequencies[level] = frequencies.get(level, 0) - freq
        if frequencies[level] == 0:
            del frequencies[level]
    return CategoricalStat(
        total=agg.total - leaving.total,
        missing=agg.missing - leaving.missing,
        frequencies=dict(sorted(frequencies.items())),
    )


def fold_window(
    batch_stats: Sequence[BatchStat],
    specs: Dict[str, FeatureSpec],
) -> Dict[str, Any]:
    """Fold per-batch records in ascending batch order into window stats."""
    ordered = sorted(batch_stats, key=lambda item: item.batch_id)
    result: Dict[str, Any] = {
        "batch_ids": [item.batch_id for item in ordered],
        "numeric": {},
        "categorical": {},
    }
    for feature in sorted(specs):
        spec = specs[feature]
        if spec.column_type == NUMERIC:
            merged = merge_numeric([item.numeric[feature] for item in ordered])
            result["numeric"][feature] = merged
        else:
            merged = merge_categorical([item.categorical[feature] for item in ordered])
            result["categorical"][feature] = merged
    return result


def cumulative_stats(
    batch_stats: Sequence[BatchStat],
    specs: Dict[str, FeatureSpec],
) -> Dict[str, Any]:
    return fold_window(batch_stats, specs)


def stat_record_hash(stat: BatchStat) -> str:
    return content_hash(
        {
            "schema": STAT_SCHEMA_VERSION,
            "record": stat.to_cache_dict(),
        }
    )


def describe_numeric(stat: NumericStat, quantiles: Sequence[float]) -> Dict[str, Any]:
    q_values = stat.quantiles(quantiles)
    return {
        **stat.to_dict(),
        "quantiles": {
            _format_quantile(q): value for q, value in zip(quantiles, q_values)
        },
    }


def describe_categorical(stat: CategoricalStat) -> Dict[str, Any]:
    return stat.to_dict()


def _format_quantile(q: float) -> str:
    # Canonical label independent of float repr noise (e.g. 0.25 -> "q0.25").
    return f"q{q:.6f}".rstrip("0").rstrip(".")
