"""Deterministic local batch generation and validation."""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

import numpy as np

from .canonical import sha256_obj, stable_float
from .errors import E_BATCH_INVALID, AppError
from .models import (
    CATEGORY,
    NUMERIC,
    Batch,
    DriftEvent,
    FeatureColumn,
    FeatureSpec,
    MonitorConfig,
)


def _stable_seed(seed: int, batch_index: int, feature_name: str, phase: str) -> int:
    material = f"{seed}|{batch_index}|{feature_name}|{phase}".encode("utf-8")
    digest = hashlib.sha256(material).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def _rng(seed: int, batch_index: int, feature_name: str, phase: str) -> np.random.Generator:
    return np.random.Generator(np.random.PCG64(_stable_seed(seed, batch_index, feature_name, phase)))


def _events_for(
    config: MonitorConfig, batch_index: int, feature_name: str
) -> tuple[DriftEvent, ...]:
    return tuple(
        sorted(
            event
            for event in config.drift_events
            if event.batch == batch_index and event.feature == feature_name
        )
    )


def _category_probabilities(
    feature: FeatureSpec, events: Iterable[DriftEvent]
) -> tuple[float, ...]:
    probabilities = feature.probabilities
    for event in events:
        if event.kind == "category_shift":
            if len(event.probabilities) != len(feature.levels):
                raise AppError(
                    E_BATCH_INVALID,
                    "类别漂移占比维度与特征 levels 不一致",
                    details={"feature": feature.name},
                )
            probabilities = event.probabilities
    return tuple(probabilities)


def _generate_values(
    config: MonitorConfig,
    batch_index: int,
    feature: FeatureSpec,
) -> tuple[list[float | str | None], list[bool]]:
    events = _events_for(config, batch_index, feature.name)
    rng = _rng(config.seed, batch_index, feature.name, "values")
    size = config.batch_size

    if feature.kind == NUMERIC:
        values = rng.normal(feature.mean, feature.std, size=size).astype(float)
        for event in events:
            if event.kind == "mean_shift":
                values += event.magnitude * feature.std
            elif event.kind == "variance_scale":
                values = feature.mean + (values - feature.mean) * event.magnitude
        missing_rate = 0.0
        for event in events:
            if event.kind == "missing_rate":
                missing_rate = max(missing_rate, event.missing_rate)
        mask_rng = _rng(config.seed, batch_index, feature.name, "missing")
        missing_mask = mask_rng.random(size) < missing_rate
        output: list[float | str | None] = [
            None if missing_mask[row] else stable_float(values[row])
            for row in range(size)
        ]
        return output, [bool(value) for value in missing_mask]

    probabilities = _category_probabilities(feature, events)
    uniforms = rng.random(size)
    cumulative = np.cumsum(np.asarray(probabilities, dtype=float))
    selected = np.searchsorted(cumulative, uniforms, side="right")
    selected = np.minimum(selected, len(feature.levels) - 1)
    missing_rate = 0.0
    for event in events:
        if event.kind == "missing_rate":
            missing_rate = max(missing_rate, event.missing_rate)
    mask_rng = _rng(config.seed, batch_index, feature.name, "missing")
    missing_mask = mask_rng.random(size) < missing_rate
    output = [
        None if missing_mask[row] else feature.levels[int(selected[row])]
        for row in range(size)
    ]
    return output, [bool(value) for value in missing_mask]


def _validate_values(
    feature: FeatureSpec, values: tuple[float | str | None, ...], batch_index: int
) -> None:
    if feature.kind == NUMERIC:
        for value in values:
            if value is None:
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise AppError(
                    E_BATCH_INVALID,
                    "数值列包含非法值",
                    details={"feature": feature.name, "batch": batch_index},
                )
            if not np.isfinite(float(value)):
                raise AppError(
                    E_BATCH_INVALID,
                    "数值列包含非有限值",
                    details={"feature": feature.name, "batch": batch_index},
                )
        return
    allowed = set(feature.levels)
    for value in values:
        if value is not None and value not in allowed:
            raise AppError(
                E_BATCH_INVALID,
                "类别列包含未定义水平",
                details={"feature": feature.name, "batch": batch_index, "value": value},
            )


def feature_column_sha256(
    name: str, kind: str, definition_sha256: str, values: tuple[float | str | None, ...]
) -> str:
    return sha256_obj(
        {
            "name": name,
            "kind": kind,
            "definition_sha256": definition_sha256,
            "values": list(values),
        }
    )


def generate_batch(config: MonitorConfig, batch_index: int) -> Batch:
    if batch_index < 0 or batch_index >= config.n_batches:
        raise AppError(
            E_BATCH_INVALID,
            "批次索引超出范围",
            details={"batch": batch_index, "n_batches": config.n_batches},
        )

    columns: list[FeatureColumn] = []
    for feature in config.features:
        values_list, missing_mask = _generate_values(config, batch_index, feature)
        values = tuple(values_list)
        if len(values) != config.batch_size:
            raise AppError(
                E_BATCH_INVALID,
                "批次样本数不正确",
                details={"batch": batch_index, "feature": feature.name},
            )
        _validate_values(feature, values, batch_index)
        definition_hash = feature.definition_sha256()
        columns.append(
            FeatureColumn(
                name=feature.name,
                kind=feature.kind,
                definition_sha256=definition_hash,
                values=values,
                missing_count=sum(missing_mask),
                content_sha256=feature_column_sha256(
                    feature.name, feature.kind, definition_hash, values
                ),
            )
        )

    fingerprint_payload: dict[str, Any] = {
        "batch_index": batch_index,
        "seed": config.seed,
        "row_count": config.batch_size,
        "columns": [
            {
                "name": column.name,
                "kind": column.kind,
                "definition_sha256": column.definition_sha256,
                "values": list(column.values),
            }
            for column in sorted(columns, key=lambda item: item.name)
        ],
        "drift_events": [
            event.to_state()
            for event in sorted(
                (
                    event
                    for event in config.drift_events
                    if event.batch == batch_index
                ),
                key=lambda item: (item.feature, item.kind),
            )
        ],
    }
    fingerprint = sha256_obj(fingerprint_payload)
    audit = {
        "batch_index": batch_index,
        "row_count": config.batch_size,
        "feature_count": len(columns),
        "numeric_feature_count": sum(1 for feature in config.features if feature.kind == NUMERIC),
        "category_feature_count": sum(
            1 for feature in config.features if feature.kind == CATEGORY
        ),
        "missing_by_feature": {
            column.name: column.missing_count
            for column in sorted(columns, key=lambda item: item.name)
        },
        "status": "validated",
    }
    return Batch(
        index=batch_index,
        seed=config.seed,
        row_count=config.batch_size,
        columns=tuple(columns),
        fingerprint=fingerprint,
        audit=audit,
    )


def generate_stream(config: MonitorConfig) -> dict[int, Batch]:
    return {index: generate_batch(config, index) for index in range(config.n_batches)}

