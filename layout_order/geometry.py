"""Deterministic page normalization and candidate successor-edge construction."""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Iterable

from .canonical import canonical_bytes, canonical_json, round_float, sha256_obj
from .errors import LayoutError

DEFAULT_GEOMETRY_CONFIG: dict[str, Any] = {
    "column_grid": 3,
    "adjacent_gap_ratio": 0.02,
    "max_candidates_per_block": 16,
    "precision": 8,
}

DEFAULT_LIMITS: dict[str, Any] = {
    "max_blocks_per_page": 14,
    "max_edge_comparisons": 200_000,
    "max_resident_memory_mb": 64,
    "limit_policy": "error",
}

_REQUIRED_BLOCK_FIELDS = (
    "block_id",
    "x1",
    "y1",
    "x2",
    "y2",
    "block_type",
    "text_len",
)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def normalize_geometry_config(config: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULT_GEOMETRY_CONFIG)
    if config:
        merged.update(config)
    for field in ("column_grid", "max_candidates_per_block"):
        if not _is_int(merged.get(field)) or merged[field] < 1:
            raise LayoutError(
                "E_CONFIG_INVALID",
                "geometry configuration must be a positive integer",
                field=f"geometry.{field}",
            )
    if not _is_int(merged.get("precision")) or not 1 <= merged["precision"] <= 12:
        raise LayoutError(
            "E_CONFIG_INVALID",
            "geometry precision must be an integer in [1, 12]",
            field="geometry.precision",
        )
    gap = merged.get("adjacent_gap_ratio")
    if not _is_number(gap) or not math.isfinite(float(gap)) or float(gap) < 0:
        raise LayoutError(
            "E_CONFIG_INVALID",
            "adjacent_gap_ratio must be a finite non-negative number",
            field="geometry.adjacent_gap_ratio",
        )
    merged["adjacent_gap_ratio"] = round_float(float(gap), merged["precision"])
    return merged


def normalize_limits(limits: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULT_LIMITS)
    if limits:
        merged.update(limits)
    for field in ("max_blocks_per_page", "max_edge_comparisons"):
        value = merged.get(field)
        if not _is_int(value) or value < 1:
            raise LayoutError(
                "E_CONFIG_INVALID",
                "resource limit must be a positive integer",
                field=f"limits.{field}",
            )
    memory_limit = merged.get("max_resident_memory_mb")
    if (
        not _is_number(memory_limit)
        or not math.isfinite(float(memory_limit))
        or float(memory_limit) <= 0
    ):
        raise LayoutError(
            "E_CONFIG_INVALID",
            "memory limit must be a finite positive number of MB",
            field="limits.max_resident_memory_mb",
        )
    merged["max_resident_memory_mb"] = float(memory_limit)
    if merged.get("limit_policy") not in {"error", "degrade"}:
        raise LayoutError(
            "E_CONFIG_INVALID",
            "limit_policy must be 'error' or 'degrade'",
            field="limits.limit_policy",
        )
    return merged


def _validate_canvas(page: dict[str, Any]) -> tuple[int, int]:
    canvas = page.get("canvas_shape")
    if (
        not isinstance(canvas, (list, tuple))
        or len(canvas) != 2
        or not all(_is_int(v) and v > 0 for v in canvas)
    ):
        raise LayoutError(
            "E_FORMAT_INVALID",
            "canvas_shape must be [height, width] positive integers",
            field="canvas_shape",
            page_id=str(page.get("page_id")),
            line=page.get("_line"),
        )
    return int(canvas[0]), int(canvas[1])


