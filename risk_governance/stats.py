"""Deterministic batch and window statistics."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any, Iterable

import numpy as np

from .canonical import batch_sort_key, sha256_json
from .schema import record_features, record_label, record_score

DEFAULT_QUANTILES = [0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99]


def calibration_quantile_levels(config: dict[str, Any]) -> list[float]:
    levels = set(DEFAULT_QUANTILES)
    for item in config.get("calibrations", []):
        for key in ("anchor_probabilities", "quantile_levels", "target_quantiles"):
            for value in item.get(key, []) or []:
                try:
                    level = float(value)
                except (TypeError, ValueError):
                    continue
                if 0.0 <= level <= 1.0:
                    levels.add(level)
    return sorted(levels)


def group_batches(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        batch_id = str(row.get("batch_id", ""))
        cycle_id = str(row.get("cycle_id", ""))
        groups[(batch_id, cycle_id)].append(row)

    datasets = []
    for (batch_id, cycle_id), records in groups.items():
        ordered = sorted(records, key=lambda row: row.get("_line_no", 0))
        datasets.append(
            {
                "batch_id": batch_id,
                "cycle_id": cycle_id,
                "records": ordered,
                "content_hash": sha256_json(
                    sorted(row["_line_hash"] for row in ordered)
                ),
            }
        )
    return sorted(datasets, key=lambda item: batch_sort_key(item))


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float, np.number)):
        return math.isfinite(float(value))
    return False


def _quantile_entries(values: list[float], levels: Iterable[float]) -> list[dict[str, float]]:
    if not values:
        return []
    array = np.asarray(values, dtype=np.float64)
    return [
        {"probability": float(level), "value": float(np.quantile(array, level))}
        for level in sorted(set(float(level) for level in levels))
    ]


def quantile_map(stats: dict[str, Any] | None) -> dict[float, float | None]:
    if not stats or stats.get("dtype") != "numeric":
        return {}
    result: dict[float, float | None] = {}
    for entry in stats.get("quantiles", []):
        result[float(entry["probability"])] = entry.get("value")
    return result


class StatisticsAccumulator:
    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self.domains = self.config.get("feature_domains", {}) or {}
        self.levels = calibration_quantile_levels(self.config)
        self.numeric_values: dict[str, list[float]] = defaultdict(list)
        self.categorical_values: dict[str, list[str]] = defaultdict(list)
        self.counters: dict[str, Counter[str]] = defaultdict(Counter)
        self.anomaly_counts: Counter[str] = Counter()
        self.anomaly_evidence: list[dict[str, Any]] = []
        self.record_count = 0
        self.line_hashes: list[str] = []

    def add_anomaly(self, code: str, evidence: dict[str, Any], *, limit: int = 50) -> None:
        self.anomaly_counts[code] += 1
        if len(self.anomaly_evidence) < limit:
            self.anomaly_evidence.append({"code": code, **evidence})

    def add_record(self, row: dict[str, Any], *, validate: bool = True) -> None:
        self.record_count += 1
        if "_line_hash" in row:
            self.line_hashes.append(row["_line_hash"])
        features = record_features(row)
        if not isinstance(features, dict):
            if validate:
                self.add_anomaly(
                    "invalid_features_object",
                    {"line_no": row.get("_line_no"), "batch_id": row.get("batch_id")},
                )
            features = {}

        all_names = set(self.domains) | set(features)
        for name in sorted(all_names):
            domain = self.domains.get(name, {}) or {}
            dtype = domain.get("dtype")
            value = features.get(name)
            if name not in features or value is None:
                if validate and domain.get("required", name in self.domains):
                    self.add_anomaly(
                        "missing_feature",
                        {
                            "feature": name,
                            "line_no": row.get("_line_no"),
                            "batch_id": row.get("batch_id"),
                        },
                    )
                continue

            if dtype is None:
                dtype = "numeric" if _is_number(value) else "categorical"

            if dtype == "numeric":
                if not _is_number(value):
                    if validate:
                        self.add_anomaly(
                            "invalid_numeric_feature",
                            {
                                "feature": name,
                                "line_no": row.get("_line_no"),
                                "value": repr(value)[:120],
                            },
                        )
                    continue
                number = float(value)
                self.numeric_values[name].append(number)
                if validate:
                    lower = domain.get("min")
                    upper = domain.get("max")
                    if lower is not None and number < float(lower):
                        self.add_anomaly(
                            "feature_below_domain",
                            {"feature": name, "line_no": row.get("_line_no"), "value": number, "min": lower},
                        )
                    if upper is not None and number > float(upper):
                        self.add_anomaly(
                            "feature_above_domain",
                            {"feature": name, "line_no": row.get("_line_no"), "value": number, "max": upper},
                        )
            else:
                category = str(value)
                self.categorical_values[name].append(category)
                self.counters[name][category] += 1
                allowed = domain.get("allowed")
                if validate and allowed and category not in set(map(str, allowed)):
                    self.add_anomaly(
                        "category_outside_domain",
                        {"feature": name, "line_no": row.get("_line_no"), "value": category},
                    )

        score = record_score(row)
        if _is_number(score):
            self.numeric_values["score"].append(float(score))
            if validate and not 0.0 <= float(score) <= 1.0:
                self.add_anomaly(
                    "score_outside_domain",
                    {"line_no": row.get("_line_no"), "value": float(score)},
                )
        elif validate:
            self.add_anomaly("missing_or_invalid_score", {"line_no": row.get("_line_no")})

        label = record_label(row)
        if label in (0, 1, True, False):
            self.counters["label"][str(int(bool(label)))] += 1
        elif validate:
            self.add_anomaly("invalid_label", {"line_no": row.get("_line_no"), "value": repr(label)[:80]})

    def feature_result(self, name: str) -> dict[str, Any]:
        domain = self.domains.get(name, {}) or {}
        if name in self.categorical_values or domain.get("dtype") == "categorical":
            counts = dict(sorted(self.counters.get(name, Counter()).items()))
            total = sum(counts.values())
            return {
                "dtype": "categorical",
                "count": total,
                "unique_count": len(counts),
                "counts": counts,
                "proportions": {key: (value / total if total else None) for key, value in counts.items()},
                "allowed_values": sorted(map(str, domain.get("allowed", []))),
            }

        values = self.numeric_values.get(name, [])
        if not values:
            return {
                "dtype": "numeric",
                "count": 0,
                "mean": None,
                "variance": None,
                "std": None,
                "min": None,
                "max": None,
                "quantiles": [],
            }
        array = np.asarray(values, dtype=np.float64)
        mean = float(np.mean(array))
        variance = float(np.var(array, ddof=0))
        return {
            "dtype": "numeric",
            "count": int(array.size),
            "mean": mean,
            "variance": variance,
            "std": float(np.sqrt(variance)),
            "min": float(np.min(array)),
            "max": float(np.max(array)),
            "quantiles": _quantile_entries(values, self.levels),
        }

    def result(self, *, include_evidence: bool = True) -> dict[str, Any]:
        feature_names = sorted(set(self.domains) | set(self.numeric_values) | set(self.categorical_values))
        features = {name: self.feature_result(name) for name in feature_names}
        label_counts = dict(sorted(self.counters.get("label", Counter()).items()))
        result = {
            "record_count": self.record_count,
            "sample_count": self.record_count,
            "content_hash": sha256_json(sorted(self.line_hashes)),
            "anomaly_count": sum(self.anomaly_counts.values()),
            "anomaly_counts": dict(sorted(self.anomaly_counts.items())),
            "features": features,
            "score": self.feature_result("score"),
            "label_distribution": label_counts,
        }
        if include_evidence:
            result["anomalies"] = self.anomaly_evidence
        return result


def compute_batch_statistics(dataset: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    accumulator = StatisticsAccumulator(config)
    expected = config.get("expected_sample_count")
    minimum = int(config.get("min_sample_count", 1))
    record_count = len(dataset["records"])
    if expected is not None and record_count != int(expected):
        accumulator.add_anomaly(
            "sample_count_mismatch",
            {"batch_id": dataset["batch_id"], "expected": int(expected), "actual": record_count},
        )
    if record_count < minimum:
        accumulator.add_anomaly(
            "sample_count_below_minimum",
            {"batch_id": dataset["batch_id"], "minimum": minimum, "actual": record_count},
        )
    for record in dataset["records"]:
        accumulator.add_record(record)

    stats = accumulator.result()
    stats.update(
        {
            "batch_id": dataset["batch_id"],
            "cycle_id": dataset["cycle_id"],
            "content_hash": dataset["content_hash"],
        }
    )
    stats["fingerprint"] = sha256_json(
        {
            "batch_id": stats["batch_id"],
            "cycle_id": stats["cycle_id"],
            "record_count": stats["record_count"],
            "content_hash": stats["content_hash"],
            "anomaly_count": stats["anomaly_count"],
            "anomaly_counts": stats["anomaly_counts"],
            "score": stats["score"],
            "features": stats["features"],
            "label_distribution": stats["label_distribution"],
        }
    )
    return stats


def compute_window_statistics(
    datasets: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    window_name: str,
    batch_ids: list[str] | None = None,
    cycle_ids: list[str] | None = None,
) -> dict[str, Any]:
    selected_ids = set(batch_ids) if batch_ids is not None else None
    selected_cycles = set(cycle_ids or [])
    accumulator = StatisticsAccumulator(config)
    selected: list[dict[str, Any]] = []
    for dataset in datasets:
        if selected_ids is not None and dataset["batch_id"] not in selected_ids:
            continue
        if cycle_ids and dataset["cycle_id"] not in selected_cycles:
            continue
        selected.append(dataset)
        for record in dataset["records"]:
            accumulator.add_record(record, validate=False)

    result = accumulator.result(include_evidence=False)
    result["window_name"] = window_name
    result["batch_ids"] = sorted(item["batch_id"] for item in selected)
    result["cycle_ids"] = sorted(set(item["cycle_id"] for item in selected))
    result["batch_count"] = len(selected)
    result["fingerprint"] = sha256_json(result)
    return result


def cross_batch_checks(batch_stats: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    batch_ids = [item["batch_id"] for item in batch_stats]
    checks.append(
        {
            "check": "unique_batch_ids",
            "status": "passed" if len(batch_ids) == len(set(batch_ids)) else "failed",
            "detail": {"batch_count": len(batch_ids)},
        }
    )
    ordered_keys = [
        (natural_safe(item["batch_id"]), natural_safe(item["cycle_id"])) for item in batch_stats
    ]
    expected_order = sorted(ordered_keys)
    checks.append(
        {
            "check": "batch_cycle_order",
            "status": "passed" if ordered_keys == expected_order else "failed",
            "detail": {"order": batch_ids},
        }
    )
    feature_sets = {
        batch["batch_id"]: sorted(name for name, value in batch.get("features", {}).items() if value.get("count", 0) > 0)
        for batch in batch_stats
    }
    baseline = set(next(iter(feature_sets.values()), []))
    schema_drift = {
        batch_id: sorted(set(names) ^ baseline)
        for batch_id, names in feature_sets.items()
        if set(names) != baseline
    }
    checks.append(
        {
            "check": "feature_schema_consistency",
            "status": "passed" if not schema_drift else "failed",
            "detail": {"drift": schema_drift},
        }
    )
    minimum = int(config.get("min_sample_count", 1))
    low_samples = [batch["batch_id"] for batch in batch_stats if batch["record_count"] < minimum]
    checks.append(
        {
            "check": "minimum_sample_count",
            "status": "passed" if not low_samples else "failed",
            "detail": {"minimum": minimum, "low_sample_batches": low_samples},
        }
    )
    return checks


def natural_safe(value: str) -> list[Any]:
    from .canonical import natural_key

    return natural_key(value)
