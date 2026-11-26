"""Pixel-level error propagation diagnostics."""

from __future__ import annotations

from typing import Any

import numpy as np

from .hashing import content_key
from .normalization import normalize_v2
from .numerics import flush_subnormal, safe_ratio


HISTOGRAM_BINS = (0.0, 0.025, 0.05, 0.1, 0.2, 0.4, 1.01)


def deviation_metrics(reference: np.ndarray, candidate: np.ndarray) -> dict[str, Any]:
    ref = flush_subnormal(np.asarray(reference, dtype=np.float64))
    value = flush_subnormal(np.asarray(candidate, dtype=np.float64))
    if ref.shape != value.shape:
        raise ValueError("reference and candidate shapes must match")
    error = value - ref
    absolute = np.abs(error)
    ref_abs_sum = float(np.abs(ref).sum(dtype=np.float64))
    abs_sum = float(absolute.sum(dtype=np.float64))
    flat = np.sort(absolute.reshape(-1))
    percentiles = {
        label: float(flat[min(len(flat) - 1, int(round(q * (len(flat) - 1))))])
        for label, q in [("p50", 0.50), ("p90", 0.90), ("p95", 0.95), ("p99", 0.99)]
    }
    histogram, edges = np.histogram(absolute, bins=np.asarray(HISTOGRAM_BINS, dtype=np.float64))
    return {
        "absolute_deviation_mean": float(absolute.mean(dtype=np.float64)),
        "absolute_deviation_max": float(absolute.max()),
        "absolute_error_sum": abs_sum,
        "relative_deviation_l1": safe_ratio(abs_sum, ref_abs_sum),
        "relative_denominator_abs_sum": ref_abs_sum,
        "rmse": float(np.sqrt(np.mean(error.astype(np.float64) ** 2, dtype=np.float64))),
        "percentiles": percentiles,
        "error_histogram": {
            "edges": [float(x) for x in edges],
            "counts": [int(x) for x in histogram],
        },
        "finite": bool(np.all(np.isfinite(value)) and np.all(np.isfinite(ref))),
    }


def amplification(numerator_abs: float, denominator_abs: float) -> float:
    return safe_ratio(float(numerator_abs), float(denominator_abs), default=0.0)


