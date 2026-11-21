"""Cascade error propagation and dominant-source attribution."""

from __future__ import annotations

from collections import Counter
from typing import Any


STAGES = (
    ("weighted_scoring", "quality", "weighted_scoring"),
    ("threshold_comparison", "threshold", "threshold_comparison"),
    ("consistency_smoothing", "consistency", "normalization"),
    ("normalization", "normalization", "normalization"),
    ("quantization", "quantization", "quantization"),
)


def _relative_error(deviation: float, reference: float) -> float:
    if abs(reference) > 1e-15:
        return deviation / abs(reference)
    if deviation == 0.0:
        return 0.0
    return deviation / 1e-15


def _extract_stage_values(result: dict[str, Any], thresholds: dict[str, float]) -> dict[str, dict[str, float]]:
    values: dict[str, dict[str, float]] = {name: {} for name, _, _ in STAGES}
    for pair_id, detail in result["quality"]["samples"].items():
        values["weighted_scoring"][pair_id] = float(detail["score"])
    for pair_id, detail in result["dedup"]["samples"].items():
        values["threshold_comparison"][pair_id] = (
            float(detail["max_similarity"]) - float(thresholds["similarity"])
        )
    for pair_id, detail in result["consistency"]["samples"].items():
        values["consistency_smoothing"][pair_id] = float(detail["consistency_weight"])
    for pair_id, detail in result["sampling"]["samples"].items():
        if detail.get("selected"):
            values["normalization"][pair_id] = float(detail["normalized_sampling_weight"])
            values["quantization"][pair_id] = float(detail["final_weight"])
    return values


