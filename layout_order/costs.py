"""Declarative edge-cost components and deterministic edge ordering."""

from __future__ import annotations

import math
from typing import Any

from .canonical import round_float, sha256_obj
from .errors import LayoutError

DEFAULT_COST_CONFIG: dict[str, Any] = {
    "weights": {
        "type_order_violation": 1.0,
        "column_transition": 4.0,
        "vertical_gap": 6.0,
        "horizontal_misalignment": 1.5,
        "cross_zone": 2.0,
    },
    "type_order": {
        "header": 0,
        "title": 1,
        "section_title": 2,
        "body": 2,
        "figure_caption": 2,
        "footer": 3,
    },
    "type_violation_penalty": 10.0,
    "precision": 8,
}

_WEIGHT_FIELDS = (
    "type_order_violation",
    "column_transition",
    "vertical_gap",
    "horizontal_misalignment",
    "cross_zone",
)

COMPONENT_NAMES = (
    "type_order_violation",
    "column_transition",
    "vertical_gap",
    "horizontal_misalignment",
    "cross_zone",
)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def normalize_cost_config(config: dict[str, Any] | None) -> dict[str, Any]:
    merged = {
        "weights": dict(DEFAULT_COST_CONFIG["weights"]),
        "type_order": dict(DEFAULT_COST_CONFIG["type_order"]),
        "type_violation_penalty": DEFAULT_COST_CONFIG["type_violation_penalty"],
        "precision": DEFAULT_COST_CONFIG["precision"],
    }
    if config:
        if "weights" in config:
            if not isinstance(config["weights"], dict):
                raise LayoutError(
                    "E_CONFIG_INVALID",
                    "weights must be an object",
                    field="weights",
                )
            merged["weights"].update(config["weights"])
        if "type_order" in config:
            if not isinstance(config["type_order"], dict):
                raise LayoutError(
                    "E_CONFIG_INVALID",
                    "type_order must be an object",
                    field="type_order",
                )
            merged["type_order"] = dict(config["type_order"])
        if "type_violation_penalty" in config:
            merged["type_violation_penalty"] = config["type_violation_penalty"]
        if "precision" in config:
            merged["precision"] = config["precision"]
    for field in _WEIGHT_FIELDS:
        if field not in merged["weights"]:
            raise LayoutError(
                "E_CONFIG_INVALID",
                "required edge-cost weight is missing",
                field=f"weights.{field}",
            )
        value = merged["weights"][field]
        if not _is_number(value) or not math.isfinite(float(value)) or float(value) < 0:
            raise LayoutError(
                "E_CONFIG_INVALID",
                "edge-cost weight must be finite and non-negative",
                field=f"weights.{field}",
            )
        merged["weights"][field] = round_float(float(value), 12)
    extra_weights = sorted(set(merged["weights"]) - set(_WEIGHT_FIELDS))
    if extra_weights:
        raise LayoutError(
            "E_CONFIG_INVALID",
            "unknown edge-cost weight",
            field=f"weights.{extra_weights[0]}",
        )
    penalty = merged["type_violation_penalty"]
    if not _is_number(penalty) or not math.isfinite(float(penalty)) or float(penalty) < 0:
        raise LayoutError(
            "E_CONFIG_INVALID",
            "type_violation_penalty must be finite and non-negative",
            field="type_violation_penalty",
        )
    merged["type_violation_penalty"] = round_float(float(penalty), 12)
    if not _is_int(merged["precision"]) or not 1 <= merged["precision"] <= 12:
        raise LayoutError(
            "E_CONFIG_INVALID",
            "cost precision must be an integer in [1, 12]",
            field="precision",
        )
    if not merged["type_order"]:
        raise LayoutError(
            "E_CONFIG_INVALID",
            "type_order must not be empty",
            field="type_order",
        )
    for block_type, rank in sorted(merged["type_order"].items()):
        if not isinstance(block_type, str) or not block_type:
            raise LayoutError(
                "E_CONFIG_INVALID",
                "type_order keys must be non-empty strings",
                field="type_order",
            )
        if not _is_int(rank) or rank < 0:
            raise LayoutError(
                "E_CONFIG_INVALID",
                "type order rank must be a non-negative integer",
                field=f"type_order.{block_type}",
            )
    return merged


def cost_config_fingerprint(config: dict[str, Any]) -> str:
    return sha256_obj(normalize_cost_config(config))


def _horizontal_overlap(source: dict[str, Any], target: dict[str, Any]) -> float:
    overlap = max(
        0.0,
        min(source["x2"], target["x2"]) - max(source["x1"], target["x1"]),
    )
    denom = min(source["width"], target["width"])
    if denom <= 0:
        return 0.0
    return min(1.0, overlap / denom)


