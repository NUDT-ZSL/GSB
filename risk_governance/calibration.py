"""Segmented quantile calibration."""

from __future__ import annotations

import copy
from typing import Any

from .canonical import sha256_json
from .errors import E_QUANTILE_ORDER, E_ZERO_DENOM, make_error
from .stats import quantile_map

CALIBRATION_ALGORITHM_VERSION = "piecewise-linear-quantile-v1"


def normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(config)
    if not normalized.get("calibrations"):
        items = normalized.get("calibration_items") or normalized.get("items") or []
        default_quantiles = normalized.get("target_quantiles") or [0.1, 0.25, 0.5, 0.75, 0.9]
        calibrated = []
        for item in items:
            if isinstance(item, str):
                calibrated.append(
                    {
                        "item": item,
                        "source_field": item,
                        "anchor_probabilities": default_quantiles,
                        "target_anchor_values": default_quantiles,
                        "target_quantiles": default_quantiles,
                        "threshold_bands": normalized.get("threshold_bands", []),
                        "cost_weights": normalized.get("cost_weights", {}),
                    }
                )
            elif isinstance(item, dict):
                calibrated.append(copy.deepcopy(item))
        normalized["calibrations"] = calibrated

    for calibration in normalized.get("calibrations", []):
        boundaries = calibration.get("segment_boundaries")
        if not calibration.get("segments") and isinstance(boundaries, list) and len(boundaries) >= 2:
            calibration["segments"] = [
                {
                    "segment_index": index,
                    "left_probability": left,
                    "right_probability": right,
                    "boundary": right,
                }
                for index, (left, right) in enumerate(zip(boundaries, boundaries[1:]))
            ]
    return normalized


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def effective_config_for_batch(config: dict[str, Any], batch_id: str) -> dict[str, Any]:
    effective = normalize_config(config)
    override = (config.get("batch_overrides", {}) or {}).get(batch_id)
    if not override:
        return effective

    calibration_overrides = override.get("calibrations", {})
    if isinstance(calibration_overrides, dict):
        items = effective.setdefault("calibrations", [])
        by_item = {item.get("item"): index for index, item in enumerate(items)}
        for item_name, item_override in calibration_overrides.items():
            if item_name in by_item:
                index = by_item[item_name]
                items[index] = deep_merge(items[index], item_override)
            else:
                new_item = {"item": item_name}
                new_item.update(copy.deepcopy(item_override))
                items.append(new_item)
        override = {key: value for key, value in override.items() if key != "calibrations"}

    return deep_merge(effective, override)


def segment_parameter_version(config: dict[str, Any], batch_id: str | None = None) -> str:
    effective = effective_config_for_batch(config, batch_id) if batch_id else config
    material = {
        "declared_version": config.get("segment_parameter_version", "unversioned"),
        "calibrations": effective.get("calibrations", []),
        "boundary_targets": effective.get("boundary_targets", []),
    }
    return f"{config.get('segment_parameter_version', 'seg-v0')}:{sha256_json(material)[:16]}"


def stats_for_item(batch_stats: dict[str, Any], calibration: dict[str, Any]) -> dict[str, Any] | None:
    field_name = calibration.get("source_field") or calibration.get("stats_field") or calibration.get("item")
    item_name = calibration.get("item")
    if field_name == "score" or item_name == "score":
        return batch_stats.get("score")
    features = batch_stats.get("features", {})
    return features.get(field_name) or features.get(item_name)


def _anchor_probabilities(calibration: dict[str, Any]) -> list[float]:
    probabilities = calibration.get("anchor_probabilities")
    if not probabilities:
        probabilities = calibration.get("quantile_levels") or calibration.get("target_quantiles") or []
    return sorted({float(value) for value in probabilities})


def _target_values(calibration: dict[str, Any], probabilities: list[float]) -> list[float]:
    values = calibration.get("target_anchor_values")
    if values is None:
        values = calibration.get("target_values")
    if values is None:
        values = calibration.get("target_quantiles")
    if values is None:
        values = probabilities
    return [float(value) for value in values]


