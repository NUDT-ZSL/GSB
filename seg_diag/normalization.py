"""Normalize v1 hard masks and v2 category/instance/confidence masks."""

from __future__ import annotations

from typing import Any

import numpy as np

from .codes import CODES
from .errors import DiagnosticError
from .models import UnifiedConfig
from .numerics import strict_threshold, tolerant_threshold


def _mask_invalid(message: str, **details: Any) -> DiagnosticError:
    return DiagnosticError(CODES.MASK_INVALID, message, details)


def _lookup_categories(config: UnifiedConfig) -> dict[int, int]:
    return {mapping.instance_id: mapping.category_id for mapping in config.mappings}


def normalize_v1(
    instance_mask: np.ndarray, config: UnifiedConfig
) -> dict[str, np.ndarray]:
    instance_mask = np.asarray(instance_mask)
    lookup = _lookup_categories(config)
    category = np.zeros(instance_mask.shape, dtype=np.int64)
    unknown = sorted({int(value) for value in np.unique(instance_mask) if int(value) not in lookup})
    if unknown:
        raise _mask_invalid("v1 mask contains instances absent from configuration", unknown=unknown)
    for instance_id, category_id in lookup.items():
        category[instance_mask == instance_id] = category_id
    confidence = np.where(instance_mask > 0, 1.0, 0.0).astype(np.float64)
    accepted = instance_mask > 0
    return {
        "category": category,
        "instance": instance_mask.astype(np.int64),
        "confidence": confidence,
        "accepted": accepted,
    }


def normalize_v2(
    category_mask: np.ndarray,
    instance_mask: np.ndarray,
    confidence_mask: np.ndarray,
    config: UnifiedConfig,
    *,
    strategy: str = "strict",
    tolerance: float = 1e-12,
    threshold_override: float | dict[int, float] | None = None,
) -> dict[str, np.ndarray]:
    category_mask = np.asarray(category_mask)
    instance_mask = np.asarray(instance_mask)
    confidence = np.asarray(confidence_mask, dtype=np.float64)
    if category_mask.shape != instance_mask.shape or confidence.shape != instance_mask.shape:
        raise _mask_invalid("v2 category, instance and confidence shapes must match")

    lookup = _lookup_categories(config)
    unknown = sorted({int(value) for value in np.unique(instance_mask) if int(value) not in lookup})
    if unknown:
        raise _mask_invalid("v2 mask contains instances absent from metadata", unknown=unknown)

    expected_category = np.zeros(instance_mask.shape, dtype=np.int64)
    thresholds = np.full(instance_mask.shape, float(config.default_confidence_threshold), dtype=np.float64)
    for instance_id, category_id in lookup.items():
        locations = instance_mask == instance_id
        expected_category[locations] = category_id
        if threshold_override is not None:
            if isinstance(threshold_override, dict):
                threshold = float(threshold_override.get(instance_id, config.threshold_for(instance_id)))
            else:
                threshold = float(threshold_override)
        else:
            threshold = float(config.threshold_for(instance_id))
        thresholds[locations] = threshold

    channel_conflict = (category_mask.astype(np.int64) != expected_category) & (instance_mask > 0)

    if strategy == "strict":
        accepted = strict_threshold(confidence, thresholds)
        in_band = np.zeros_like(confidence, dtype=bool)
    elif strategy == "tolerant":
        accepted, in_band = tolerant_threshold(confidence, thresholds, tolerance)
    else:
        raise _mask_invalid("unknown threshold strategy", strategy=strategy)

    accepted = np.asarray(accepted, dtype=bool)
    accepted &= instance_mask > 0
    normalized_category = np.where(accepted, expected_category, 0).astype(np.int64)
    normalized_instance = np.where(accepted, instance_mask.astype(np.int64), 0).astype(np.int64)
    return {
        "category": normalized_category,
        "instance": normalized_instance,
        "raw_category": category_mask.astype(np.int64),
        "raw_instance": instance_mask.astype(np.int64),
        "semantic_category": expected_category,
        "channel_conflict": channel_conflict,
        "confidence": confidence,
        "thresholds": thresholds,
        "accepted": accepted,
        "in_band": in_band,
    }
