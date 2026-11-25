"""Canonical segmented threshold model and deterministic adjudication."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np

from .io_utils import payload_checksum


QUANTILE_PROBS: tuple[float, ...] = (0.50, 0.90, 0.95, 0.99)
GLOBAL_SEGMENT = "__global__"
EFFECTIVE_TIER = "effective"


def prob_label(prob: float) -> str:
    return f"p{int(round(float(prob) * 100)):02d}"


def empirical_quantiles(
    values: Sequence[float] | np.ndarray,
    probs: Sequence[float] = QUANTILE_PROBS,
) -> dict[str, float]:
    array = np.sort(np.asarray(list(values), dtype=np.float64).reshape(-1))
    if array.size == 0:
        raise ValueError("cannot estimate quantiles from an empty sample")
    result = {}
    last = float(array[-1])
    for prob in probs:
        if array.size == 1:
            value = float(array[0])
        else:
            position = float(prob) * (array.size - 1)
            low = int(np.floor(position))
            high = int(np.ceil(position))
            fraction = position - low
            value = float(array[low] * (1.0 - fraction) + array[high] * fraction)
        result[prob_label(prob)] = value
    # Explicit maximum is useful for tiny calibration batches.
    result.setdefault("pmax", last)
    return result


@dataclass
class ThresholdRow:
    segment_id: str
    metric: str = "score"
    tier: str = EFFECTIVE_TIER
    granularity: str = "window_segment"
    quantiles: dict[str, float] = field(default_factory=dict)
    sample_count: int = 0
    condition: dict | None = None
    inherited_from: str | None = None
    checksum: str = ""

    def recompute_checksum(self) -> str:
        payload = {
            "segment_id": self.segment_id,
            "metric": self.metric,
            "tier": self.tier,
            "granularity": self.granularity,
            "quantiles": self.quantiles,
            "condition": self.condition,
            "inherited_from": self.inherited_from,
        }
        self.checksum = payload_checksum(payload)
        return self.checksum

    def to_dict(self) -> dict:
        return {
            "segment_id": self.segment_id,
            "metric": self.metric,
            "tier": self.tier,
            "granularity": self.granularity,
            "quantiles": self.quantiles,
            "sample_count": self.sample_count,
            "condition": self.condition,
            "inherited_from": self.inherited_from,
            "checksum": self.checksum or self.recompute_checksum(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ThresholdRow":
        row = cls(
            segment_id=str(data["segment_id"]),
            metric=str(data.get("metric", "score")),
            tier=str(data.get("tier", EFFECTIVE_TIER)),
            granularity=str(data.get("granularity", "window_segment")),
            quantiles={k: float(v) for k, v in data.get("quantiles", {}).items()},
            sample_count=int(data.get("sample_count", 0)),
            condition=data.get("condition"),
            inherited_from=data.get("inherited_from"),
            checksum=str(data.get("checksum", "")),
        )
        if not row.checksum:
            row.recompute_checksum()
        return row

    @property
    def warn(self) -> float:
        return float(self.quantiles.get("p95", self.quantiles.get("p90", next(iter(self.quantiles.values())))))

    @property
    def alarm(self) -> float:
        return float(self.quantiles.get("p99", self.quantiles.get("pmax", self.warn)))


@dataclass
class ThresholdModel:
    model_version: int = 1
    rows: list[ThresholdRow] = field(default_factory=list)
    quantile_probs: list[float] = field(default_factory=lambda: list(QUANTILE_PROBS))
    default_granularity: str = "window_segment"
    tolerance_ratio: float = 0.02
    calibration_path: list[str] = field(default_factory=list)
    migration_sources: list[dict] = field(default_factory=list)
    checksum: str = ""

    def row(
        self,
        segment_id: str,
        metric: str = "score",
        tier: str = EFFECTIVE_TIER,
    ) -> ThresholdRow | None:
        for item in self.rows:
            if item.segment_id == segment_id and item.metric == metric and item.tier == tier:
                return item
        return None

    def effective_row(self, segment_id: str, metric: str = "score") -> ThresholdRow:
        row = self.row(segment_id, metric, EFFECTIVE_TIER)
        if row is not None:
            return row
        global_row = self.row(GLOBAL_SEGMENT, metric, EFFECTIVE_TIER)
        if global_row is not None:
            return global_row
        if self.rows:
            for item in self.rows:
                if item.metric == metric and item.tier == EFFECTIVE_TIER:
                    return item
        raise KeyError(f"no effective threshold for segment={segment_id!r} metric={metric!r}")

    def segment_ids(self, include_global: bool = False) -> list[str]:
        result = sorted(
            {
                row.segment_id
                for row in self.rows
                if row.tier == EFFECTIVE_TIER and row.metric == "score"
                and (include_global or row.segment_id != GLOBAL_SEGMENT)
            }
        )
        return result

    def canonical_dict(self) -> dict:
        rows = sorted(
            (row.to_dict() for row in self.rows),
            key=lambda item: (item["segment_id"], item["tier"], item["metric"]),
        )
        return {
            "model_version": int(self.model_version),
            "quantile_probs": [float(p) for p in self.quantile_probs],
            "default_granularity": self.default_granularity,
            "tolerance_ratio": float(self.tolerance_ratio),
            "calibration_path": list(self.calibration_path),
            "rows": rows,
        }

    def recompute_checksum(self) -> str:
        self.checksum = payload_checksum(self.canonical_dict())
        return self.checksum

    def to_dict(self) -> dict:
        data = self.canonical_dict()
        data["format"] = "canonical-threshold-model"
        data["checksum"] = self.checksum or self.recompute_checksum()
        data["migration_sources"] = list(self.migration_sources)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ThresholdModel":
        model = cls(
            model_version=int(data.get("model_version", 1)),
            rows=[ThresholdRow.from_dict(item) for item in data.get("rows", [])],
            quantile_probs=[float(p) for p in data.get("quantile_probs", QUANTILE_PROBS)],
            default_granularity=str(data.get("default_granularity", "window_segment")),
            tolerance_ratio=float(data.get("tolerance_ratio", 0.02)),
            calibration_path=list(data.get("calibration_path", [])),
            migration_sources=list(data.get("migration_sources", [])),
            checksum=str(data.get("checksum", "")),
        )
        if not model.checksum:
            model.recompute_checksum()
        return model


def band_for_row(row: ThresholdRow, tolerance_ratio: float = 0.02) -> dict[str, float | str]:
    alarm = row.alarm
    tolerance = max(1e-9, abs(alarm) * float(tolerance_ratio))
    return {
        "metric": row.metric,
        "warn": float(row.warn),
        "alarm": float(alarm),
        "tolerance": float(tolerance),
        "alarm_lower": float(alarm - tolerance),
        "alarm_upper": float(alarm + tolerance),
        "quantiles": dict(row.quantiles),
        "segment_checksum": row.checksum,
    }


def adjudicate(
    score: float,
    segment_id: str,
    model: ThresholdModel,
    strategy: str = "strict",
    tolerance: float | None = None,
    metric: str = "score",
) -> dict:
    """Adjudicate a score using either strict or tolerance-band semantics.

    Strict uses ``score >= alarm``.  Tolerance band uses a symmetric interval:
    above the upper edge is an alarm; inside the interval is a reviewable
    boundary; below the lower edge is normal.  Warnings are retained when the
    score reaches the warning quantile.
    """

    if strategy not in {"strict", "tolerance_band"}:
        raise ValueError(f"unknown threshold strategy: {strategy}")
    row = model.effective_row(segment_id, metric=metric)
    ratio = model.tolerance_ratio
    band = band_for_row(row, ratio)
    tol = float(band["tolerance"] if tolerance is None else tolerance)
    value = float(score)
    alarm = float(band["alarm"])
    warn = float(band["warn"])

    if strategy == "strict":
        is_anomaly = bool(value >= alarm)
        if is_anomaly:
            severity = "alarm"
        elif value >= warn:
            severity = "warning"
        else:
            severity = "normal"
        in_band = False
        comparator = "score >= alarm"
    else:
        lower = alarm - tol
        upper = alarm + tol
        in_band = bool(lower <= value <= upper)
        if value > upper:
            severity = "alarm"
            is_anomaly = True
        elif in_band:
            severity = "boundary"
            # Boundary cases are not asserted as anomalies; replay records and
            # operators can promote them through pseudo-label correction.
            is_anomaly = False
        elif value >= warn:
            severity = "warning"
            is_anomaly = False
        else:
            severity = "normal"
            is_anomaly = False
        comparator = "score > alarm + tolerance"

    return {
        "segment_id": row.segment_id,
        "requested_segment_id": segment_id,
        "used_global_fallback": row.segment_id == GLOBAL_SEGMENT and segment_id != GLOBAL_SEGMENT,
        "metric": metric,
        "strategy": strategy,
        "comparator": comparator,
        "score": value,
        "severity": severity,
        "is_anomaly": is_anomaly,
        "in_tolerance_band": in_band,
        "band": band,
        "threshold_model_version": model.model_version,
        "threshold_model_checksum": model.checksum,
        "threshold_row_checksum": row.checksum,
    }


def build_rows_from_samples(
    samples_by_segment: dict[str, Iterable[float]],
    model_version: int = 1,
    tier: str = EFFECTIVE_TIER,
    metric: str = "score",
    granularity: str | None = None,
    probs: Sequence[float] = QUANTILE_PROBS,
) -> list[ThresholdRow]:
    rows = []
    for segment_id in sorted(samples_by_segment):
        values = list(samples_by_segment[segment_id])
        if not values:
            continue
        row = ThresholdRow(
            segment_id=segment_id,
            metric=metric,
            tier=tier,
            granularity=granularity or ("flat" if segment_id == GLOBAL_SEGMENT else "window_segment"),
            quantiles=empirical_quantiles(values, probs),
            sample_count=len(values),
        )
        row.recompute_checksum()
        rows.append(row)
    return rows