def _configured_segments(calibration: dict[str, Any], probabilities: list[float]) -> list[dict[str, Any]]:
    configured = calibration.get("segments")
    if configured:
        result = []
        for index, segment in enumerate(configured):
            item = dict(segment)
            item.setdefault("segment_index", segment.get("index", index))
            if "left_probability" not in item:
                if "left_quantile" in item:
                    item["left_probability"] = item["left_quantile"]
                elif "left_index" in item:
                    item["left_probability"] = probabilities[int(item["left_index"])]
            if "right_probability" not in item:
                if "right_quantile" in item:
                    item["right_probability"] = item["right_quantile"]
                elif "right_index" in item:
                    item["right_probability"] = probabilities[int(item["right_index"])]
            result.append(item)
        return sorted(result, key=lambda item: int(item["segment_index"]))

    return [
        {
            "segment_index": index,
            "left_probability": left,
            "right_probability": right,
            "boundary": right,
        }
        for index, (left, right) in enumerate(zip(probabilities, probabilities[1:]))
    ]


def calibrate_item(
    batch_stats: dict[str, Any], calibration: dict[str, Any]
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    batch_id = batch_stats.get("batch_id")
    cycle_id = batch_stats.get("cycle_id")
    item_name = calibration.get("item", "unknown")
    source_stats = stats_for_item(batch_stats, calibration)
    qmap = quantile_map(source_stats)
    probabilities = _anchor_probabilities(calibration)
    target_values = _target_values(calibration, probabilities)
    errors: list[dict[str, Any]] = []

    if not probabilities or len(target_values) != len(probabilities):
        errors.append(
            make_error(
                E_QUANTILE_ORDER,
                "calibration anchor probabilities and target values are invalid",
                batch_id=batch_id,
                cycle_id=cycle_id,
                item=item_name,
                segment_index=None,
                reason="invalid_anchor_configuration",
            )
        )
        return None, errors

    anchors: list[dict[str, Any]] = []
    for probability, target_value in zip(probabilities, target_values):
        source_value = qmap.get(float(probability))
        if source_value is None:
            errors.append(
                make_error(
                    E_QUANTILE_ORDER,
                    "missing quantile cannot be treated as zero",
                    batch_id=batch_id,
                    cycle_id=cycle_id,
                    item=item_name,
                    segment_index=None,
                    probability=probability,
                    reason="missing_quantile",
                )
            )
        anchors.append(
            {
                "probability": float(probability),
                "source_value": None if source_value is None else float(source_value),
                "target_value": float(target_value),
            }
        )

    segments = _configured_segments(calibration, probabilities)
    segment_results: list[dict[str, Any]] = []
    anchor_by_probability = {anchor["probability"]: anchor for anchor in anchors}

    for segment in segments:
        segment_index = int(segment["segment_index"])
        left_p = float(segment["left_probability"])
        right_p = float(segment["right_probability"])
        left = anchor_by_probability.get(left_p)
        right = anchor_by_probability.get(right_p)
        if not left or not right or left["source_value"] is None or right["source_value"] is None:
            errors.append(
                make_error(
                    E_QUANTILE_ORDER,
                    "segment quantile is missing and cannot be treated as zero",
                    batch_id=batch_id,
                    cycle_id=cycle_id,
                    item=item_name,
                    segment_index=segment_index,
                    left_probability=left_p,
                    right_probability=right_p,
                    reason="missing_quantile",
                )
            )
            continue

        left_source = float(left["source_value"])
        right_source = float(right["source_value"])
        left_target = float(left["target_value"])
        right_target = float(right["target_value"])

        if left_source > right_source or left_target > right_target:
            errors.append(
                make_error(
                    E_QUANTILE_ORDER,
                    "quantile order inversion across segment boundary",
                    batch_id=batch_id,
                    cycle_id=cycle_id,
                    item=item_name,
                    segment_index=segment_index,
                    left_source=left_source,
                    right_source=right_source,
                    left_target=left_target,
                    right_target=right_target,
                    reason="segment_inversion",
                )
            )
            continue

        denominator = right_source - left_source
        if denominator == 0.0:
            errors.append(
                make_error(
                    E_ZERO_DENOM,
                    "zero denominator in linear quantile interpolation",
                    batch_id=batch_id,
                    cycle_id=cycle_id,
                    item=item_name,
                    segment_index=segment_index,
                    left_source=left_source,
                    right_source=right_source,
                )
            )
            continue

        slope = (right_target - left_target) / denominator
        segment_results.append(
            {
                "segment_index": segment_index,
                "left_probability": left_p,
                "right_probability": right_p,
                "left_source": left_source,
                "right_source": right_source,
                "left_target": left_target,
                "right_target": right_target,
                "denominator": denominator,
                "slope": slope,
                "intercept": left_target - slope * left_source,
            }
        )

    valid_anchors = [anchor for anchor in anchors if anchor["source_value"] is not None]
    for previous, current in zip(valid_anchors, valid_anchors[1:]):
        if previous["source_value"] > current["source_value"]:
            detected_segment = next(
                (
                    item["segment_index"]
                    for item in segment_results
                    if item["right_probability"] == current["probability"]
                ),
                len(segment_results),
            )
            errors.append(
                make_error(
                    E_QUANTILE_ORDER,
                    "cross-segment quantile order inversion",
                    batch_id=batch_id,
                    cycle_id=cycle_id,
                    item=item_name,
                    segment_index=detected_segment,
                    previous_probability=previous["probability"],
                    current_probability=current["probability"],
                    previous_value=previous["source_value"],
                    current_value=current["source_value"],
                    reason="cross_segment_inversion",
                )
            )

    target_quantiles = [float(value) for value in calibration.get("target_quantiles", probabilities)]
    calibrated_quantiles = []
    if not errors and segment_results:
        for probability in target_quantiles:
            source_value = qmap.get(probability)
            if source_value is None:
                errors.append(
                    make_error(
                        E_QUANTILE_ORDER,
                        "target quantile is missing and cannot be treated as zero",
                        batch_id=batch_id,
                        cycle_id=cycle_id,
                        item=item_name,
                        segment_index=None,
                        probability=probability,
                        reason="missing_target_quantile",
                    )
                )
                continue
            calibrated_quantiles.append(
                {
                    "probability": probability,
                    "source_value": float(source_value),
                    "calibrated_value": calibrate_value(float(source_value), segment_results),
                }
            )

    result = {
        "item": item_name,
        "status": "failed" if errors else "calibrated",
        "algorithm_version": CALIBRATION_ALGORITHM_VERSION,
        "probabilities": probabilities,
        "anchors": anchors,
        "segments": segment_results,
        "target_quantiles": calibrated_quantiles,
        "threshold_bands": calibration.get("threshold_bands", []),
        "cost_weights": calibration.get("cost_weights", {}),
    }
    return result, errors


def calibrate_value(value: float, segments: list[dict[str, Any]]) -> float | None:
    if not segments:
        return None
    ordered = sorted(segments, key=lambda item: item["segment_index"])
    chosen = None
    for segment in ordered:
        if segment["left_source"] <= value <= segment["right_source"]:
            chosen = segment
            break
    if chosen is None:
        chosen = ordered[0] if value < ordered[0]["left_source"] else ordered[-1]
    return float(chosen["left_target"] + chosen["slope"] * (value - chosen["left_source"]))


def calibrate_batch(
    batch_stats: dict[str, Any], config: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    effective = effective_config_for_batch(config, batch_stats["batch_id"])
    item_results = []
    errors: list[dict[str, Any]] = []
    for calibration in effective.get("calibrations", []):
        result, item_errors = calibrate_item(batch_stats, calibration)
        if result is not None:
            item_results.append(result)
        errors.extend(item_errors)
    return (
        {
            "batch_id": batch_stats["batch_id"],
            "cycle_id": batch_stats["cycle_id"],
            "status": "failed" if errors else "calibrated",
            "items": item_results,
        },
        errors,
    )