def _winner_arrays(ledger: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    height = int(ledger["height"])
    width = int(ledger["width"])
    category = np.zeros((height, width), dtype=np.int64)
    instance = np.zeros((height, width), dtype=np.int64)
    for row in ledger["rows"]:
        y = int(row["pixel"]["y"])
        x = int(row["pixel"]["x"])
        category[y, x] = int(row["winner"]["category_id"])
        instance[y, x] = int(row["winner"]["instance_id"])
    return category, instance


def threshold_metrics(
    arrays: dict[str, np.ndarray],
    v2_config: Any,
    *,
    strategy: str,
    tolerance: float,
) -> dict[str, Any]:
    normalized = normalize_v2(
        arrays["v2_category"],
        arrays["v2_instance"],
        arrays["v2_confidence"],
        v2_config,
        strategy=strategy,
        tolerance=tolerance,
    )
    reference_binary = arrays["gt_category"] > 0
    decision = normalized["accepted"]
    mismatch = decision != reference_binary
    false_positive = decision & ~reference_binary
    false_negative = ~decision & reference_binary
    abs_deviation = float(mismatch.mean(dtype=np.float64))
    positive_area = max(1.0, float(reference_binary.sum(dtype=np.float64)))
    relative_deviation = safe_ratio(float(mismatch.sum(dtype=np.float64)), positive_area)
    return {
        "strategy": strategy,
        "tolerance": float(tolerance),
        "accepted_pixel_count": int(decision.sum()),
        "reference_positive_count": int(reference_binary.sum()),
        "mismatch_count": int(mismatch.sum()),
        "false_positive_count": int(false_positive.sum()),
        "false_negative_count": int(false_negative.sum()),
        "tolerance_band_pixel_count": int(normalized["in_band"].sum()),
        "absolute_deviation": abs_deviation,
        "relative_deviation": relative_deviation,
        "strict_or_band_decision_checksum": content_key(decision.astype(np.uint8)),
    }


def semantic_metrics(arrays: dict[str, np.ndarray], ledger: dict[str, Any]) -> dict[str, Any]:
    pred_category, pred_instance = _winner_arrays(ledger)
    gt_category = arrays["gt_category"].astype(np.int64)
    gt_instance = arrays["gt_instance"].astype(np.int64)
    category_mismatch = pred_category != gt_category
    instance_mismatch = pred_instance != gt_instance
    distance = np.zeros(gt_category.shape, dtype=np.float64)
    distance[instance_mismatch & ~category_mismatch] = 0.5
    distance[category_mismatch] = 1.0
    abs_deviation = float(distance.mean(dtype=np.float64))
    relative_deviation = safe_ratio(
        float(distance.sum(dtype=np.float64)),
        max(1.0, float((gt_category > 0).sum(dtype=np.float64))),
    )
    exact_match = ~category_mismatch & ~instance_mismatch
    return {
        "absolute_deviation": abs_deviation,
        "relative_deviation": relative_deviation,
        "category_mismatch_count": int(category_mismatch.sum()),
        "instance_mismatch_count": int(instance_mismatch.sum()),
        "exact_match_count": int(exact_match.sum()),
        "total_count": int(gt_category.size),
        "pixel_accuracy": float(exact_match.mean(dtype=np.float64)),
        "semantic_distance_checksum": content_key(distance),
    }


def build_error_report(
    arrays: dict[str, np.ndarray],
    ledger: dict[str, Any],
    v2_config: Any,
    *,
    tolerant_band: float = 0.02,
) -> dict[str, Any]:
    reference = arrays["reference_score"].astype(np.float64)
    raw_v1 = arrays["v1_score"].astype(np.float64)
    raw_v2 = arrays["v2_score"].astype(np.float64)
    norm_v1 = flush_subnormal(np.clip(raw_v1, 0.0, 1.0))
    norm_v2 = flush_subnormal(np.clip(arrays["v2_confidence"].astype(np.float64), 0.0, 1.0))

    raw_v1_metrics = deviation_metrics(reference, raw_v1)
    raw_v2_metrics = deviation_metrics(reference, raw_v2)
    norm_v1_metrics = deviation_metrics(reference, norm_v1)
    norm_v2_metrics = deviation_metrics(reference, norm_v2)
    normalization_abs = 0.5 * (
        norm_v1_metrics["absolute_deviation_mean"] + norm_v2_metrics["absolute_deviation_mean"]
    )
    raw_abs = 0.5 * (
        raw_v1_metrics["absolute_deviation_mean"] + raw_v2_metrics["absolute_deviation_mean"]
    )

    strict = threshold_metrics(arrays, v2_config, strategy="strict", tolerance=0.0)
    tolerant = threshold_metrics(
        arrays, v2_config, strategy="tolerant", tolerance=tolerant_band
    )
    threshold_abs = strict["absolute_deviation"]
    semantic = semantic_metrics(arrays, ledger)

    contributions = {
        "float64_v1_solver": raw_v1_metrics["absolute_deviation_mean"],
        "float64_v2_solver": raw_v2_metrics["absolute_deviation_mean"],
        "normalization_v1": norm_v1_metrics["absolute_deviation_mean"],
        "normalization_v2": norm_v2_metrics["absolute_deviation_mean"],
        "threshold_strict": threshold_abs,
        "threshold_tolerant": tolerant["absolute_deviation"],
        "semantic_merge": semantic["absolute_deviation"],
    }
    dominant = max(contributions, key=contributions.get)

    return {
        "reference": "truth template soft support field + ground-truth semantic mask",
        "float64_input": {
            "v1": raw_v1_metrics,
            "v2": raw_v2_metrics,
        },
        "normalization": {
            "v1": norm_v1_metrics,
            "v2": norm_v2_metrics,
            "combined_absolute_deviation": normalization_abs,
            "combined_relative_deviation": safe_ratio(
                norm_v1_metrics["relative_deviation_l1"] + norm_v2_metrics["relative_deviation_l1"],
                2.0,
            ),
            "error_amplification_vs_float64": amplification(normalization_abs, raw_abs),
        },
        "threshold_decision": {
            "strict": strict,
            "tolerant": tolerant,
            "error_amplification_vs_normalization": amplification(
                threshold_abs, normalization_abs
            ),
        },
        "semantic_merge": {
            **semantic,
            "error_amplification_vs_threshold": amplification(
                semantic["absolute_deviation"], threshold_abs
            ),
        },
        "dominant_error_source": dominant,
        "source_contributions": contributions,
        "subnormal_protection": {
            "cutoff": float(np.finfo(np.float64).tiny),
            "policy": "flush abs(x) < smallest normal float64 to signed zero",
        },
    }