def build_error_spectrum(precision_results: dict[str, Any], reference: str = "float64") -> dict[str, Any]:
    reference_result = precision_results[reference]
    thresholds = reference_result["thresholds"]
    reference_values = _extract_stage_values(reference_result, thresholds)
    selected_ids = set(reference_result["sampling"]["selected_ids"])

    tiers: dict[str, Any] = {}
    for tier_name in ("float64", "float32", "fixed"):
        result = precision_results[tier_name]
        tier_values = _extract_stage_values(result, thresholds)
        per_sample: dict[str, Any] = {}
        all_ids = sorted(
            set().union(*(set(values) for values in tier_values.values()), set(reference_values["weighted_scoring"]))
        )
        stage_details: dict[str, list[dict[str, Any]]] = {name: [] for name, _, _ in STAGES}

        for pair_id in all_ids:
            details: dict[str, Any] = {}
            score_candidates: list[tuple[float, int, str, str]] = []
            for order, (stage, _, source) in enumerate(STAGES):
                if pair_id not in tier_values[stage] or pair_id not in reference_values[stage]:
                    continue
                reference_value = reference_values[stage][pair_id]
                tier_value = tier_values[stage][pair_id]
                deviation = abs(tier_value - reference_value)
                relative = _relative_error(deviation, reference_value)
                first_relative = 1.0
                if reference_values["weighted_scoring"].get(pair_id) is not None:
                    first_value = reference_values["weighted_scoring"][pair_id]
                    first_deviation = abs(
                        tier_values["weighted_scoring"].get(pair_id, first_value) - first_value
                    )
                    first_relative = _relative_error(first_deviation, first_value)
                amplification = (relative + 1e-15) / (first_relative + 1e-15)
                stage_detail = {
                    "stage": stage,
                    "source": source,
                    "reference": reference_value,
                    "tier_value": tier_value,
                    "absolute_deviation": deviation,
                    "relative_deviation": relative,
                    "error_interval": [min(reference_value, tier_value), max(reference_value, tier_value)],
                    "amplification_vs_weighted_scoring": amplification,
                }
                details[stage] = stage_detail
                stage_details[stage].append(stage_detail)
                score_candidates.append((relative, order, stage, source))

            if score_candidates:
                dominant = sorted(score_candidates, key=lambda item: (-item[0], item[1]))[0]
                dominant_source = dominant[3] if dominant[0] > 0.0 else "none"
                dominant_stage = dominant[2] if dominant[0] > 0.0 else "none"
            else:
                dominant_source = "none"
                dominant_stage = "none"

            confidence = None
            if pair_id in selected_ids:
                ref_final = reference_values["quantization"].get(pair_id)
                tier_final = tier_values["quantization"].get(pair_id)
                if ref_final is not None and tier_final is not None:
                    radius = abs(tier_final - ref_final)
                    confidence = {
                        "reference_weight": ref_final,
                        "tier_weight": tier_final,
                        "absolute_radius": radius,
                        "interval": [max(0.0, ref_final - radius), ref_final + radius],
                    }
            per_sample[pair_id] = {
                "selected": pair_id in selected_ids,
                "stages": details,
                "dominant_source": dominant_source,
                "dominant_stage": dominant_stage,
                "attribution_basis": "largest_relative_deviation",
                "attribution_details": sorted(
                    details.values(), key=lambda item: (-item["relative_deviation"], item["stage"])
                ),
                "weight_confidence_interval": confidence,
            }

        summaries: dict[str, Any] = {}
        for stage, _, source in STAGES:
            records = stage_details[stage]
            if records:
                abs_values = [record["absolute_deviation"] for record in records]
                rel_values = [record["relative_deviation"] for record in records]
                amp_values = [record["amplification_vs_weighted_scoring"] for record in records]
                intervals = [record["error_interval"] for record in records]
                summaries[stage] = {
                    "source": source,
                    "sample_count": len(records),
                    "mean_absolute_deviation": sum(abs_values) / len(abs_values),
                    "max_absolute_deviation": max(abs_values),
                    "mean_relative_deviation": sum(rel_values) / len(rel_values),
                    "max_relative_deviation": max(rel_values),
                    "max_amplification_ratio": max(amp_values),
                    "error_interval_union": [
                        min(interval[0] for interval in intervals),
                        max(interval[1] for interval in intervals),
                    ],
                }
            else:
                summaries[stage] = {
                    "source": source,
                    "sample_count": 0,
                    "mean_absolute_deviation": 0.0,
                    "max_absolute_deviation": 0.0,
                    "mean_relative_deviation": 0.0,
                    "max_relative_deviation": 0.0,
                    "max_amplification_ratio": 0.0,
                    "error_interval_union": [0.0, 0.0],
                }

        dominant_counts = Counter(
            record["dominant_source"] for record in per_sample.values() if record["selected"]
        )
        final_errors = [
            abs(
                record["weight_confidence_interval"]["tier_weight"]
                - record["weight_confidence_interval"]["reference_weight"]
            )
            for record in per_sample.values()
            if record.get("weight_confidence_interval")
        ]
        amplification_values = [
            details["quantization"]["amplification_vs_weighted_scoring"]
            for details in (record["stages"] for record in per_sample.values())
            if "quantization" in details
        ]
        tiers[tier_name] = {
            "precision": result["precision"],
            "reference_precision": reference,
            "per_sample": per_sample,
            "stage_summaries": summaries,
            "dominant_source_counts": dict(sorted(dominant_counts.items())),
            "selected_weight_error": {
                "mean_absolute": sum(final_errors) / len(final_errors) if final_errors else 0.0,
                "max_absolute": max(final_errors) if final_errors else 0.0,
            },
            "final_amplification_ratio": {
                "mean": sum(amplification_values) / len(amplification_values)
                if amplification_values
                else 0.0,
                "max": max(amplification_values) if amplification_values else 0.0,
            },
        }

    return {
        "reference_precision": reference,
        "stage_order": [name for name, _, _ in STAGES],
        "dominant_source_domain": [
            "weighted_scoring",
            "threshold_comparison",
            "normalization",
            "quantization",
            "none",
        ],
        "tiers": tiers,
    }

