"""End-to-end deterministic multi-label classification experiment."""

from __future__ import annotations

import copy
from collections import Counter
from typing import Any, Iterable

import numpy as np

from .config import feature_dimension, validate_config, validate_projection
from .decision import decide_scores, validate_decision_alignment
from .errors import (
    E_INCONSISTENT,
    E_INJECTION,
    E_INJECTION_TARGET,
    E_LABEL_INDEX,
    E_PIXEL_RANGE,
    E_PROJECTION_SHAPE,
    E_RESOURCE_LIMIT,
    E_THRESHOLD,
    E_UNATTRIBUTED,
    LabError,
)
from .features import (
    block_statistics,
    preprocess_image,
    project_features,
    score_features,
)
from .hashing import array_digest, sha256_json
from .injection import Injection, normalize_injections
from .metrics import compute_metrics
from .samples import Sample, generate_samples


STAGES = ("preprocess", "feature", "score", "decision")


def snapshot_metadata(snapshot: dict[str, Any]) -> dict[str, Any]:
    categories = snapshot["categories"]
    return {
        "bundle_checksum": snapshot.get("bundle_checksum"),
        "categories": {
            category: {
                "version": categories[category]["version"],
                "checksum": categories[category]["checksum"],
            }
            for category in ("config", "projection", "annotations")
        },
    }


