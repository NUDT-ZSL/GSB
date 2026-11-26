"""Deterministic v1 <-> v2 configuration migration."""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np

from .hashing import array_digest, content_key
from .models import InstanceMapping, MigrationReport, UnifiedConfig


DEFAULT_V2_CONFIDENCE_THRESHOLD = 0.75


def _replace_checksum(config: UnifiedConfig) -> UnifiedConfig:
    config.checksum = content_key(config.canonical_payload())
    return config


def migrate_v1_to_v2(
    config: UnifiedConfig,
    *,
    default_threshold: float = DEFAULT_V2_CONFIDENCE_THRESHOLD,
    effective_at_ms: int | None = None,
) -> tuple[UnifiedConfig, MigrationReport]:
    if config.source_version != 1:
        raise ValueError("migrate_v1_to_v2 requires a source_version=1 config")
    if not 0.0 <= float(default_threshold) <= 1.0:
        raise ValueError("default_threshold must be in [0, 1]")

    report = MigrationReport(direction="v1_to_v2", policy="complete_explicit_mapping")
    mappings: list[InstanceMapping] = []
    for mapping in config.mappings:
        completed = InstanceMapping(
            instance_id=mapping.instance_id,
            category_id=mapping.category_id,
            confidence_threshold=float(default_threshold),
            effective_at_ms=effective_at_ms or config.effective_at_ms,
            confidence_scope="migrated_implicit_one",
        )
        mappings.append(completed)
        report.add(
            "complete_mapping",
            f"instance:{mapping.instance_id}",
            category_id=mapping.category_id,
            explicit_mapping=True,
            completion_rule=config.mapping_source,
            confidence_threshold=float(default_threshold),
            effective_at_ms=completed.effective_at_ms,
        )

    report.add(
        "complete_default_confidence_threshold",
        "config.default_confidence_threshold",
        old_value=None,
        new_value=float(default_threshold),
        reason="v1 hard instance masks use implicit confidence 1.0",
    )
    migrated = UnifiedConfig(
        source_version=2,
        revision=config.revision,
        categories=config.categories,
        mappings=tuple(mappings),
        default_confidence_threshold=float(default_threshold),
        effective_at_ms=effective_at_ms or config.effective_at_ms,
        relative_effective_seconds=config.relative_effective_seconds,
        time_origin_ms=config.time_origin_ms,
        downgrade_policy=config.downgrade_policy,
        mapping_source=f"migrated_{config.mapping_source}",
        raw_digest=config.raw_digest,
    )
    _replace_checksum(migrated)
    report.add("complete_checksum", "config.checksum", checksum=migrated.checksum)
    return migrated, report


def _flatten_value(values: Iterable[float], rule: float | str) -> float:
    items = [float(v) for v in values]
    if isinstance(rule, (int, float)):
        return float(rule)
    if rule == "min":
        return min(items)
    if rule == "max":
        return max(items)
    if rule in {"mean", "average"}:
        return sum(items) / len(items)
    raise ValueError(f"unsupported flatten rule: {rule!r}")


def migrate_v2_to_v1(
    config: UnifiedConfig,
    *,
    policy: str | None = None,
    flatten_rule: float | str | None = None,
) -> tuple[UnifiedConfig, MigrationReport]:
    if config.source_version != 2:
        raise ValueError("migrate_v2_to_v1 requires a source_version=2 config")
    selected_policy = policy or config.downgrade_policy or "discard"
    if selected_policy not in {"discard", "flatten"}:
        raise ValueError("downgrade policy must be discard or flatten")

    report = MigrationReport(direction="v2_to_v1", policy=selected_policy)
    thresholds = [mapping.confidence_threshold for mapping in config.mappings]
    flattened_threshold: float | None = None
    if selected_policy == "flatten":
        rule = flatten_rule if flatten_rule is not None else "max"
        flattened_threshold = _flatten_value(thresholds, rule)
        report.add(
            "flatten_confidence_thresholds",
            "config.mappings[*].confidence_threshold",
            rule=str(rule),
            flattened_threshold=flattened_threshold,
        )

    mappings: list[InstanceMapping] = []
    for mapping in config.mappings:
        if selected_policy == "discard":
            report.add(
                "discard_instance_confidence",
                f"instance:{mapping.instance_id}",
                confidence_threshold=mapping.confidence_threshold,
                confidence_scope=mapping.confidence_scope,
            )
        else:
            report.add(
                "flatten_instance_confidence",
                f"instance:{mapping.instance_id}",
                old_threshold=mapping.confidence_threshold,
                flattened_threshold=flattened_threshold,
            )
        mappings.append(
            InstanceMapping(
                instance_id=mapping.instance_id,
                category_id=mapping.category_id,
                confidence_threshold=1.0,
                effective_at_ms=mapping.effective_at_ms,
                confidence_scope="implicit_one",
            )
        )

    effective_seconds = config.relative_effective_seconds
    if effective_seconds is None:
        effective_seconds = max(0.0, (config.effective_at_ms - config.time_origin_ms) / 1000.0)
    downgraded = UnifiedConfig(
        source_version=1,
        revision=config.revision,
        categories=config.categories,
        mappings=tuple(mappings),
        default_confidence_threshold=1.0,
        effective_at_ms=config.effective_at_ms,
        relative_effective_seconds=effective_seconds,
        time_origin_ms=config.time_origin_ms,
        downgrade_policy=selected_policy,
        raw_digest=config.raw_digest,
    )
    _replace_checksum(downgraded)
    report.add("rewrite_checksum", "config.checksum", checksum=downgraded.checksum)
    return downgraded, report


def flatten_confidence_mask(
    confidence: np.ndarray,
    accepted: np.ndarray,
    *,
    policy: str = "flatten",
) -> np.ndarray | None:
    """Return the v1-compatible hard confidence mask for flatten mode."""
    if policy == "discard":
        return None
    if policy != "flatten":
        raise ValueError("mask policy must be discard or flatten")
    hard = np.zeros_like(confidence, dtype=np.float64)
    hard[accepted] = 1.0
    return hard


def pixel_contract_checksum(*arrays: np.ndarray, extra: dict[str, Any] | None = None) -> str:
    payload: dict[str, Any] = {
        "arrays": [
            {"dtype": str(np.asarray(arr).dtype), "shape": list(np.asarray(arr).shape), "digest": array_digest(np.asarray(arr))}
            for arr in arrays
        ]
    }
    if extra is not None:
        payload["extra"] = extra
    return content_key(payload)
