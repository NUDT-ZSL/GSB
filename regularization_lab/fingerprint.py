"""Stable, human-auditable cache keys and configuration hashes."""

from __future__ import annotations

import hashlib
from typing import Any

from .atomic import canonical_json
from .config import ExperimentConfig, OptimizerConfig, RegularizerSpec
from .data import Dataset

KEY_SCHEMA = "regularization-lab-key-v1"


def stable_float(value: float) -> str:
    normalized = 0.0 if value == 0.0 else float(value)
    return format(normalized, ".17g")


def _sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def cache_key_payload(scheme: RegularizerSpec, data_fingerprint: str) -> dict[str, Any]:
    return {
        "schema": KEY_SCHEMA,
        "scheme": scheme.name,
        "alpha": stable_float(scheme.alpha),
        "l1_ratio": stable_float(scheme.l1_ratio),
        "data_fingerprint": data_fingerprint,
    }


def cache_key(scheme: RegularizerSpec, data_fingerprint: str) -> str:
    return _sha256(cache_key_payload(scheme, data_fingerprint))


def optimizer_payload(optimizer: OptimizerConfig) -> dict[str, Any]:
    return {
        "learning_rate": stable_float(optimizer.learning_rate),
        "max_iterations": optimizer.max_iterations,
        "tolerance": stable_float(optimizer.tolerance),
        "batch_size": optimizer.batch_size,
        "fit_intercept": optimizer.fit_intercept,
    }


def optimizer_fingerprint(optimizer: OptimizerConfig) -> str:
    return _sha256({"schema": KEY_SCHEMA, "optimizer": optimizer_payload(optimizer)})


def training_payload(
    config: ExperimentConfig,
    dataset: Dataset,
    scheme: RegularizerSpec,
) -> dict[str, Any]:
    return {
        "schema": KEY_SCHEMA,
        "config_schema_version": config.schema_version,
        "cache_key": cache_key(scheme, dataset.data_fingerprint),
        "regularization_type": scheme.kind,
        "optimizer": optimizer_payload(config.optimizer),
        "target_column": dataset.target_column,
        "feature_names": list(dataset.feature_names),
        "source_feature_columns": list(dataset.source_feature_columns),
        "data_fingerprint": dataset.data_fingerprint,
    }


def config_hash(
    config: ExperimentConfig,
    dataset: Dataset,
    scheme: RegularizerSpec,
) -> str:
    return _sha256(training_payload(config, dataset, scheme))


def training_fingerprint(
    config: ExperimentConfig,
    dataset: Dataset,
    scheme: RegularizerSpec,
) -> str:
    return config_hash(config, dataset, scheme)