def _error_result(error: LabError, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    result = {
        "schema_version": "1.0",
        "status": "error",
        "result_code": error.code,
        "business_codes": [error.code],
        "errors": [error.to_dict()],
    }
    if snapshot is not None:
        result["snapshot"] = snapshot_metadata(snapshot)
    return result


def _resource_degradation(
    *,
    category: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    return {
        "stage": "resources",
        "category": category,
        "triggered_by": E_RESOURCE_LIMIT,
        "sample_id": None,
        "label": None,
        "details": details,
    }


def _estimated_bytes_per_sample(config: dict[str, Any], effective_feature_dim: int) -> int:
    image = config["image"]
    image_values = image["height"] * image["width"] * image["channels"]
    # Image, raw blocks, projected features, scores, and a conservative copy.
    values = image_values + 4 * effective_feature_dim + len(config["labels"])
    return int(values * 8)


def apply_resource_limits(
    config: dict[str, Any],
    samples: list[Sample],
    *,
    base_feature_dim: int,
) -> tuple[list[Sample], int, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply declared resource policies and return auditable decisions."""

    limits = config["limits"]
    original_sample_count = len(samples)
    effective_sample_count = original_sample_count
    effective_feature_dim = base_feature_dim
    resource_events: list[dict[str, Any]] = []
    degradations: list[dict[str, Any]] = []

    if effective_sample_count > limits["max_samples"]:
        if limits["sample_limit_policy"] == "error":
            raise LabError(
                E_RESOURCE_LIMIT,
                "sample count exceeds the configured limit",
                stage="resources",
                details={
                    "resource": "sample_count",
                    "requested": effective_sample_count,
                    "limit": limits["max_samples"],
                    "policy": limits["sample_limit_policy"],
                },
            )
        allowed = limits["max_samples"]
        affected = [sample.sample_id for sample in samples[allowed:]]
        resource_events.append(
            {
                "stage": "resources",
                "code": E_RESOURCE_LIMIT,
                "resource": "sample_count",
                "requested": effective_sample_count,
                "allowed": allowed,
                "policy": limits["sample_limit_policy"],
                "affected_sample_ids": affected,
            }
        )
        degradations.append(
            _resource_degradation(
                category="DEGRADE_SAMPLE_TRUNCATION",
                details={
                    "requested_sample_count": effective_sample_count,
                    "effective_sample_count": allowed,
                    "affected_sample_ids": affected,
                },
            )
        )
        effective_sample_count = allowed

    if effective_feature_dim > limits["max_feature_dim"]:
        if limits["feature_limit_policy"] == "error":
            raise LabError(
                E_RESOURCE_LIMIT,
                "feature dimension exceeds the configured limit",
                stage="resources",
                details={
                    "resource": "feature_dim",
                    "requested": effective_feature_dim,
                    "limit": limits["max_feature_dim"],
                    "policy": limits["feature_limit_policy"],
                },
            )
        allowed = limits["max_feature_dim"]
        resource_events.append(
            {
                "stage": "resources",
                "code": E_RESOURCE_LIMIT,
                "resource": "feature_dim",
                "requested": effective_feature_dim,
                "allowed": allowed,
                "policy": limits["feature_limit_policy"],
                "affected_feature_indices": list(range(allowed, effective_feature_dim)),
            }
        )
        degradations.append(
            _resource_degradation(
                category="DEGRADE_FEATURE_TRUNCATION",
                details={
                    "requested_feature_dim": effective_feature_dim,
                    "effective_feature_dim": allowed,
                },
            )
        )
        effective_feature_dim = allowed

    bytes_per_sample = _estimated_bytes_per_sample(config, effective_feature_dim)
    estimated_bytes = effective_sample_count * bytes_per_sample
    memory_limit = limits["max_resident_matrix_bytes"]
    if estimated_bytes > memory_limit:
        if limits["memory_limit_policy"] == "error":
            raise LabError(
                E_RESOURCE_LIMIT,
                "estimated resident matrix memory exceeds the configured limit",
                stage="resources",
                details={
                    "resource": "resident_matrix_bytes",
                    "requested": estimated_bytes,
                    "limit": memory_limit,
                    "bytes_per_sample": bytes_per_sample,
                    "policy": limits["memory_limit_policy"],
                },
            )
        allowed = memory_limit // bytes_per_sample
        if allowed < 1:
            raise LabError(
                E_RESOURCE_LIMIT,
                "memory limit cannot hold even one sample",
                stage="resources",
                details={
                    "resource": "resident_matrix_bytes",
                    "requested": estimated_bytes,
                    "limit": memory_limit,
                    "bytes_per_sample": bytes_per_sample,
                    "policy": limits["memory_limit_policy"],
                },
            )
        allowed = min(effective_sample_count, allowed)
        affected = [sample.sample_id for sample in samples[allowed:]]
        resource_events.append(
            {
                "stage": "resources",
                "code": E_RESOURCE_LIMIT,
                "resource": "resident_matrix_bytes",
                "requested": estimated_bytes,
                "allowed": allowed,
                "policy": limits["memory_limit_policy"],
                "affected_sample_ids": affected,
            }
        )
        degradations.append(
            _resource_degradation(
                category="DEGRADE_MEMORY_SAMPLE_TRUNCATION",
                details={
                    "estimated_bytes": estimated_bytes,
                    "limit": memory_limit,
                    "effective_sample_count": allowed,
                    "affected_sample_ids": affected,
                },
            )
        )
        effective_sample_count = allowed
        estimated_bytes = effective_sample_count * bytes_per_sample

    plan = {
        "source_sample_ids": [sample.sample_id for sample in samples],
        "effective_sample_ids": [
            sample.sample_id for sample in samples[:effective_sample_count]
        ],
        "requested_sample_count": original_sample_count,
        "effective_sample_count": effective_sample_count,
        "requested_feature_dim": base_feature_dim,
        "effective_feature_dim": effective_feature_dim,
        "estimated_bytes_per_sample": bytes_per_sample,
        "estimated_resident_matrix_bytes": estimated_bytes,
        "max_samples": limits["max_samples"],
        "max_feature_dim": limits["max_feature_dim"],
        "max_resident_matrix_bytes": memory_limit,
    }
    return (
        samples[:effective_sample_count],
        effective_feature_dim,
        plan,
        resource_events,
        degradations,
    )


def _slice_projection(
    projection: dict[str, Any],
    effective_feature_dim: int,
) -> dict[str, Any]:
    projection_weights = np.asarray(
        projection["projection_weights"], dtype=np.float64
    )
    score_weights = np.asarray(projection["score_weights"], dtype=np.float64)
    sliced = copy.deepcopy(projection)
    sliced["feature_dim"] = effective_feature_dim
    sliced["projection_weights"] = np.ascontiguousarray(
        projection_weights[:effective_feature_dim, :effective_feature_dim],
        dtype=np.float64,
    ).tolist()
    sliced["score_weights"] = np.ascontiguousarray(
        score_weights[:effective_feature_dim, :], dtype=np.float64
    ).tolist()
    return sliced


def _apply_pixel_injection(
    image: np.ndarray,
    injection: Injection,
    config: dict[str, Any],
) -> np.ndarray:
    params = injection.params
    row = params.get("row", 0)
    col = params.get("col", 0)
    channel = params.get("channel", 0)
    value = params.get("value", 300)
    if (
        not isinstance(row, int)
        or isinstance(row, bool)
        or not isinstance(col, int)
        or isinstance(col, bool)
        or not isinstance(channel, int)
        or isinstance(channel, bool)
        or not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not np.isfinite(value)
    ):
        raise LabError(
            E_INJECTION,
            "pixel_out_of_range injection parameters are invalid",
            stage="injection",
            sample_id=injection.sample_id,
            details=params,
        )
    height, width, channels = image.shape
    if not (0 <= row < height and 0 <= col < width and 0 <= channel < channels):
        raise LabError(
            E_INJECTION,
            "pixel_out_of_range injection index is outside the image",
            stage="injection",
            sample_id=injection.sample_id,
            details=params,
        )
    clip_min = float(config["preprocess"]["clip_min"])
    clip_max = float(config["preprocess"]["clip_max"])
    if clip_min <= float(value) <= clip_max:
        raise LabError(
            E_INJECTION,
            "pixel_out_of_range injection value is inside the valid range",
            stage="injection",
            sample_id=injection.sample_id,
            details={
                "value": value,
                "clip_min": clip_min,
                "clip_max": clip_max,
            },
        )
    updated = image.astype(np.int64, copy=True)
    updated[row, col, channel] = int(value) if float(value).is_integer() else value
    return updated


def _stage_error(
    *,
    stage: str,
    code: str,
    message: str,
    sample_id: str,
    label: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "stage": stage,
        "code": code,
        "message": message,
        "sample_id": sample_id,
        "label": label,
        "details": details or {},
    }


def _degradation(
    *,
    stage: str,
    category: str,
    triggered_by: str,
    sample_id: str | None,
    label: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "stage": stage,
        "category": category,
        "triggered_by": triggered_by,
        "sample_id": sample_id,
        "label": label,
        "details": details or {},
    }


def _unattributed_record(stage: str, sample_id: str, exc: Exception) -> dict[str, Any]:
    return {
        "stage": stage,
        "code": E_UNATTRIBUTED,
        "sample_id": sample_id,
        "error_type": type(exc).__name__,
        "message": str(exc),
    }


def _apply_injection_validation(
    injections: list[Injection],
    *,
    original_sample_ids: set[str],
    effective_sample_ids: set[str],
    labels: list[str],
) -> None:
    for injection in injections:
        if injection.sample_id not in original_sample_ids:
            raise LabError(
                E_INJECTION_TARGET,
                "injection targets an unknown sample",
                stage=injection.stage,
                sample_id=injection.sample_id,
                details={"injection_id": injection.injection_id},
            )
        if injection.sample_id not in effective_sample_ids:
            raise LabError(
                E_INJECTION_TARGET,
                "injection target was removed by a resource-limit policy",
                stage=injection.stage,
                sample_id=injection.sample_id,
                details={"injection_id": injection.injection_id},
            )
        if injection.stage == "decision" and injection.label not in labels:
            raise LabError(
                E_INJECTION_TARGET,
                "decision injection targets a label outside the controlled set",
                stage=injection.stage,
                sample_id=injection.sample_id,
                label=injection.label,
                details={"injection_id": injection.injection_id},
            )


def classify_experiment(
    snapshot: dict[str, Any],
    injections: Iterable[Injection | dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run one deterministic classification experiment and return its report."""

    try:
        return _classify_experiment(snapshot, injections)
    except LabError as error:
        return _error_result(error, snapshot)
    except Exception as exc:  # Defensive boundary: never silently swallow.
        error = LabError(
            E_UNATTRIBUTED,
            "unattributed exception escaped the experiment boundary",
            stage="experiment",
            details={"error_type": type(exc).__name__, "message": str(exc)},
        )
        return _error_result(error, snapshot)


def _classify_experiment(
    snapshot: dict[str, Any],
    injections: Iterable[Injection | dict[str, Any]] | None,
) -> dict[str, Any]:
    categories = snapshot["categories"]
    config = validate_config(categories["config"]["payload"])
    projection = categories["projection"]["payload"]
    annotations = categories["annotations"]["payload"]
    validate_projection(projection, config)

    if injections is None:
        injection_specs = []
    else:
        provided = list(injections)
        if all(isinstance(item, Injection) for item in provided):
            injection_specs = provided  # type: ignore[assignment]
        else:
            injection_specs = normalize_injections(provided)  # type: ignore[arg-type]

    labels = list(config["labels"])
    original_samples = generate_samples(config, annotations)
    original_ids = {sample.sample_id for sample in original_samples}
    base_feature_dim = feature_dimension(config)
    (
        samples,
        effective_feature_dim,
        resource_plan,
        resource_events,
        degradations,
    ) = apply_resource_limits(config, original_samples, base_feature_dim=base_feature_dim)
    effective_ids = {sample.sample_id for sample in samples}
    _apply_injection_validation(
        injection_specs,
        original_sample_ids=original_ids,
        effective_sample_ids=effective_ids,
        labels=labels,
    )

    effective_projection = _slice_projection(projection, effective_feature_dim)
    projection_weights = np.asarray(
        effective_projection["score_weights"], dtype=np.float64
    )
    projection_bias = np.asarray(effective_projection["bias"], dtype=np.float64)
    fallback_threshold = float(config["decision"]["fallback_threshold"])
    configured_thresholds = {
        label: float(config["decision"]["thresholds"][label]) for label in labels
    }

    injection_records: list[dict[str, Any]] = []
    stage_errors: list[dict[str, Any]] = []
    unattributed: list[dict[str, Any]] = []
    stage_audit: dict[str, list[str]] = {stage: [] for stage in STAGES}
    sample_records: list[dict[str, Any]] = []

    injections_by_sample_stage: dict[tuple[str, str], list[Injection]] = {}
    for injection in injection_specs:
        injections_by_sample_stage.setdefault(
            (injection.sample_id, injection.stage), []
        ).append(injection)

    for sample in samples:
        sample_id = sample.sample_id
        sample_degradation_categories: list[str] = []

        # Stage 1: preprocessing. Pixel injections mutate a defensive copy only.
        image_input: np.ndarray = sample.image.copy()
        preprocess_injections = injections_by_sample_stage.get((sample_id, "preprocess"), [])
        for injection in preprocess_injections:
            injection_records.append(injection.to_record())
            image_input = _apply_pixel_injection(image_input, injection, config)
        try:
            normalized, preprocess_issues = preprocess_image(image_input, config)
            preprocess_path = "validated"
        except LabError:
            raise
        except Exception as exc:
            unattributed.append(_unattributed_record("preprocess", sample_id, exc))
            degradations.append(
                _degradation(
                    stage="preprocess",
                    category="DEGRADE_UNATTRIBUTED_FALLBACK",
                    triggered_by=E_UNATTRIBUTED,
                    sample_id=sample_id,
                    details={"fallback": "zero_image"},
                )
            )
            sample_degradation_categories.extend(
                ["DEGRADE_UNATTRIBUTED_FALLBACK"]
            )
            normalized = np.zeros(
                (
                    config["image"]["height"],
                    config["image"]["width"],
                    config["image"]["channels"],
                ),
                dtype=np.float64,
            )
            preprocess_issues = []
            preprocess_path = "unattributed_fallback"
        if preprocess_issues:
            for issue in preprocess_issues:
                stage_errors.append(
                    _stage_error(
                        stage="preprocess",
                        code=issue["code"],
                        message=issue["message"],
                        sample_id=sample_id,
                        details={
                            "invalid_count": issue["invalid_count"],
                            "positions_preview": issue["positions_preview"],
                        },
                    )
                )
            degradation = _degradation(
                stage="preprocess",
                category="DEGRADE_CLAMP_PIXELS",
                triggered_by=E_PIXEL_RANGE,
                sample_id=sample_id,
                details={
                    "clip_min": config["preprocess"]["clip_min"],
                    "clip_max": config["preprocess"]["clip_max"],
                    "invalid_count": preprocess_issues[0]["invalid_count"],
                },
            )
            degradations.append(degradation)
            sample_degradation_categories.append(degradation["category"])
            preprocess_path = "clamped"
        preprocess_sha256 = array_digest(normalized)
        stage_audit["preprocess"].append(sample_id)

        # Stage 2: block statistics and fixed-weight projection.
        feature_injections = injections_by_sample_stage.get((sample_id, "feature"), [])
        for injection in feature_injections:
            injection_records.append(injection.to_record())
        raw_features: np.ndarray | None = None
        try:
            raw_features = block_statistics(normalized, config)
            if feature_injections:
                injection = feature_injections[0]
                injected_shape = injection.params.get("shape")
                if injected_shape is not None:
                    if (
                        not isinstance(injected_shape, list)
                        or len(injected_shape) != 2
                        or any(
                            not isinstance(value, int) or isinstance(value, bool)
                            for value in injected_shape
                        )
                        or tuple(injected_shape)
                        == (effective_feature_dim, len(labels))
                    ):
                        raise LabError(
                            E_INJECTION,
                            "projection_shape_mismatch injection does not describe a mismatched shape",
                            stage="injection",
                            sample_id=sample_id,
                            details={"shape": injected_shape},
                        )
                stage_errors.append(
                    _stage_error(
                        stage="feature",
                        code=E_PROJECTION_SHAPE,
                        message="projection weights were injected with an invalid shape",
                        sample_id=sample_id,
                        details={
                            "injection_id": injection.injection_id,
                            "expected_shape": [
                                effective_feature_dim,
                                len(labels),
                            ],
                            "injected_shape": injected_shape
                            if injected_shape is not None
                            else [effective_feature_dim + 1, len(labels)],
                        },
                    )
                )
                features = np.ascontiguousarray(
                    raw_features[:effective_feature_dim], dtype=np.float64
                )
                feature_path = "raw_block_stats"
                degradation = _degradation(
                    stage="feature",
                    category="DEGRADE_RAW_BLOCK_STATS",
                    triggered_by=E_PROJECTION_SHAPE,
                    sample_id=sample_id,
                    details={"fallback": "block_statistics_without_projection"},
                )
                degradations.append(degradation)
                sample_degradation_categories.append(degradation["category"])
            else:
                projected, _weights, _bias = project_features(
                    raw_features[:effective_feature_dim],
                    effective_projection,
                    feature_dim=effective_feature_dim,
                    labels=labels,
                )
                features = projected
                feature_path = "projected"
        except LabError as error:
            if error.code != E_PROJECTION_SHAPE or raw_features is None:
                raise
            stage_errors.append(
                _stage_error(
                    stage="feature",
                    code=error.code,
                    message=error.message,
                    sample_id=sample_id,
                    details=error.details,
                )
            )
            features = np.ascontiguousarray(
                raw_features[:effective_feature_dim], dtype=np.float64
            )
            feature_path = "raw_block_stats"
            degradation = _degradation(
                stage="feature",
                category="DEGRADE_RAW_BLOCK_STATS",
                triggered_by=error.code,
                sample_id=sample_id,
                details={"fallback": "block_statistics_without_projection"},
            )
            degradations.append(degradation)
            sample_degradation_categories.append(degradation["category"])
        except Exception as exc:
            unattributed.append(_unattributed_record("feature", sample_id, exc))
            features = np.zeros(effective_feature_dim, dtype=np.float64)
            feature_path = "unattributed_fallback"
            degradation = _degradation(
                stage="feature",
                category="DEGRADE_UNATTRIBUTED_FALLBACK",
                triggered_by=E_UNATTRIBUTED,
                sample_id=sample_id,
                details={"fallback": "zero_feature_vector"},
            )
            degradations.append(degradation)
            sample_degradation_categories.append(degradation["category"])
        feature_sha256 = array_digest(features)
        stage_audit["feature"].append(sample_id)

        # Stage 3: independent per-label linear score.
        score_injections = injections_by_sample_stage.get((sample_id, "score"), [])
        for injection in score_injections:
            injection_records.append(injection.to_record())
        try:
            if score_injections:
                injection = score_injections[0]
                requested_index = injection.params.get("label_index")
                if requested_index is not None and (
                    not isinstance(requested_index, int)
                    or isinstance(requested_index, bool)
                    or 0 <= requested_index < len(labels)
                ):
                    raise LabError(
                        E_INJECTION,
                        "label_index_oob injection index is inside the controlled range",
                        stage="injection",
                        sample_id=sample_id,
                        label=injection.label,
                        details={
                            "label_index": requested_index,
                            "label_count": len(labels),
                        },
                    )
                injected_label = injection.label or str(
                    injection.params.get("label", "__out_of_range__")
                )
                stage_errors.append(
                    _stage_error(
                        stage="score",
                        code=E_LABEL_INDEX,
                        message="score label index is outside the controlled label range",
                        sample_id=sample_id,
                        label=injected_label,
                        details={
                            "injection_id": injection.injection_id,
                            "label_index": injection.params.get("label_index"),
                            "label_count": len(labels),
                        },
                    )
                )
                scores = np.ascontiguousarray(projection_bias.copy(), dtype=np.float64)
                score_path = "bias_only"
                degradation = _degradation(
                    stage="score",
                    category="DEGRADE_BIAS_ONLY_SCORE",
                    triggered_by=E_LABEL_INDEX,
                    sample_id=sample_id,
                    label=injected_label,
                    details={"fallback": "fixed_bias_vector"},
                )
                degradations.append(degradation)
                sample_degradation_categories.append(degradation["category"])
            else:
                scores = score_features(
                    features,
                    projection_weights,
                    projection_bias,
                    labels,
                )
                score_path = "linear"
        except LabError as error:
            if error.code not in (E_PROJECTION_SHAPE, E_LABEL_INDEX):
                raise
            stage_errors.append(
                _stage_error(
                    stage="score",
                    code=error.code,
                    message=error.message,
                    sample_id=sample_id,
                    label=error.label,
                    details=error.details,
                )
            )
            scores = np.ascontiguousarray(projection_bias.copy(), dtype=np.float64)
            score_path = "bias_only"
            degradation = _degradation(
                stage="score",
                category="DEGRADE_BIAS_ONLY_SCORE",
                triggered_by=error.code,
                sample_id=sample_id,
                label=error.label,
                details={"fallback": "fixed_bias_vector"},
            )
            degradations.append(degradation)
            sample_degradation_categories.append(degradation["category"])
        except Exception as exc:
            unattributed.append(_unattributed_record("score", sample_id, exc))
            scores = np.zeros(len(labels), dtype=np.float64)
            score_path = "unattributed_fallback"
            degradation = _degradation(
                stage="score",
                category="DEGRADE_UNATTRIBUTED_FALLBACK",
                triggered_by=E_UNATTRIBUTED,
                sample_id=sample_id,
                details={"fallback": "zero_score_vector"},
            )
            degradations.append(degradation)
            sample_degradation_categories.append(degradation["category"])
        score_sha256 = array_digest(scores)
        stage_audit["score"].append(sample_id)

        # Stage 4: independent per-label threshold decisions.
        applied_thresholds = [configured_thresholds[label] for label in labels]
        decision_injections = injections_by_sample_stage.get((sample_id, "decision"), [])
        for injection in decision_injections:
            injection_records.append(injection.to_record())
            if injection.label not in labels:
                raise LabError(
                    E_INJECTION_TARGET,
                    "decision injection label is outside the controlled set",
                    stage="decision",
                    sample_id=sample_id,
                    label=injection.label,
                )
            requested_threshold = injection.params.get("threshold", 0.0)
            if (
                not isinstance(requested_threshold, (int, float))
                or isinstance(requested_threshold, bool)
                or not np.isfinite(requested_threshold)
                or requested_threshold > 0
            ):
                raise LabError(
                    E_INJECTION,
                    "nonpositive_threshold injection parameter is invalid",
                    stage="decision",
                    sample_id=sample_id,
                    label=injection.label,
                    details={"threshold": requested_threshold},
                )
            label_index = labels.index(injection.label)
            applied_thresholds[label_index] = fallback_threshold
            stage_errors.append(
                _stage_error(
                    stage="decision",
                    code=E_THRESHOLD,
                    message="threshold is not positive",
                    sample_id=sample_id,
                    label=injection.label,
                    details={
                        "injection_id": injection.injection_id,
                        "requested_threshold": requested_threshold,
                        "fallback_threshold": fallback_threshold,
                    },
                )
            )
            degradation = _degradation(
                stage="decision",
                category="DEGRADE_DEFAULT_THRESHOLD",
                triggered_by=E_THRESHOLD,
                sample_id=sample_id,
                label=injection.label,
                details={"fallback_threshold": fallback_threshold},
            )
            degradations.append(degradation)
            sample_degradation_categories.append(degradation["category"])
        try:
            decisions = decide_scores(
                scores.tolist(),
                applied_thresholds,
                labels,
                sample_id=sample_id,
            )
            predicted_labels = [
                label
                for label, decision in zip(labels, decisions, strict=True)
                if decision
            ]
            validate_decision_alignment(
                sample_id=sample_id,
                labels=labels,
                scores=scores.tolist(),
                thresholds=applied_thresholds,
                decisions=decisions,
                predicted_labels=predicted_labels,
            )
            decision_path = "fallback_thresholds" if decision_injections else "configured"
        except LabError:
            raise
        except Exception as exc:
            unattributed.append(_unattributed_record("decision", sample_id, exc))
            decisions = [False for _label in labels]
            predicted_labels = []
            decision_path = "unattributed_fallback"
            degradation = _degradation(
                stage="decision",
                category="DEGRADE_UNATTRIBUTED_FALLBACK",
                triggered_by=E_UNATTRIBUTED,
                sample_id=sample_id,
                details={"fallback": "all_negative"},
            )
            degradations.append(degradation)
            sample_degradation_categories.append(degradation["category"])
        decision_sha256 = array_digest(np.asarray(decisions, dtype=np.uint8), dtype="u1")
        stage_audit["decision"].append(sample_id)

        distinct_categories = sorted(set(sample_degradation_categories))
        sample_records.append(
            {
                "sample_id": sample_id,
                "source_image_sha256": array_digest(sample.image, dtype="u1"),
                "truth_labels": list(sample.truth_labels),
                "uncertain_labels": list(sample.uncertain_labels),
                "stages": {
                    "preprocess": {
                        "path": preprocess_path,
                        "shape": list(normalized.shape),
                        "sha256": preprocess_sha256,
                    },
                    "feature": {
                        "path": feature_path,
                        "dimension": int(features.shape[0]),
                        "sha256": feature_sha256,
                    },
                    "score": {
                        "path": score_path,
                        "labels": list(labels),
                        "values": scores.tolist(),
                        "sha256": score_sha256,
                    },
                    "decision": {
                        "path": decision_path,
                        "thresholds": [float(value) for value in applied_thresholds],
                        "decisions": [bool(value) for value in decisions],
                        "predicted_labels": list(predicted_labels),
                        "sha256": decision_sha256,
                    },
                },
                "path": {
                    "mode": "degraded" if distinct_categories else "normal",
                    "degradation_categories": distinct_categories,
                },
            }
        )

    if not sample_records:
        raise LabError(
            E_RESOURCE_LIMIT,
            "resource policies produced an empty experiment",
            stage="resources",
        )

    metric_input = [
        {
            "sample_id": record["sample_id"],
            "truth_labels": record["truth_labels"],
            "uncertain_labels": record["uncertain_labels"],
            "predicted_labels": record["stages"]["decision"]["predicted_labels"],
        }
        for record in sample_records
    ]
    metrics = compute_metrics(
        metric_input,
        labels,
        uncertain_strategy=config["decision"]["uncertain_strategy"],
        zero_division=config["metrics"].get("zero_division"),
    )

    degradation_categories = sorted(
        {record["category"] for record in degradations}
    )
    triggered_by = sorted({record["triggered_by"] for record in degradations})
    metrics_path = {
        "mode": "degraded" if degradations else "normal",
        "degradation_categories": degradation_categories,
        "triggered_by": triggered_by,
        "source": "degraded_predictions" if degradations else "direct_predictions",
    }
    business_codes = sorted(
        set(resource_events_code["code"] for resource_events_code in resource_events)
        | set(record["code"] for record in stage_errors)
        | set(record["code"] for record in injection_records)
        | ({E_UNATTRIBUTED} if unattributed else set())
    )
    if unattributed:
        status = "error"
        result_code = E_UNATTRIBUTED
    elif degradations:
        status = "degraded"
        result_code = "OK_DEGRADED"
    else:
        status = "ok"
        result_code = "OK"

    stage_manifests = {
        stage: {
            "sample_count": len(stage_audit[stage]),
            "sample_sha256": sha256_json(
                [
                    {
                        "sample_id": record["sample_id"],
                        "sha256": record["stages"][stage]["sha256"],
                    }
                    for record in sample_records
                ]
            ),
        }
        for stage in STAGES
    }
    code_counts = Counter(
        [record["code"] for record in stage_errors]
        + [record["code"] for record in injection_records]
        + [record["code"] for record in resource_events]
        + ([E_UNATTRIBUTED] * len(unattributed))
    )

    return {
        "schema_version": "1.0",
        "status": status,
        "result_code": result_code,
        "business_codes": business_codes,
        "business_code_counts": [
            {"code": code, "count": count} for code, count in sorted(code_counts.items())
        ],
        "config": config,
        "snapshot": snapshot_metadata(snapshot),
        "resource_limits": resource_plan,
        "resource_events": resource_events,
        "injection_records": injection_records,
        "stage_errors": stage_errors,
        "degradations": degradations,
        "unattributed": unattributed,
        "stage_audit": stage_audit,
        "stage_manifests": stage_manifests,
        "samples": sample_records,
        "metrics": metrics,
        "metrics_path": metrics_path,
    }
