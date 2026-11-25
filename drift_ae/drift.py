"""Distribution drift monitoring and attribution-to-module propagation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np


MONITORED_METRICS = (
    "score",
    "mean_residual",
    "max_residual",
    "smooth_residual",
    "boundary_instability",
    "mask_consistency",
)


@dataclass(frozen=True)
class DriftConfig:
    z_threshold: float = 4.0
    minimum_samples: int = 3
    epsilon: float = 1e-3


def build_baseline(records: Sequence[dict], config: DriftConfig | None = None) -> dict:
    grouped: dict[str, dict[str, list[float]]] = {}
    for record in records:
        segment = str(record["segment_id"])
        metrics = record.get("metrics", record)
        target = grouped.setdefault(segment, {name: [] for name in MONITORED_METRICS})
        for name in MONITORED_METRICS:
            if name in metrics:
                target[name].append(float(metrics[name]))

    baseline: dict[str, dict[str, dict[str, float]]] = {}
    for segment, metrics_map in grouped.items():
        baseline[segment] = {}
        for name, values in metrics_map.items():
            if not values:
                continue
            array = np.asarray(values, dtype=np.float64)
            std = float(array.std())
            median = float(np.median(array))
            mad = float(np.median(np.abs(array - median)) * 1.4826)
            baseline[segment][name] = {
                "mean": float(array.mean()),
                "std": std,
                "median": median,
                "mad": mad,
                "count": int(array.size),
            }
    return baseline


def evaluate_drift(
    batch_records: Sequence[dict],
    baseline: dict,
    config: DriftConfig | None = None,
    event_id: str | None = None,
) -> dict:
    cfg = config or DriftConfig()
    grouped: dict[str, list[dict]] = {}
    for item in batch_records:
        grouped.setdefault(str(item["segment_id"]), []).append(item)

    attribution = []
    alerted_segments = set()
    all_indices = []
    all_window_ids = []

    for segment, items in sorted(grouped.items()):
        values_by_metric: dict[str, list[float]] = {name: [] for name in MONITORED_METRICS}
        for item in items:
            all_window_ids.append(str(item.get("window_id", item.get("window_index", "unknown"))))
            if "window_index" in item:
                all_indices.append(int(item["window_index"]))
            metrics = item.get("metrics", item)
            for name in MONITORED_METRICS:
                if name in metrics:
                    values_by_metric[name].append(float(metrics[name]))

        segment_baseline = baseline.get(segment, {})
        for metric, values in values_by_metric.items():
            if len(values) < min(2, cfg.minimum_samples):
                continue
            recent_array = np.asarray(values, dtype=np.float64)
            recent_mean = float(recent_array.mean())
            recent_median = float(np.median(recent_array))
            base = segment_baseline.get(metric)
            if base is None:
                continue
            robust_scale = max(float(base.get("mad", base["std"])), cfg.epsilon)
            z_score = (recent_median - float(base.get("median", base["mean"]))) / robust_scale
            mean_z = (recent_mean - float(base["mean"])) / max(float(base["std"]), cfg.epsilon)
            contribution = abs(float(z_score))
            if contribution >= cfg.z_threshold:
                alerted_segments.add(segment)
            attribution.append(
                {
                    "segment_id": segment,
                    "metric": metric,
                    "baseline_mean": float(base["mean"]),
                    "recent_mean": recent_mean,
                    "baseline_median": float(base.get("median", base["mean"])),
                    "recent_median": recent_median,
                    "z_score": float(z_score),
                    "mean_z_score": float(mean_z),
                    "contribution": contribution,
                    "sample_count": len(values),
                    "alert": contribution >= cfg.z_threshold,
                }
            )

    attribution.sort(key=lambda item: item["contribution"], reverse=True)
    alert = bool(alerted_segments)
    if all_indices:
        time_window = [min(all_indices), max(all_indices)]
        affected_time_windows = [f"w{min(all_indices):04d}:w{max(all_indices):04d}"]
    else:
        time_window = None
        affected_time_windows = []

    module_set = modules_for_attribution(attribution if alert else [])
    deterministic_id = event_id
    if deterministic_id is None and time_window is not None:
        deterministic_id = f"DRIFT-RUNTIME-{time_window[0]:04d}-{time_window[1]:04d}"

    return {
        "alert_id": deterministic_id,
        "alert": alert,
        "affected_segments": sorted(alerted_segments),
        "evaluated_segments": sorted(grouped),
        "time_window": time_window,
        "affected_time_windows": affected_time_windows,
        "window_ids": sorted(set(all_window_ids)),
        "attribution": attribution,
        "module_set": sorted(module_set),
        "threshold_action": "local_recalibration" if alert else "none",
    }


def modules_for_attribution(attribution: Sequence[dict]) -> set[str]:
    modules = {"drift_monitor", "threshold_model", "calibration_scheduler"}
    for item in attribution:
        metric = str(item.get("metric", ""))
        if not item.get("alert", True):
            continue
        if metric == "mask_consistency" or metric == "boundary_instability":
            modules.update({"masking", "feature_extraction"})
        elif metric in {"mean_residual", "max_residual", "smooth_residual"}:
            modules.update({"autoencoder", "feature_extraction"})
        elif metric == "score":
            modules.update({"scoring", "feature_extraction"})
        else:
            modules.add("feature_extraction")
    return modules
