"""Deterministic instance scoring and scene metric aggregation."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

import numpy as np

from .errors import BusinessError
from .io_utils import round_float
from .models import Instance, Peak


def validate_scoring(
    scoring: dict[str, Any], *, scene_id: str | None = None
) -> None:
    threshold = scoring.get("match_threshold")
    if not isinstance(threshold, (int, float)) or threshold <= 0:
        raise BusinessError(
            "E_MATCH_THRESHOLD",
            "match_threshold must be positive",
            field="match_threshold",
            scene_id=scene_id,
            details={"value": threshold},
        )
    weight_sum = 0.0
    for field_name in (
        "intensity_weight",
        "coverage_weight",
        "consistency_weight",
    ):
        value = scoring.get(field_name)
        if not isinstance(value, (int, float)) or value < 0:
            raise BusinessError(
                "E_CONFIG_INVALID",
                f"{field_name} must be non-negative",
                field=field_name,
                scene_id=scene_id,
            )
        weight_sum += float(value)
    if weight_sum <= 0:
        raise BusinessError(
            "E_CONFIG_INVALID",
            "at least one scoring weight must be positive",
            field="scoring",
            scene_id=scene_id,
        )
    decimals = scoring.get("score_decimals", 9)
    if not isinstance(decimals, int) or not 0 <= decimals <= 12:
        raise BusinessError(
            "E_CONFIG_INVALID",
            "score_decimals must be an integer between 0 and 12",
            field="score_decimals",
            scene_id=scene_id,
        )


def _peak_distance(left: Peak, right: Peak) -> float:
    return float(
        np.hypot(left.refined_x - right.refined_x, left.refined_y - right.refined_y)
    )


def score_instances(
    instances: Iterable[Instance],
    metadata: dict[str, Any],
    scoring: dict[str, Any],
    *,
    scene_id: str | None = None,
) -> list[Instance]:
    validate_scoring(scoring, scene_id=scene_id)
    channel_weights = {
        int(channel["channel"]): float(channel.get("weight", 1.0))
        for channel in metadata["channels"]
    }
    total_weight = sum(channel_weights.values())
    if total_weight <= 0:
        raise BusinessError(
            "E_CONFIG_INVALID",
            "sum of keypoint channel weights must be positive",
            field="channels",
            scene_id=scene_id,
        )
    # Build the same rooted parent relation as grouping, independent of edge order.
    adjacency: dict[int, list[int]] = {
        channel: [] for channel in channel_weights
    }
    for left, right in metadata["edges"]:
        adjacency[int(left)].append(int(right))
        adjacency[int(right)].append(int(left))
    parents = {}
    queue = [0]
    seen = {0}
    while queue:
        parent = queue.pop(0)
        for child in sorted(adjacency[parent]):
            if child in seen:
                continue
            seen.add(child)
            parents[child] = parent
            queue.append(child)

    threshold = float(scoring["match_threshold"])
    intensity_weight = float(scoring.get("intensity_weight", 0.0))
    coverage_weight = float(scoring.get("coverage_weight", 0.0))
    consistency_weight = float(scoring.get("consistency_weight", 0.0))
    normalization_weight = intensity_weight + coverage_weight + consistency_weight
    link_distance = float(metadata["link_distance"])
    score_decimals = int(scoring.get("score_decimals", 9))
    scored: list[Instance] = []

    for instance in instances:
        channel_to_peak = {peak.channel: peak for peak in instance.peaks}
        present_weight = sum(channel_weights[channel] for channel in channel_to_peak)
        intensity = (
            sum(
                channel_weights[channel] * peak.intensity
                for channel, peak in channel_to_peak.items()
            )
            / present_weight
            if present_weight > 0
            else 0.0
        )
        coverage = present_weight / total_weight
        qualities: list[float] = []
        for peak in sorted(instance.peaks, key=lambda item: item.channel):
            parent_channel = parents.get(peak.channel)
            if parent_channel is None or parent_channel not in channel_to_peak:
                continue
            distance = _peak_distance(peak, channel_to_peak[parent_channel])
            qualities.append(max(0.0, 1.0 - distance / link_distance))
        consistency = (
            sum(qualities) / len(qualities) if qualities else 0.0
        )
        raw_score = (
            intensity_weight * intensity
            + coverage_weight * coverage
            + consistency_weight * consistency
        ) / normalization_weight
        instance.score = round_float(raw_score, score_decimals)
        instance.matched = bool(instance.score >= threshold)
        scored.append(instance)
    scored.sort(key=lambda instance: instance.instance_id)
    return scored


def scene_metrics(
    instances: Iterable[Instance],
    *,
    score_decimals: int = 9,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    items = list(instances)
    scores = [instance.score for instance in items]
    metrics: dict[str, Any] = {
        "instance_count": len(items),
        "matched_count": sum(1 for instance in items if instance.matched),
        "keypoint_count": sum(len(instance.peaks) for instance in items),
        "mean_score": round_float(
            math.fsum(scores) / len(scores), score_decimals
        )
        if scores
        else 0.0,
        "min_score": round_float(min(scores), score_decimals) if scores else 0.0,
        "max_score": round_float(max(scores), score_decimals) if scores else 0.0,
    }
    if extra:
        metrics.update(extra)
    return metrics

