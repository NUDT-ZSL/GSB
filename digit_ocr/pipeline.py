"""Pure deterministic pipeline nodes.

Nothing in this module reads clocks, environment state, or system entropy.
Process-pool workers call the top-level ``compute_*_worker`` functions.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from .canonical import (
    SCHEMA_VERSION,
    seal_payload,
    stable_float,
)
from .errors import (
    E_CONFIDENCE_CONFLICT,
    E_INCONSISTENT,
    E_INVALID_INPUT,
    BusinessError,
)
from .models import (
    CONFIDENCE_TIERS,
    CONTROLLED_LABELS,
    DistanceMetric,
    ImageSample,
    NormalizationConfig,
    Prototype,
    RecognitionConfig,
    ThresholdConfig,
)


FEATURE_NODE = "feature"
DISTANCE_NODE = "distance"
DECISION_NODE = "decision"
METRICS_NODE = "metrics"


def _normalization_identity(config: NormalizationConfig) -> Dict[str, Any]:
    return {
        "version": config.version,
        "checksum": config.checksum,
        "feature_dim": config.feature_dim,
        "projection_bins": config.projection_bins,
    }


def _threshold_identity(config: ThresholdConfig) -> Dict[str, Any]:
    return {"version": config.version, "checksum": config.checksum}


def _metric_identity(metric: DistanceMetric) -> Dict[str, Any]:
    return {
        "name": metric.name,
        "version": metric.version,
        "checksum": metric.checksum,
    }


def feature_key_parts(
    sample: ImageSample, config: NormalizationConfig
) -> Dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "node": FEATURE_NODE,
        "image_id": sample.image_id,
        "seed": sample.seed,
        "image_sha256": sample.image_sha256,
        "normalization_version": config.version,
        "normalization_checksum": config.checksum,
        "projection_bins": config.projection_bins,
        "feature_dim": config.feature_dim,
    }


def distance_key_parts(
    sample: ImageSample,
    config: RecognitionConfig,
    class_id: int,
    feature_content_hash: str,
) -> Dict[str, Any]:
    prototype = config.prototypes.member(class_id)
    return {
        "schema": SCHEMA_VERSION,
        "node": DISTANCE_NODE,
        "image_id": sample.image_id,
        "image_sha256": sample.image_sha256,
        "normalization_version": config.normalization.version,
        "normalization_checksum": config.normalization.checksum,
        "feature_content_hash": feature_content_hash,
        "prototype_class": class_id,
        "prototype_member_version": prototype.version,
        "prototype_member_hash": prototype.checksum,
        "distance_metric_name": config.distance_metric.name,
        "distance_metric_version": config.distance_metric.version,
        "distance_metric_checksum": config.distance_metric.checksum,
    }


def decision_key_parts(
    sample: ImageSample,
    config: RecognitionConfig,
    feature_content_hash: str,
    distance_hashes: Mapping[int, str],
) -> Dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "node": DECISION_NODE,
        "image_id": sample.image_id,
        "image_sha256": sample.image_sha256,
        "normalization_version": config.normalization.version,
        "normalization_checksum": config.normalization.checksum,
        "feature_content_hash": feature_content_hash,
        "prototype_set_version": config.prototypes.version,
        "prototype_set_checksum": config.prototypes.checksum,
        "distance_metric_name": config.distance_metric.name,
        "distance_metric_version": config.distance_metric.version,
        "distance_metric_checksum": config.distance_metric.checksum,
        "distance_content_hashes": {
            str(class_id): distance_hashes[class_id]
            for class_id in sorted(distance_hashes)
        },
        "threshold_version": config.thresholds.version,
        "threshold_checksum": config.thresholds.checksum,
    }


def metrics_key_parts(
    decisions: Sequence[Mapping[str, Any]],
    samples: Sequence[ImageSample],
    metric_spec_version: int = 1,
) -> Dict[str, Any]:
    decision_refs = [
        {
            "image_id": str(decision["image_id"]),
            "content_hash": str(decision["content_hash"]),
        }
        for decision in sorted(decisions, key=lambda item: str(item["image_id"]))
    ]
    sample_refs = [
        {
            "image_id": sample.image_id,
            "label": sample.label,
            "uncertain": sample.uncertain,
            "image_sha256": sample.image_sha256,
        }
        for sample in sorted(samples, key=lambda item: item.image_id)
    ]
    return {
        "schema": SCHEMA_VERSION,
        "node": METRICS_NODE,
        "metric_spec_version": metric_spec_version,
        "decisions": decision_refs,
        "samples": sample_refs,
    }


def normalize_image(
    sample: ImageSample, config: NormalizationConfig
) -> np.ndarray:
    if sample.height != config.canvas_height or sample.width != config.canvas_width:
        raise BusinessError(
            E_INVALID_INPUT,
            "sample canvas does not match normalization canvas",
            image_id=sample.image_id,
            category="feature",
        )
    raw = np.frombuffer(sample.pixels, dtype=np.uint8).reshape(
        sample.height, sample.width
    )
    values = raw.astype(np.float64)
    values = np.clip(values, float(config.input_min), float(config.input_max))
    span = float(config.input_max) - float(config.input_min)
    unit = (values - float(config.input_min)) / span
    normalized = float(config.output_min) + unit * (
        float(config.output_max) - float(config.output_min)
    )
    return normalized


def projection_bin_ranges(size: int, bins: int) -> List[Tuple[int, int]]:
    return [
        ((index * size) // bins, ((index + 1) * size) // bins)
        for index in range(bins)
    ]


def extract_feature_values(
    sample: ImageSample, config: NormalizationConfig
) -> List[float]:
    image = normalize_image(sample, config)
    row_ranges = projection_bin_ranges(image.shape[0], config.projection_bins)
    column_ranges = projection_bin_ranges(image.shape[1], config.projection_bins)
    values: List[float] = []
    for start, stop in row_ranges:
        region = image[start:stop, :]
        values.append(0.0 if region.size == 0 else stable_float(float(np.mean(region))))
    for start, stop in column_ranges:
        region = image[:, start:stop]
        values.append(0.0 if region.size == 0 else stable_float(float(np.mean(region))))
    row_cell_ranges = projection_bin_ranges(
        image.shape[0], config.cell_grid_size
    )
    column_cell_ranges = projection_bin_ranges(
        image.shape[1], config.cell_grid_size
    )
    for row_start, row_stop in row_cell_ranges:
        for column_start, column_stop in column_cell_ranges:
            region = image[row_start:row_stop, column_start:column_stop]
            values.append(
                0.0 if region.size == 0 else stable_float(float(np.mean(region)))
            )
    if len(values) != config.feature_dim:
        raise BusinessError(
            E_INCONSISTENT,
            "feature dimension differs from configuration",
            image_id=sample.image_id,
            category="feature_dimension",
        )
    return values


def build_feature_artifact(
    sample: ImageSample,
    config: NormalizationConfig,
    values: Sequence[float],
) -> Dict[str, Any]:
    row_ranges = projection_bin_ranges(config.canvas_height, config.projection_bins)
    column_ranges = projection_bin_ranges(config.canvas_width, config.projection_bins)
    cell_row_ranges = projection_bin_ranges(
        config.canvas_height, config.cell_grid_size
    )
    cell_column_ranges = projection_bin_ranges(
        config.canvas_width, config.cell_grid_size
    )
    payload = {
        "kind": FEATURE_NODE,
        "schema": SCHEMA_VERSION,
        "image_id": sample.image_id,
        "seed": sample.seed,
        "image_sha256": sample.image_sha256,
        "normalization": _normalization_identity(config),
        "pixel_index_order": config.pixel_index_order,
        "bin_boundary_rule": config.bin_boundary_rule,
        "row_bin_ranges": [[start, stop] for start, stop in row_ranges],
        "column_bin_ranges": [[start, stop] for start, stop in column_ranges],
        "cell_grid_size": config.cell_grid_size,
        "cell_row_ranges": [[start, stop] for start, stop in cell_row_ranges],
        "cell_column_ranges": [[start, stop] for start, stop in cell_column_ranges],
        "values": [stable_float(value) for value in values],
    }
    return seal_payload(payload)


def compute_feature_worker(task: Mapping[str, Any]) -> Dict[str, Any]:
    """Top-level spawn-safe worker entry point."""

    sample = ImageSample(
        image_id=str(task["image_id"]),
        seed=int(task["seed"]),
        label=int(task["label"]),
        uncertain=bool(task["uncertain"]),
        pixels=bytes(task["pixels"]),
        height=int(task["height"]),
        width=int(task["width"]),
    )
    config = NormalizationConfig.from_payload(task["normalization"])
    values = extract_feature_values(sample, config)
    return {
        "cache_key": str(task["cache_key"]),
        "artifact": build_feature_artifact(sample, config, values),
    }


def compute_distance_value(
    feature_values: Sequence[float],
    prototype_values: Sequence[float],
    metric: DistanceMetric,
) -> float:
    if len(feature_values) != len(prototype_values):
        raise BusinessError(
            E_INCONSISTENT,
            "feature and prototype dimensions differ",
            category="distance_dimension",
        )
    left = np.asarray(feature_values, dtype=np.float64)
    right = np.asarray(prototype_values, dtype=np.float64)
    delta = left - right
    if metric.name == "euclidean":
        value = float(np.sqrt(np.dot(delta, delta)))
    elif metric.name == "squared_euclidean":
        value = float(np.dot(delta, delta))
    elif metric.name == "manhattan":
        value = float(np.sum(np.abs(delta)))
    else:
        raise BusinessError(
            E_INVALID_INPUT,
            "unsupported distance metric",
            category="distance_metric",
        )
    return stable_float(value)


def build_distance_artifact(
    sample: ImageSample,
    normalization: NormalizationConfig,
    prototype: Prototype,
    metric: DistanceMetric,
    feature_content_hash: str,
    distance: float,
) -> Dict[str, Any]:
    payload = {
        "kind": DISTANCE_NODE,
        "schema": SCHEMA_VERSION,
        "image_id": sample.image_id,
        "image_sha256": sample.image_sha256,
        "class_id": prototype.class_id,
        "normalization": _normalization_identity(normalization),
        "feature_content_hash": feature_content_hash,
        "prototype_member_version": prototype.version,
        "prototype_member_hash": prototype.checksum,
        "prototype_pixel_sha256": prototype.pixel_sha256,
        "distance_metric": _metric_identity(metric),
        "distance": stable_float(distance),
        "distance_hex": float(distance).hex(),
    }
    return seal_payload(payload)


def compute_distance_worker(task: Mapping[str, Any]) -> Dict[str, Any]:
    """Top-level spawn-safe worker entry point."""

    sample = ImageSample(
        image_id=str(task["image_id"]),
        seed=int(task["seed"]),
        label=int(task["label"]),
        uncertain=bool(task["uncertain"]),
        pixels=bytes(task["pixels"]),
        height=int(task["height"]),
        width=int(task["width"]),
    )
    prototype = Prototype.from_payload(task["prototype"])
    normalization = NormalizationConfig.from_payload(task["normalization"])
    metric = DistanceMetric.from_payload(task["distance_metric"])
    class_id = int(task["class_id"])
    if prototype.class_id != class_id:
        raise BusinessError(
            E_INCONSISTENT,
            "distance task prototype class does not match class_id",
            image_id=sample.image_id,
            category="distance_task",
        )
    distance = compute_distance_value(
        [float(value) for value in task["feature_values"]],
        [float(value) for value in task["prototype_values"]],
        metric,
    )
    return {
        "cache_key": str(task["cache_key"]),
        "artifact": build_distance_artifact(
            sample,
            normalization,
            prototype,
            metric,
            str(task["feature_content_hash"]),
            distance,
        ),
    }


def calculate_prototype_features(
    config: RecognitionConfig,
) -> Dict[int, List[float]]:
    result: Dict[int, List[float]] = {}
    for prototype in config.prototypes.prototypes:
        sample = ImageSample(
            image_id="prototype-{0}".format(prototype.class_id),
            seed=0,
            label=prototype.class_id,
            uncertain=False,
            pixels=bytes(bytearray(prototype.pixels)),
            height=prototype.canvas_height,
            width=prototype.canvas_width,
        )
        result[prototype.class_id] = extract_feature_values(
            sample, config.normalization
        )
    return result


def validate_confidence_combination(
    *,
    rejected: bool,
    confidence: str,
    image_id: Optional[str] = None,
) -> None:
    if confidence not in CONFIDENCE_TIERS:
        raise BusinessError(
            E_CONFIDENCE_CONFLICT,
            "unknown confidence tier: {0}".format(confidence),
            image_id=image_id,
            category="confidence_combination",
            details={"rejected": bool(rejected), "confidence": confidence},
        )
    if rejected and confidence == "high":
        raise BusinessError(
            E_CONFIDENCE_CONFLICT,
            "a rejected sample cannot carry a high-confidence tier",
            image_id=image_id,
            category="confidence_combination",
            details={"rejected": True, "confidence": confidence},
        )
    if (not rejected) and confidence == "none":
        raise BusinessError(
            E_CONFIDENCE_CONFLICT,
            "an accepted sample must carry a confidence tier",
            image_id=image_id,
            category="confidence_combination",
            details={"rejected": False, "confidence": confidence},
        )


def decide_from_distances(
    distances: Mapping[int, float],
    thresholds: ThresholdConfig,
    *,
    image_id: Optional[str] = None,
) -> Dict[str, Any]:
    if set(int(class_id) for class_id in distances) != set(CONTROLLED_LABELS):
        raise BusinessError(
            E_INCONSISTENT,
            "decision requires a distance for every controlled label",
            image_id=image_id,
            category="decision_labels",
        )
    normalized: Dict[int, float] = {}
    for class_id in CONTROLLED_LABELS:
        value = stable_float(float(distances[class_id]))
        if value < 0.0:
            raise BusinessError(
                E_INCONSISTENT,
                "distance must be non-negative",
                image_id=image_id,
                category="distance_value",
            )
        normalized[class_id] = value

    # min() over class-ascending order implements the required tie break.
    predicted_class = min(
        CONTROLLED_LABELS, key=lambda class_id: (normalized[class_id], class_id)
    )
    best_distance = normalized[predicted_class]
    rejected = best_distance > float(thresholds.reject_threshold)
    if rejected:
        confidence = "none"
        rejection_reason: Optional[str] = "best_distance_gt_reject_threshold"
    else:
        rejection_reason = None
        if best_distance <= float(thresholds.high_max):
            confidence = "high"
        elif best_distance <= float(thresholds.medium_max):
            confidence = "medium"
        else:
            confidence = "low"
    validate_confidence_combination(
        rejected=rejected, confidence=confidence, image_id=image_id
    )
    return {
        "predicted_class": predicted_class,
        "rejected": rejected,
        "confidence": confidence,
        "best_distance": best_distance,
        "rejection_reason": rejection_reason,
        "distances": {str(class_id): normalized[class_id] for class_id in CONTROLLED_LABELS},
    }


def build_decision_artifact(
    sample: ImageSample,
    config: RecognitionConfig,
    feature_content_hash: str,
    distance_artifacts: Mapping[int, Mapping[str, Any]],
    decision_cache_key: str,
    feature_cache_key: str,
    distance_cache_keys: Mapping[int, str],
) -> Dict[str, Any]:
    distances = {
        class_id: float(distance_artifacts[class_id]["distance"])
        for class_id in CONTROLLED_LABELS
    }
    result = decide_from_distances(
        distances, config.thresholds, image_id=sample.image_id
    )
    distance_hashes = {
        str(class_id): str(distance_artifacts[class_id]["content_hash"])
        for class_id in CONTROLLED_LABELS
    }
    payload = {
        "kind": DECISION_NODE,
        "schema": SCHEMA_VERSION,
        "image_id": sample.image_id,
        "image_sha256": sample.image_sha256,
        "feature_version": feature_content_hash,
        "feature_cache_key": feature_cache_key,
        "prototype_set_version": config.prototypes.version,
        "prototype_set_checksum": config.prototypes.checksum,
        "prototype_member_hashes": config.prototypes.member_hashes(),
        "threshold_version": config.thresholds.version,
        "threshold_checksum": config.thresholds.checksum,
        "normalization_version": config.normalization.version,
        "normalization_checksum": config.normalization.checksum,
        "distance_metric": _metric_identity(config.distance_metric),
        "distance_content_hashes": distance_hashes,
        "distances": {
            str(class_id): stable_float(distances[class_id])
            for class_id in CONTROLLED_LABELS
        },
        "predicted_class": result["predicted_class"],
        "rejected": result["rejected"],
        "confidence": result["confidence"],
        "best_distance": result["best_distance"],
        "rejection_reason": result["rejection_reason"],
        "node_path": {
            "feature": feature_cache_key,
            "decision": decision_cache_key,
            "distances": {
                str(class_id): str(distance_cache_keys[class_id])
                for class_id in CONTROLLED_LABELS
            },
        },
    }
    validate_confidence_combination(
        rejected=bool(payload["rejected"]),
        confidence=str(payload["confidence"]),
        image_id=sample.image_id,
    )
    return seal_payload(payload)


def compute_metrics_artifact(
    decisions: Sequence[Mapping[str, Any]],
    samples: Sequence[ImageSample],
) -> Dict[str, Any]:
    decision_by_id = {str(item["image_id"]): item for item in decisions}
    sample_by_id = {sample.image_id: sample for sample in samples}
    if set(decision_by_id) != set(sample_by_id):
        raise BusinessError(
            E_INCONSISTENT,
            "metrics decision and sample id sets differ",
            category="metrics_image_ids",
        )
    confusion: Dict[str, Dict[str, int]] = {
        str(label): {str(predicted): 0 for predicted in CONTROLLED_LABELS}
        for label in CONTROLLED_LABELS
    }
    confidence_counts = {tier: 0 for tier in CONFIDENCE_TIERS}
    correct = 0
    rejected = 0
    uncertain_total = 0
    uncertain_correct = 0
    for image_id in sorted(decision_by_id):
        decision = decision_by_id[image_id]
        sample = sample_by_id[image_id]
        predicted = int(decision["predicted_class"])
        if predicted not in CONTROLLED_LABELS:
            raise BusinessError(
                E_INCONSISTENT,
                "prediction is outside the controlled label set",
                image_id=image_id,
                category="label_domain",
            )
        confusion[str(sample.label)][str(predicted)] += 1
        confidence = str(decision["confidence"])
        confidence_counts[confidence] = confidence_counts.get(confidence, 0) + 1
        if bool(decision["rejected"]):
            rejected += 1
        if predicted == sample.label:
            correct += 1
            if sample.uncertain:
                uncertain_correct += 1
        if sample.uncertain:
            uncertain_total += 1
    total = len(decision_by_id)
    payload = {
        "kind": METRICS_NODE,
        "schema": SCHEMA_VERSION,
        "total": total,
        "accepted": total - rejected,
        "rejected": rejected,
        "correct": correct,
        "accuracy": stable_float((correct / total) if total else 0.0),
        "uncertain_total": uncertain_total,
        "uncertain_correct": uncertain_correct,
        "confidence_counts": confidence_counts,
        "confusion": confusion,
        "decision_hashes": [
            {
                "image_id": image_id,
                "content_hash": str(decision_by_id[image_id]["content_hash"]),
            }
            for image_id in sorted(decision_by_id)
        ],
    }
    return seal_payload(payload)
