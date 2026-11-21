"""Deterministic window/full statistics and feature snapshot artifacts."""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

from .canonical import sha256_obj, stable_float
from .models import CATEGORY, NUMERIC, Batch, FeatureSpec, MonitorConfig


EPSILON = 1e-12
QUANTILE_METHOD = "linear_interpolation"


def _quantile_key(probability: float) -> str:
    return format(float(probability), ".12g")


def _linear_quantile(sorted_values: Sequence[float], probability: float) -> float:
    if len(sorted_values) == 1:
        return stable_float(sorted_values[0])
    position = probability * (len(sorted_values) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    fraction = position - lower
    value = sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction
    return stable_float(value)


def numeric_statistics(values: Iterable[float | str | None], quantiles: Sequence[float]) -> dict[str, Any]:
    clean = [float(value) for value in values if value is not None]
    total_count = len(clean)
    if not clean:
        return {
            "kind": NUMERIC,
            "total_count": 0,
            "valid_count": 0,
            "missing_count": 0,
            "mean": None,
            "variance": None,
            "std": None,
            "min": None,
            "max": None,
            "sum": None,
            "quantiles": {_quantile_key(q): None for q in quantiles},
            "quantile_method": QUANTILE_METHOD,
        }
    mean = stable_float(math.fsum(clean) / total_count)
    variance = stable_float(math.fsum((value - mean) ** 2 for value in clean) / total_count)
    standard_deviation = stable_float(math.sqrt(max(variance, 0.0)))
    ordered = sorted(clean)
    assert total_count == len(ordered)
    return {
        "kind": NUMERIC,
        "total_count": total_count,
        "valid_count": total_count,
        "missing_count": 0,
        "mean": mean,
        "variance": variance,
        "std": standard_deviation,
        "min": stable_float(ordered[0]),
        "max": stable_float(ordered[-1]),
        "sum": stable_float(math.fsum(clean)),
        "quantiles": {
            _quantile_key(probability): _linear_quantile(ordered, probability)
            for probability in quantiles
        },
        "quantile_method": QUANTILE_METHOD,
    }


def category_statistics(
    values: Iterable[float | str | None], levels: Sequence[str]
) -> dict[str, Any]:
    ordered_levels = sorted(levels)
    counts = {level: 0 for level in ordered_levels}
    missing_count = 0
    for value in values:
        if value is None:
            missing_count += 1
            continue
        text = str(value)
        counts[text] = counts.get(text, 0) + 1
    total_count = sum(counts.values())
    proportions = {
        level: stable_float((counts[level] / total_count) if total_count else 0.0)
        for level in ordered_levels
    }
    mode = None
    if total_count:
        mode = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[0][0]
    return {
        "kind": CATEGORY,
        "total_count": total_count + missing_count,
        "valid_count": total_count,
        "missing_count": missing_count,
        "levels": ordered_levels,
        "counts": counts,
        "proportions": proportions,
        "mode": mode,
    }


def compute_statistics(
    feature: FeatureSpec,
    values: Iterable[float | str | None],
    quantiles: Sequence[float],
) -> dict[str, Any]:
    materialized = list(values)
    missing_count = sum(1 for value in materialized if value is None)
    if feature.kind == NUMERIC:
        stats = numeric_statistics(materialized, quantiles)
    else:
        stats = category_statistics(materialized, feature.levels)
    stats["total_count"] = len(materialized)
    stats["missing_count"] = missing_count
    return stats


class FeatureAccumulator:
    """Chunked batch accumulator with exact eviction.

    Removal drops the complete batch chunk and subsequent statistics are
    recomputed from the remaining raw values.  This avoids the numerical drift
    that would come from subtracting sums/sums-of-squares.
    """

    def __init__(self, feature: FeatureSpec, quantiles: Sequence[float]) -> None:
        self.feature = feature
        self.quantiles = tuple(quantiles)
        self._chunks: dict[int, tuple[float | str | None, ...]] = {}

    def add_batch(self, batch: Batch) -> None:
        self._chunks[batch.index] = batch.column(self.feature.name).values

    def remove_batch(self, batch_index: int) -> bool:
        return self._chunks.pop(batch_index, None) is not None

    def batch_indices(self) -> list[int]:
        return sorted(self._chunks)

    def sample_count(self) -> int:
        return sum(len(values) for values in self._chunks.values())

    def statistics(self) -> dict[str, Any]:
        values: list[float | str | None] = []
        for index in self.batch_indices():
            values.extend(self._chunks[index])
        return compute_statistics(self.feature, values, self.quantiles)


def _window_descriptor(
    feature_name: str,
    batch_indices: Sequence[int],
    batches: dict[int, Batch],
) -> dict[str, Any]:
    indices = sorted(batch_indices)
    if indices:
        payload = {
            "feature": feature_name,
            "batches": [
                {
                    "batch": index,
                    "column_sha256": batches[index].column(feature_name).content_sha256,
                }
                for index in indices
            ],
        }
        content_hash = sha256_obj(payload)
        sample_count = sum(batches[index].row_count for index in indices)
    else:
        content_hash = sha256_obj({"feature": feature_name, "batches": []})
        sample_count = 0
    return {
        "start_batch": indices[0] if indices else None,
        "end_batch": indices[-1] if indices else None,
        "batch_indices": indices,
        "batch_count": len(indices),
        "sample_count": sample_count,
        "content_sha256": content_hash,
    }


def build_feature_snapshot(
    config: MonitorConfig,
    batches: dict[int, Batch],
    feature: FeatureSpec,
    target_batch: int,
) -> dict[str, Any]:
    reference_indices = list(
        range(max(0, target_batch - config.window_size), target_batch)
    )
    monitor_indices = list(
        range(max(0, target_batch - config.window_size + 1), target_batch + 1)
    )
    full_indices = list(range(0, target_batch + 1))
    removed_indices = [index for index in full_indices if index not in set(monitor_indices)]

    reference_window = _window_descriptor(feature.name, reference_indices, batches)
    monitor_window = _window_descriptor(feature.name, monitor_indices, batches)
    full_window = _window_descriptor(feature.name, full_indices, batches)
    monitor_window["removed_batch_indices"] = removed_indices
    monitor_window["removed_batch_count"] = len(removed_indices)
    monitor_window["removed_sample_count"] = sum(
        batches[index].row_count for index in removed_indices
    )

    current_batch = batches[target_batch]
    current_column = current_batch.column(feature.name)
    reference_accumulator = FeatureAccumulator(feature, config.quantiles)
    monitor_accumulator = FeatureAccumulator(feature, config.quantiles)
    full_accumulator = FeatureAccumulator(feature, config.quantiles)
    current_accumulator = FeatureAccumulator(feature, config.quantiles)
    for index in reference_indices:
        reference_accumulator.add_batch(batches[index])
    for index in monitor_indices:
        monitor_accumulator.add_batch(batches[index])
    for index in full_indices:
        full_accumulator.add_batch(batches[index])
    current_accumulator.add_batch(current_batch)
    statistics = {
        "reference": reference_accumulator.statistics(),
        "current": current_accumulator.statistics(),
        "monitor_window": monitor_accumulator.statistics(),
        "full": full_accumulator.statistics(),
    }

    payload: dict[str, Any] = {
        "artifact_type": "feature_stats",
        "schema_version": config.schema_version,
        "feature": feature.name,
        "kind": feature.kind,
        "enabled": feature.enabled,
        "stat_version": feature.stat_version,
        "feature_definition": feature.definition_payload(),
        "feature_definition_sha256": feature.definition_sha256(),
        "target_batch": target_batch,
        "windows": {
            "reference": reference_window,
            "monitor": monitor_window,
            "full": full_window,
            "current": {
                "batch": target_batch,
                "sample_count": current_batch.row_count,
                "content_sha256": current_column.content_sha256,
            },
        },
        "statistics": statistics,
        "scope_note": (
            "reference=current batch 之前的参考窗口; "
            "monitor_window=包含当前批的滑动窗口; "
            "full=从首批到当前批的全量累计"
        ),
    }
    return payload


def stats_key_components(
    config: MonitorConfig,
    feature: FeatureSpec,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": config.schema_version,
        "artifact_type": "feature_stats",
        "feature": feature.name,
        "target_batch": snapshot["target_batch"],
        "stat_version": feature.stat_version,
        "feature_definition_sha256": feature.definition_sha256(),
        "reference_window_sha256": snapshot["windows"]["reference"]["content_sha256"],
        "monitor_window_sha256": snapshot["windows"]["monitor"]["content_sha256"],
        "full_history_sha256": snapshot["windows"]["full"]["content_sha256"],
        "current_batch_sha256": snapshot["windows"]["current"]["content_sha256"],
        "test_method": (
            config.numeric_test if feature.kind == NUMERIC else "category_proportion_distance"
        ),
        "numeric_threshold": config.numeric_threshold,
        "category_threshold": config.category_threshold,
        "quantile_probabilities": list(config.quantiles),
        "upstream_product_hashes": {
            "feature_column": snapshot["windows"]["current"]["content_sha256"],
            "history": snapshot["windows"]["full"]["content_sha256"],
        },
    }

