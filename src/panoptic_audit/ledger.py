from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import AuditError, ErrorCode
from .groundtruth import ParsedTruth, semantic_key
from .hashing import canonical_checksum


@dataclass
class AdjudicatedLedger:
    height: int
    width: int
    category: np.ndarray
    instance: np.ndarray
    ignore: np.ndarray
    winner: list[list[str]]
    sources: list[list[list[str]]]
    is_conflict: np.ndarray
    confidence: np.ndarray
    pixels: dict[str, dict[str, Any]]
    conflict_positions: list[dict[str, int]]
    format_priority: list[str]
    content_checksum: str

    def pixel_at(self, x: int, y: int) -> dict[str, Any]:
        return self.pixels[f"{x},{y}"]


def build_unified_ledger(
    truths: dict[str, ParsedTruth],
    format_priority: list[str],
) -> AdjudicatedLedger:
    missing = [name for name in format_priority if name not in truths]
    if missing:
        raise AuditError(ErrorCode.FORMAT_CORRUPT, "configured format priority has unknown format", {"missing": missing})

    first = truths[format_priority[0]]
    height, width = first.height, first.width
    priority_rank = {name: index for index, name in enumerate(format_priority)}

    category = np.zeros((height, width), dtype=np.int64)
    instance = np.zeros((height, width), dtype=np.int64)
    ignore = np.zeros((height, width), dtype=bool)
    confidence = np.zeros((height, width), dtype=np.float64)
    winner_grid: list[list[str]] = [[None for _ in range(width)] for _ in range(height)]  # type: ignore[list-item]
    source_grid: list[list[list[str]]] = [[[] for _ in range(width)] for _ in range(height)]
    conflict_mask = np.zeros((height, width), dtype=bool)
    pixels: dict[str, dict[str, Any]] = {}
    conflict_positions: list[dict[str, int]] = []

    for y in range(height):
        for x in range(width):
            votes: list[dict[str, Any]] = []
            totals: dict[str, float] = {}
            key_to_semantic: dict[str, tuple[Any, ...]] = {}
            for fmt in format_priority:
                truth = truths[fmt]
                key_tuple = semantic_key(
                    int(truth.category[y, x]),
                    int(truth.instance[y, x]),
                    bool(truth.ignore[y, x]),
                )
                key = canonical_checksum(key_tuple)
                weight = float(truth.pixel_weight[y, x])
                totals[key] = totals.get(key, 0.0) + weight
                key_to_semantic[key] = key_tuple
                votes.append({
                    "format": fmt,
                    "semantic_key": list(key_tuple),
                    "weight": weight,
                    "key_checksum": key,
                })

            def rank(item: tuple[str, float]) -> tuple[float, int, str]:
                key, total_weight = item
                supporters = [vote["format"] for vote in votes if vote["key_checksum"] == key]
                best_priority = min(priority_rank[fmt] for fmt in supporters)
                return (-total_weight, best_priority, key)

            winner_key, winner_weight = sorted(totals.items(), key=rank)[0]
            winner_semantic = key_to_semantic[winner_key]
            winner_sources = [vote["format"] for vote in votes if vote["key_checksum"] == winner_key]
            unique_keys = set(totals)
            is_conflict = len(unique_keys) > 1
            tied = [key for key, weight in totals.items() if abs(weight - winner_weight) < 1e-12]
            tie_broken_by_priority = is_conflict and len(tied) > 1

            observed_semantics = [vote["semantic_key"] for vote in votes]
            ignore_disagreement = len({item[3] for item in observed_semantics}) > 1
            category_disagreement = len({(item[1], item[3]) for item in observed_semantics}) > 1
            instance_disagreement = len({(item[1], item[2], item[3]) for item in observed_semantics}) > 1
            if ignore_disagreement:
                conflict_severity, resolution_cost = "critical_ignore_boundary", 100
            elif category_disagreement:
                conflict_severity, resolution_cost = "high_category", 10
            elif instance_disagreement:
                conflict_severity, resolution_cost = "medium_instance", 1
            else:
                conflict_severity, resolution_cost = "none", 0

            if winner_semantic[0] == "ignore":
                _, cat, inst, ign = winner_semantic
            else:
                _, cat, inst, ign = winner_semantic
            category[y, x] = int(cat)
            instance[y, x] = int(inst)
            ignore[y, x] = bool(ign)
            total_weight = sum(totals.values())
            confidence[y, x] = winner_weight / total_weight if total_weight else 0.0
            winner_grid[y][x] = winner_key
            source_grid[y][x] = winner_sources
            conflict_mask[y, x] = is_conflict

            record = {
                "x": x,
                "y": y,
                "coordinate": [x, y],
                "winner": {
                    "category": int(cat),
                    "instance": int(inst),
                    "ignore": bool(ign),
                },
                "winner_key_checksum": winner_key,
                "winner_weight": winner_weight,
                "weighted_confidence": float(confidence[y, x]),
                "total_weight": total_weight,
                "sources_hit": winner_sources,
                "is_conflict": is_conflict,
                "tie_broken_by_priority": tie_broken_by_priority,
                "conflict_severity": conflict_severity,
                "resolution_cost": resolution_cost,
                "cost_policy": {"ignore_boundary": 100, "category": 10, "instance": 1},
                "votes": votes,
            }
            record["pixel_checksum"] = canonical_checksum({k: v for k, v in record.items() if k != "pixel_checksum"})
            pixels[f"{x},{y}"] = record
            if is_conflict:
                conflict_positions.append({"x": x, "y": y})

    ledger_body = {
        "height": height,
        "width": width,
        "format_priority": format_priority,
        "pixels": [pixels[f"{x},{y}"] for y in range(height) for x in range(width)],
    }
    content_checksum = canonical_checksum(ledger_body)
    return AdjudicatedLedger(
        height,
        width,
        category,
        instance,
        ignore,
        winner_grid,
        source_grid,
        conflict_mask,
        confidence,
        pixels,
        conflict_positions,
        format_priority,
        content_checksum,
    )


def conflict_policy(
    ledger: AdjudicatedLedger,
    accepted_probe_count: int,
    threshold: float,
) -> dict[str, Any]:
    total_pixels = ledger.height * ledger.width
    conflict_count = len(ledger.conflict_positions)
    plan_coverage_ratio = accepted_probe_count / total_pixels
    conflict_pixel_ratio = conflict_count / total_pixels
    conflict_per_probe = conflict_count / max(1, accepted_probe_count)
    limit = threshold * max(1, accepted_probe_count)
    exceeded = conflict_count > limit or conflict_pixel_ratio > threshold
    result = {
        "threshold": threshold,
        "conflict_pixel_count": conflict_count,
        "total_pixels": total_pixels,
        "accepted_probe_count": accepted_probe_count,
        "plan_coverage_ratio": plan_coverage_ratio,
        "conflict_pixel_ratio": conflict_pixel_ratio,
        "conflict_per_probe_ratio": conflict_per_probe,
        "probe_scaled_limit_pixels": limit,
        "exceeded": exceeded,
        "first_conflict": ledger.conflict_positions[0] if ledger.conflict_positions else None,
    }
    if exceeded:
        raise AuditError(
            ErrorCode.GT_SEMANTIC_CONFLICT,
            "ground truth semantic conflict coverage exceeds threshold",
            result,
        )
    return result