def score_edges(
    normalized_page: dict[str, Any],
    edges: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Attach independently recomputable cost components to candidate edges."""

    cfg = normalize_cost_config(config)
    precision = cfg["precision"]
    blocks = {block["block_id"]: block for block in normalized_page["blocks"]}
    for block in blocks.values():
        if block["block_type"] not in cfg["type_order"]:
            raise LayoutError(
                "E_CONFIG_INVALID",
                "type_order does not declare a block type",
                field=f"type_order.{block['block_type']}",
                block_id=block["block_id"],
                page_id=normalized_page.get("page_id"),
            )
    scored: list[dict[str, Any]] = []
    for edge in edges:
        source = blocks[edge["source_block_id"]]
        target = blocks[edge["target_block_id"]]
        if source["block_id"] == target["block_id"]:
            components = {
                "type_order_violation": 0.0,
                "column_transition": 0.0,
                "vertical_gap": 0.0,
                "horizontal_misalignment": 0.0,
                "cross_zone": 0.0,
            }
        else:
            source_rank = cfg["type_order"][source["block_type"]]
            target_rank = cfg["type_order"][target["block_type"]]
            violation_units = max(0, source_rank - target_rank)
            type_component = violation_units * cfg["type_violation_penalty"]
            raw_gap = target["y1"] - source["y2"]
            if target["center_y"] < source["center_y"]:
                # A pure center-distance penalty is too weak for adjacent
                # boxes in reverse order. Add one canvas-height unit so a
                # backward transition can never masquerade as a short gap.
                vertical_metric = 1.0 + (source["center_y"] - target["center_y"])
            else:
                vertical_metric = max(0.0, raw_gap)
            horiz_overlap = _horizontal_overlap(source, target)
            column_delta = target["column_index"] - source["column_index"]
            if column_delta >= 0:
                column_metric = float(column_delta)
            else:
                # Moving to an earlier column is a stronger violation than
                # moving forward by the same number of columns.
                column_metric = 1.0 + abs(float(column_delta))
            components = {
                "type_order_violation": round_float(
                    type_component * cfg["weights"]["type_order_violation"],
                    precision,
                ),
                "column_transition": round_float(
                    column_metric * cfg["weights"]["column_transition"],
                    precision,
                ),
                "vertical_gap": round_float(
                    vertical_metric * cfg["weights"]["vertical_gap"], precision
                ),
                "horizontal_misalignment": round_float(
                    (1.0 - horiz_overlap)
                    * cfg["weights"]["horizontal_misalignment"],
                    precision,
                ),
                "cross_zone": round_float(
                    float(source["zone_id"] != target["zone_id"])
                    * cfg["weights"]["cross_zone"],
                    precision,
                ),
            }
        total = round_float(
            sum(components[name] for name in COMPONENT_NAMES), precision
        )
        scored.append(
            {
                "source_block_id": edge["source_block_id"],
                "target_block_id": edge["target_block_id"],
                "components": components,
                "total_cost": total,
                "candidate_rank": edge["candidate_rank"],
                "features": edge["features"],
            }
        )
    scored.sort(
        key=lambda edge: (
            edge["total_cost"],
            edge["source_block_id"],
            edge["target_block_id"],
        )
    )
    return scored


def recompute_total(edge: dict[str, Any], precision: int = 8) -> float:
    return round_float(
        sum(float(edge["components"][name]) for name in COMPONENT_NAMES),
        precision,
    )


def validate_cost_details(
    normalized_page: dict[str, Any],
    edges: list[dict[str, Any]],
    scored_edges: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
    tolerance: float = 1e-8,
) -> None:
    cfg = normalize_cost_config(config)
    precision = cfg["precision"]
    expected = score_edges(normalized_page, edges, cfg)
    if len(expected) != len(scored_edges):
        raise LayoutError(
            "E_ORDER_INCONSISTENT",
            "scored edge count differs from candidate edge count",
            block_id=min(
                (edge["source_block_id"] for edge in scored_edges), default=None
            ),
            page_id=normalized_page.get("page_id"),
        )
    for got, want in zip(scored_edges, expected):
        key = (got["source_block_id"], got["target_block_id"])
        if key != (want["source_block_id"], want["target_block_id"]):
            raise LayoutError(
                "E_ORDER_INCONSISTENT",
                "scored edge ordering is not reproducible",
                block_id=got["source_block_id"],
                page_id=normalized_page.get("page_id"),
            )
        if abs(got["total_cost"] - want["total_cost"]) > tolerance:
            raise LayoutError(
                "E_ORDER_INCONSISTENT",
                "edge total cost differs from full rescore",
                block_id=got["source_block_id"],
                page_id=normalized_page.get("page_id"),
            )
        if abs(recompute_total(got, precision) - got["total_cost"]) > tolerance:
            raise LayoutError(
                "E_ORDER_INCONSISTENT",
                "edge total cost differs from component sum",
                block_id=got["source_block_id"],
                page_id=normalized_page.get("page_id"),
            )


def costed_edges_fingerprint(scored_edges: list[dict[str, Any]]) -> str:
    return sha256_obj(scored_edges)