def _validate_blocks(page: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = page.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise LayoutError(
            "E_FORMAT_INVALID",
            "blocks must be a non-empty list",
            field="blocks",
            page_id=str(page.get("page_id")),
            line=page.get("_line"),
        )
    result: list[dict[str, Any]] = []
    seen: set[int] = set()
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise LayoutError(
                "E_FORMAT_INVALID",
                "block must be an object",
                field=f"blocks[{index}]",
                page_id=str(page.get("page_id")),
                line=page.get("_line"),
            )
        for field in _REQUIRED_BLOCK_FIELDS:
            if field not in block:
                raise LayoutError(
                    "E_FORMAT_INVALID",
                    "required block field is missing",
                    field=f"blocks[{index}].{field}",
                    page_id=str(page.get("page_id")),
                    line=page.get("_line"),
                )
        block_id = block["block_id"]
        if not _is_int(block_id) or block_id < 0:
            raise LayoutError(
                "E_FORMAT_INVALID",
                "block_id must be a non-negative integer",
                field=f"blocks[{index}].block_id",
                page_id=str(page.get("page_id")),
                line=page.get("_line"),
            )
        if block_id in seen:
            raise LayoutError(
                "E_FORMAT_INVALID",
                "duplicate block_id",
                field=f"blocks[{index}].block_id",
                block_id=block_id,
                page_id=str(page.get("page_id")),
                line=page.get("_line"),
            )
        seen.add(block_id)
        for field in ("x1", "y1", "x2", "y2"):
            value = block[field]
            if not _is_number(value) or not math.isfinite(float(value)):
                raise LayoutError(
                    "E_FORMAT_INVALID",
                    "rectangle coordinate must be finite",
                    field=f"blocks[{index}].{field}",
                    block_id=block_id,
                    page_id=str(page.get("page_id")),
                    line=page.get("_line"),
                )
        if float(block["x2"]) <= float(block["x1"]) or float(block["y2"]) <= float(
            block["y1"]
        ):
            raise LayoutError(
                "E_FORMAT_INVALID",
                "rectangle must have positive width and height",
                field=f"blocks[{index}]",
                block_id=block_id,
                page_id=str(page.get("page_id")),
                line=page.get("_line"),
            )
        if not isinstance(block["block_type"], str) or not block["block_type"]:
            raise LayoutError(
                "E_FORMAT_INVALID",
                "block_type must be a non-empty string",
                field=f"blocks[{index}].block_type",
                block_id=block_id,
                page_id=str(page.get("page_id")),
                line=page.get("_line"),
            )
        if not _is_int(block["text_len"]) or block["text_len"] < 0:
            raise LayoutError(
                "E_FORMAT_INVALID",
                "text_len must be a non-negative integer",
                field=f"blocks[{index}].text_len",
                block_id=block_id,
                page_id=str(page.get("page_id")),
                line=page.get("_line"),
            )
        result.append(dict(block))
    return result


def _column_band(block: dict[str, Any], center_y: float) -> str:
    block_type = block["block_type"]
    if block_type == "header":
        return "header"
    if block_type == "footer":
        return "footer"
    if center_y < 1.0 / 3.0:
        return "top"
    if center_y < 2.0 / 3.0:
        return "middle"
    return "bottom"


def normalize_page(
    page: dict[str, Any], config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Normalize a page and return a canonical, JSON-serializable artifact."""

    cfg = normalize_geometry_config(config)
    precision = cfg["precision"]
    canvas_h, canvas_w = _validate_canvas(page)
    blocks = _validate_blocks(page)
    page_id = str(page.get("page_id", ""))
    true_entries = [
        block for block in blocks if block.get("reading_order_true") is not None
    ]
    if true_entries:
        if len(true_entries) != len(blocks):
            missing = next(
                (
                    block["block_id"]
                    for block in sorted(blocks, key=lambda item: item["block_id"])
                    if block.get("reading_order_true") is None
                ),
                None,
            )
            raise LayoutError(
                "E_FORMAT_INVALID",
                "reading_order_true must be present on every block or none",
                field="reading_order_true",
                block_id=missing,
                page_id=page_id,
            )
        for block in true_entries:
            if not _is_int(block["reading_order_true"]) or block["reading_order_true"] < 0:
                raise LayoutError(
                    "E_FORMAT_INVALID",
                    "reading_order_true must be a non-negative integer",
                    field="reading_order_true",
                    block_id=block["block_id"],
                    page_id=page_id,
                )
        orders = sorted(block["reading_order_true"] for block in true_entries)
        if orders != list(range(len(blocks))):
            counts: Counter[int] = Counter(orders)
            bad_ids = sorted(
                block["block_id"]
                for block in blocks
                if block["reading_order_true"] >= len(blocks)
                or counts[block["reading_order_true"]] > 1
            )
            first_bad = bad_ids[0] if bad_ids else min(
                block["block_id"] for block in blocks
            )
            raise LayoutError(
                "E_FORMAT_INVALID",
                "reading_order_true values must be a contiguous permutation",
                field="reading_order_true",
                block_id=first_bad,
                page_id=page_id,
                details={"orders": orders},
            )
    normalized: list[dict[str, Any]] = []
    for block in sorted(blocks, key=lambda item: item["block_id"]):
        x1 = round_float(float(block["x1"]) / canvas_w, precision)
        y1 = round_float(float(block["y1"]) / canvas_h, precision)
        x2 = round_float(float(block["x2"]) / canvas_w, precision)
        y2 = round_float(float(block["y2"]) / canvas_h, precision)
        center_x = round_float((x1 + x2) / 2.0, precision)
        center_y = round_float((y1 + y2) / 2.0, precision)
        width = round_float(x2 - x1, precision)
        height = round_float(y2 - y1, precision)
        if block.get("column_index") is None:
            column_index = min(cfg["column_grid"] - 1, int(center_x * cfg["column_grid"]))
        else:
            if not _is_int(block["column_index"]) or block["column_index"] < 0:
                raise LayoutError(
                    "E_FORMAT_INVALID",
                    "column_index must be a non-negative integer",
                    field=f"block[{block['block_id']}].column_index",
                    block_id=block["block_id"],
                    page_id=page_id,
                )
            column_index = int(block["column_index"])
        zone_id = block.get("zone_id")
        if zone_id is None:
            zone_id = f"{column_index}:{_column_band(block, center_y)}"
        elif not isinstance(zone_id, str) or not zone_id:
            raise LayoutError(
                "E_FORMAT_INVALID",
                "zone_id must be a non-empty string",
                field=f"block[{block['block_id']}].zone_id",
                block_id=block["block_id"],
                page_id=page_id,
            )
        nesting_path = block.get("nesting_path", str(column_index))
        if not isinstance(nesting_path, str) or not nesting_path:
            raise LayoutError(
                "E_FORMAT_INVALID",
                "nesting_path must be a non-empty string",
                field=f"block[{block['block_id']}].nesting_path",
                block_id=block["block_id"],
                page_id=page_id,
            )
        normalized.append(
            {
                "block_id": block["block_id"],
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
                "width": width,
                "height": height,
                "center_x": center_x,
                "center_y": center_y,
                "block_type": block["block_type"],
                "text_len": block["text_len"],
                "column_index": column_index,
                "zone_id": zone_id,
                "nesting_path": nesting_path,
            }
        )
    return {
        "page_id": page_id,
        "canvas_shape": [canvas_h, canvas_w],
        "blocks": normalized,
        "normalization_config": cfg,
    }


def normalize_input_fingerprint(page: dict[str, Any]) -> str:
    """Fingerprint inputs that can change normalization output.

    ``reading_order_true`` is intentionally excluded because it is an evaluation
    annotation, not an input to geometric normalization.
    """

    clean = {
        "page_id": page.get("page_id"),
        "canvas_shape": page.get("canvas_shape"),
        "blocks": [],
    }
    for block in page.get("blocks", []):
        clean["blocks"].append(
            {key: value for key, value in block.items() if key != "reading_order_true"}
        )
    return sha256_obj(clean)


def geometry_fingerprint(normalized_page: dict[str, Any]) -> str:
    blocks = []
    for block in normalized_page["blocks"]:
        blocks.append(
            {
                "block_id": block["block_id"],
                "x1": block["x1"],
                "y1": block["y1"],
                "x2": block["x2"],
                "y2": block["y2"],
                "column_index": block["column_index"],
                "zone_id": block["zone_id"],
                "nesting_path": block["nesting_path"],
            }
        )
    return sha256_obj(
        {
            "page_id": normalized_page["page_id"],
            "canvas_shape": normalized_page["canvas_shape"],
            "blocks": blocks,
        }
    )


def type_fingerprint(normalized_page: dict[str, Any]) -> str:
    return sha256_obj(
        [
            {
                "block_id": block["block_id"],
                "block_type": block["block_type"],
            }
            for block in normalized_page["blocks"]
        ]
    )


def true_order_fingerprint(page: dict[str, Any]) -> str:
    values = [
        {
            "block_id": block["block_id"],
            "reading_order_true": block.get("reading_order_true"),
        }
        for block in page.get("blocks", [])
    ]
    return sha256_obj(values)


def canonical_normalized_bytes(
    page: dict[str, Any], config: dict[str, Any] | None = None
) -> bytes:
    return canonical_bytes(normalize_page(page, config))


def canonical_edges_bytes(
    page: dict[str, Any],
    config: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
) -> bytes:
    normalized = normalize_page(page, config)
    edges = build_candidate_edges(normalized, config, limits)["edges"]
    return canonical_bytes(edges)


def _baseline_sequence(blocks: list[dict[str, Any]]) -> list[int]:
    def phase(block: dict[str, Any]) -> int:
        if block["block_type"] == "header":
            return 0
        if block["block_type"] in {"title", "document_title"}:
            return 1
        if block["block_type"] == "footer":
            return 4
        return 2

    def key(block: dict[str, Any]) -> tuple[Any, ...]:
        return (
            phase(block),
            block["column_index"],
            block["y1"],
            block["x1"],
            block["block_id"],
        )

    return [block["block_id"] for block in sorted(blocks, key=key)]


def _pair_features(
    source: dict[str, Any],
    target: dict[str, Any],
    adjacent_gap_ratio: float,
    precision: int,
) -> dict[str, Any]:
    overlap_px = max(
        0.0,
        min(source["x2"], target["x2"]) - max(source["x1"], target["x1"]),
    )
    denom = min(source["width"], target["width"])
    horizontal_overlap = 0.0 if denom <= 0 else min(1.0, overlap_px / denom)
    raw_gap = target["y1"] - source["y2"]
    gap = max(0.0, raw_gap)
    vertical_adjacent = (
        raw_gap >= -1e-12 and gap <= adjacent_gap_ratio
    )
    same_column = source["column_index"] == target["column_index"]
    cross_zone = source["zone_id"] != target["zone_id"]
    target_below = target["center_y"] > source["center_y"] + 1e-12
    backward = target["center_y"] < source["center_y"] - 1e-12
    return {
        "same_column": same_column,
        "cross_column": not same_column,
        "cross_zone": cross_zone,
        "vertical_adjacent": vertical_adjacent,
        "target_below": target_below,
        "backward": backward,
        "horizontal_overlap": round_float(horizontal_overlap, precision),
        "vertical_gap": round_float(gap, precision),
        "dx": round_float(target["center_x"] - source["center_x"], precision),
        "dy": round_float(target["center_y"] - source["center_y"], precision),
    }


def _selection_key(
    features: dict[str, Any], target_id: int, precision: int
) -> tuple[Any, ...]:
    return (
        0 if features["target_below"] else 1,
        0 if features["same_column"] else 1,
        round_float(features["vertical_gap"], precision),
        round_float(1.0 - features["horizontal_overlap"], precision),
        round_float(abs(features["dx"]), precision),
        target_id,
    )


def _degraded_edges(
    normalized: dict[str, Any],
    cfg: dict[str, Any],
    category: str,
    reason: str,
    *,
    active: bool = True,
) -> dict[str, Any]:
    blocks = normalized["blocks"]
    by_id = {block["block_id"]: block for block in blocks}
    baseline = _baseline_sequence(blocks)
    pairs: list[tuple[int, int]] = []
    if len(baseline) == 1:
        pairs.append((baseline[0], baseline[0]))
    else:
        for left, right in zip(baseline, baseline[1:]):
            pairs.append((left, right))
        pairs.append((baseline[-1], baseline[0]))
    edges: list[dict[str, Any]] = []
    for source_id, target_id in pairs:
        source = by_id[source_id]
        target = by_id[target_id]
        if source_id == target_id:
            features = {
                "same_column": True,
                "cross_column": False,
                "cross_zone": False,
                "vertical_adjacent": False,
                "target_below": False,
                "backward": False,
                "horizontal_overlap": 1.0,
                "vertical_gap": 0.0,
                "dx": 0.0,
                "dy": 0.0,
                "self_loop": True,
            }
        else:
            features = _pair_features(
                source, target, cfg["adjacent_gap_ratio"], cfg["precision"]
            )
            features["self_loop"] = False
        edges.append(
            {
                "source_block_id": source_id,
                "target_block_id": target_id,
                "selection_key": [0, 0, 0.0, 0.0, 0.0, target_id],
                "candidate_rank": 0,
                "features": features,
            }
        )
    edges.sort(key=lambda edge: (edge["source_block_id"], edge["target_block_id"]))
    return {
        "page_id": normalized["page_id"],
        "edges": edges,
        "stats": {
            "block_count": len(blocks),
            "candidate_edge_count": len(edges),
            "pair_comparisons": len(pairs),
            "max_candidates_per_block": cfg["max_candidates_per_block"],
        },
        "degraded": {
            "active": active,
            "category": category,
            "reason": reason,
        },
    }


def build_candidate_edges(
    normalized_page: dict[str, Any],
    config: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build deterministic successor candidates and validate full coverage."""

    cfg = normalize_geometry_config(config)
    lim = normalize_limits(limits)
    blocks = sorted(normalized_page["blocks"], key=lambda item: item["block_id"])
    count = len(blocks)
    if count > lim["max_blocks_per_page"]:
        raise LayoutError(
            "E_RESOURCE_LIMIT",
            "page exceeds max_blocks_per_page",
            field="limits.max_blocks_per_page",
            page_id=normalized_page["page_id"],
            details={"block_count": count, "limit": lim["max_blocks_per_page"]},
        )
    if count == 1:
        return _degraded_edges(
            normalized_page,
            cfg,
            None,
            "single block page needs a coverage self-loop",
            active=False,
        )

    comparisons = count * (count - 1)
    estimated_bytes = count * 1_200 + comparisons * 280
    if estimated_bytes > lim["max_resident_memory_mb"] * 1024 * 1024:
        if lim["limit_policy"] == "error":
            raise LayoutError(
                "E_RESOURCE_LIMIT",
                "estimated resident block/edge data exceeds memory limit",
                field="limits.max_resident_memory_mb",
                page_id=normalized_page["page_id"],
                details={
                    "estimated_bytes": estimated_bytes,
                    "limit_mb": lim["max_resident_memory_mb"],
                },
            )
        result = _degraded_edges(
            normalized_page,
            cfg,
            "memory_budget",
            "candidate construction degraded to baseline chain",
        )
        result["stats"]["estimated_bytes"] = estimated_bytes
        return result

    if comparisons > lim["max_edge_comparisons"]:
        if lim["limit_policy"] == "error":
            raise LayoutError(
                "E_RESOURCE_LIMIT",
                "candidate pair comparisons exceed limit",
                field="limits.max_edge_comparisons",
                page_id=normalized_page["page_id"],
                details={
                    "pair_comparisons": comparisons,
                    "limit": lim["max_edge_comparisons"],
                },
            )
        result = _degraded_edges(
            normalized_page,
            cfg,
            "edge_comparison_budget",
            "candidate construction degraded to baseline chain",
        )
        result["stats"]["estimated_bytes"] = estimated_bytes
        return result

    by_source: dict[int, list[dict[str, Any]]] = {block["block_id"]: [] for block in blocks}
    for source in blocks:
        for target in blocks:
            if source["block_id"] == target["block_id"]:
                continue
            features = _pair_features(
                source, target, cfg["adjacent_gap_ratio"], cfg["precision"]
            )
            features["self_loop"] = False
            edges = by_source[source["block_id"]]
            edges.append(
                {
                    "source_block_id": source["block_id"],
                    "target_block_id": target["block_id"],
                    "selection_key": [
                        *_selection_key(features, target["block_id"], cfg["precision"])
                    ],
                    "candidate_rank": 0,
                    "features": features,
                }
            )
    baseline = _baseline_sequence(blocks)
    forced = set(zip(baseline, baseline[1:]))
    by_id = {block["block_id"]: block for block in blocks}
    selected: list[dict[str, Any]] = []
    for source_id in sorted(by_source):
        candidates = sorted(
            by_source[source_id],
            key=lambda edge: (
                tuple(edge["selection_key"]),
                edge["target_block_id"],
            ),
        )
        keep_ids = {
            edge["target_block_id"]
            for edge in candidates[: cfg["max_candidates_per_block"]]
        }
        keep_ids.update(target for left, target in forced if left == source_id)
        if not keep_ids:
            keep_ids.add(candidates[0]["target_block_id"])
        for edge in candidates:
            if edge["target_block_id"] in keep_ids:
                selected.append(edge)
    # The last baseline node must have an outgoing edge for full source coverage.
    if not any(edge["source_block_id"] == baseline[-1] for edge in selected):
        source = by_id[baseline[-1]]
        target = by_id[baseline[0]]
        features = _pair_features(
            source, target, cfg["adjacent_gap_ratio"], cfg["precision"]
        )
        features["self_loop"] = False
        selected.append(
            {
                "source_block_id": source["block_id"],
                "target_block_id": target["block_id"],
                "selection_key": [
                    *_selection_key(features, target["block_id"], cfg["precision"])
                ],
                "candidate_rank": 0,
                "features": features,
            }
        )
    selected.sort(
        key=lambda edge: (
            edge["source_block_id"],
            tuple(edge["selection_key"]),
            edge["target_block_id"],
        )
    )
    last_source: int | None = None
    rank = 0
    for edge in selected:
        if edge["source_block_id"] != last_source:
            last_source = edge["source_block_id"]
            rank = 0
        edge["candidate_rank"] = rank
        rank += 1
        edge["selection_key"] = [
            round_float(value, cfg["precision"])
            if isinstance(value, float)
            else value
            for value in edge["selection_key"]
        ]
    validate_candidate_coverage(normalized_page, selected)
    return {
        "page_id": normalized_page["page_id"],
        "edges": selected,
        "stats": {
            "block_count": count,
            "candidate_edge_count": len(selected),
            "pair_comparisons": comparisons,
            "max_candidates_per_block": cfg["max_candidates_per_block"],
            "estimated_bytes": estimated_bytes,
        },
        "degraded": {"active": False, "category": None, "reason": None},
    }


def validate_candidate_coverage(
    normalized_page: dict[str, Any], edges: Iterable[dict[str, Any]]
) -> None:
    block_ids = {block["block_id"] for block in normalized_page["blocks"]}
    edge_list = list(edges)
    sources = {edge["source_block_id"] for edge in edge_list}
    targets = {edge["target_block_id"] for edge in edge_list}
    missing_sources = sorted(block_ids - sources)
    missing_targets = sorted(block_ids - targets)
    if missing_sources or missing_targets:
        first = min(missing_sources + missing_targets)
        raise LayoutError(
            "E_BLOCK_COVERAGE",
            "candidate edges do not cover every block",
            block_id=first,
            page_id=normalized_page.get("page_id"),
            details={
                "missing_sources": missing_sources,
                "missing_targets": missing_targets,
            },
        )
    for edge in edge_list:
        if (
            edge["source_block_id"] not in block_ids
            or edge["target_block_id"] not in block_ids
        ):
            raise LayoutError(
                "E_BLOCK_COVERAGE",
                "candidate edge references an unknown block",
                block_id=edge["source_block_id"],
                page_id=normalized_page.get("page_id"),
            )
    duplicate_counter = Counter(
        (edge["source_block_id"], edge["target_block_id"]) for edge in edge_list
    )
    duplicate = next(
        (pair for pair in sorted(duplicate_counter) if duplicate_counter[pair] > 1),
        None,
    )
    if duplicate is not None:
        raise LayoutError(
            "E_BLOCK_COVERAGE",
            "duplicate candidate edge",
            block_id=duplicate[0],
            page_id=normalized_page.get("page_id"),
            details={"target_block_id": duplicate[1]},
        )


def edge_set_fingerprint(edges: list[dict[str, Any]]) -> str:
    return sha256_obj(edges)


def normalized_input_blob(page: dict[str, Any]) -> dict[str, Any]:
    return {
        "page_id": page.get("page_id"),
        "canvas_shape": page.get("canvas_shape"),
        "blocks": [
            {key: value for key, value in block.items() if key != "reading_order_true"}
            for block in page.get("blocks", [])
        ],
    }


def edge_stats_text(edges: list[dict[str, Any]]) -> str:
    return canonical_json(
        {
            "count": len(edges),
            "sources": len({edge["source_block_id"] for edge in edges}),
            "targets": len({edge["target_block_id"] for edge in edges}),
        }
    )

