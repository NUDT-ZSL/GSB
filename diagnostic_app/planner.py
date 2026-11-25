from __future__ import annotations

import math
from typing import Any

import numpy as np

from .config import config_checksum
from .errors import AppError, ErrorCode
from .hashing import sha256_json


def partition_edges(length: int, parts: int) -> list[int]:
    if parts < 1 or parts > length:
        raise AppError(ErrorCode.INVALID_REQUEST, f"cannot split length {length} into {parts} blocks")
    return [int(round(i * length / parts)) for i in range(parts + 1)]


def build_block_descriptors(height: int, width: int, rows: int, cols: int) -> list[dict[str, Any]]:
    row_edges = partition_edges(height, rows)
    col_edges = partition_edges(width, cols)
    center_row = rows // 2
    center_col = cols // 2
    blocks: list[dict[str, Any]] = []
    for row in range(rows):
        for col in range(cols):
            index = row * cols + col
            r0, r1 = row_edges[row], row_edges[row + 1]
            c0, c1 = col_edges[col], col_edges[col + 1]
            descriptor = {
                "block_index": index,
                "row_index": row,
                "col_index": col,
                "row_start": r0,
                "row_end": r1,
                "col_start": c0,
                "col_end": c1,
                "height": r1 - r0,
                "width": c1 - c0,
                "pixel_count": (r1 - r0) * (c1 - c0),
                "pixel_interval": {
                    "rows": [r0, r1],
                    "cols": [c0, c1],
                    "half_open_text": f"rows[{r0}:{r1}),cols[{c0}:{c1})",
                    "inclusive": {
                        "row_start": r0,
                        "row_end": max(r0, r1 - 1),
                        "col_start": c0,
                        "col_end": max(c0, c1 - 1),
                    },
                },
                "is_center": row == center_row and col == center_col,
            }
            blocks.append(descriptor)
    return blocks


def center_block_index(rows: int, cols: int) -> int:
    return (rows // 2) * cols + (cols // 2)


def estimate_latency(block: dict[str, Any], channels: int, steps: int,
                     config: dict[str, Any]) -> dict[str, Any]:
    cost = config["cost_model"]
    radius = int(config["iteration"].get("coupling_radius", 1))
    coupling_strength = float(config["iteration"].get("coupling_strength", 0.42))
    halo = int(steps) * radius
    local_height = int(block["height"]) + 2 * halo
    local_width = int(block["width"]) + 2 * halo
    scalar_updates = local_height * local_width * channels * int(steps)
    tap_scale = 1.0 + 4.0 * coupling_strength
    estimated_ms = (
        float(cost["fixed_overhead_ms"])
        + scalar_updates
        * float(cost["ms_per_value_step_tap"])
        * tap_scale
        * float(cost.get("halo_cost_factor", 1.0))
    )
    return {
        "estimated_first_block_ms": estimated_ms,
        "block_pixels": int(block["pixel_count"]),
        "block_values": int(block["pixel_count"]) * channels,
        "halo_cells_per_side": halo,
        "local_shape_with_halo": [local_height, local_width, channels],
        "sampler_steps": int(steps),
        "neighbor_taps": 4,
        "coupling_scale": tap_scale,
        "scalar_updates": scalar_updates,
        "formula": "fixed + local_values_with_halo * steps * ms_per_value_step_tap * (1 + 4*coupling_strength) * halo_factor",
    }


def estimate_memory(block: dict[str, Any], channels: int, steps: int,
                    config: dict[str, Any]) -> dict[str, Any]:
    radius = int(config["iteration"].get("coupling_radius", 1))
    halo = int(steps) * radius
    local_height = int(block["height"]) + 2 * halo
    local_width = int(block["width"]) + 2 * halo
    profile = str(config.get("_precision_profile", "float32")).lower()
    dtype_bytes = 8 if "float64" in profile or "fp64" in profile or "high" in profile else 4
    copies = int(config["resource_limits"].get("block_buffer_copies", 5))
    work_bytes = local_height * local_width * channels * dtype_bytes * copies
    output_bytes = int(block["pixel_count"]) * channels * dtype_bytes
    u8_bytes = int(block["pixel_count"]) * channels
    total = work_bytes + output_bytes + u8_bytes
    return {
        "estimated_block_memory_bytes": int(total),
        "work_buffer_bytes": int(work_bytes),
        "output_bytes": int(output_bytes + u8_bytes),
        "dtype_bytes": dtype_bytes,
        "buffer_copies": copies,
    }


def boundary_metrics(height: int, width: int, rows: int, cols: int, steps: int) -> dict[str, Any]:
    adjacency_links = (cols - 1) * height + (rows - 1) * width
    interfaces = rows * max(0, cols - 1) + cols * max(0, rows - 1)
    total_pixels = height * width
    risk = adjacency_links * (1 + steps) / max(1, total_pixels)
    return {
        "boundary_adjacency_links": int(adjacency_links),
        "block_interface_count": int(interfaces),
        "boundary_risk_score": float(risk),
        "boundary_state_count_estimate": int(adjacency_links * (1 + steps)),
    }


def check_total_resources(task: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    height, width, channels = task["image_shape"]
    total_pixels = int(height) * int(width)
    limits = config["resource_limits"]
    max_total_pixels = int(limits["max_total_pixels"])
    if total_pixels > max_total_pixels:
        raise AppError(
            ErrorCode.RESOURCE_LIMIT,
            "generation pixel total exceeds configured limit",
            {
                "resource": "max_total_pixels",
                "total_pixels": total_pixels,
                "limit": max_total_pixels,
                "shape": [height, width, channels],
            },
        )
    return {"total_pixels": total_pixels, "total_values": total_pixels * channels,
            "limit_pixels": max_total_pixels}


def candidate_grids(config: dict[str, Any]) -> list[tuple[int, int]]:
    raw = config["grid"].get("candidates", [[1, 1], [2, 2], [3, 3]])
    grids = [(int(item[0]), int(item[1])) for item in raw]
    max_side = int(config["grid"].get("max_grid_side", 8))
    for side in range(1, max_side + 1):
        grids.append((side, side))
    # Stable de-duplication.
    seen: set[tuple[int, int]] = set()
    result = []
    for grid in grids:
        if grid not in seen and grid[0] <= max_side and grid[1] <= max_side:
            seen.add(grid)
            result.append(grid)
    return result


def evaluate_grid(task: dict[str, Any], config: dict[str, Any],
                  rows: int, cols: int) -> dict[str, Any]:
    height, width, channels = task["image_shape"]
    steps = int(task["sampler_steps"])
    blocks = build_block_descriptors(height, width, rows, cols)
    center_index = center_block_index(rows, cols)
    center = blocks[center_index]
    latency = estimate_latency(center, channels, steps, config)
    memory_config = dict(config)
    memory_config["_precision_profile"] = task["precision_profile"]
    memory = estimate_memory(center, channels, steps, memory_config)
    boundaries = boundary_metrics(height, width, rows, cols, steps)
    budget = float(config["latency_budget_ms"])
    memory_limit = int(config["resource_limits"]["max_block_memory_bytes"])
    max_block_pixels = int(config["grid"].get("max_block_pixels", 10**12))
    min_block_pixels = int(config["grid"].get("min_block_pixels", 1))
    target_pixels = float(config["grid"].get("target_block_pixels", max_block_pixels))
    block_pixels = int(center["pixel_count"])
    evaluation = {
        "rows": rows,
        "cols": cols,
        "grid_text": f"{rows}x{cols}",
        "center_block_index": center_index,
        "center_bounds": center["pixel_interval"],
        **latency,
        **memory,
        **boundaries,
        "latency_budget_ms": budget,
        "latency_slack_ms": budget - latency["estimated_first_block_ms"],
        "latency_feasible": latency["estimated_first_block_ms"] <= budget,
        "memory_feasible": memory["estimated_block_memory_bytes"] <= memory_limit,
        "memory_limit_bytes": memory_limit,
        "grid_max_pixels_feasible": block_pixels <= max_block_pixels,
        "grid_min_pixels_feasible": block_pixels >= min_block_pixels,
        "partition_target_distance": abs(math.log(max(1.0, block_pixels) / max(1.0, target_pixels))),
    }
    if boundaries["boundary_risk_score"] <= 0.18:
        evaluation["recommended_check_strength"] = "checksum_plus_bytewise_on_acceptance"
    elif boundaries["boundary_risk_score"] <= 0.42:
        evaluation["recommended_check_strength"] = "hash_and_interval_plus_bytewise_on_acceptance"
    else:
        evaluation["recommended_check_strength"] = "bytewise"
    return evaluation


def plan_blocks(task: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    resource_usage = check_total_resources(task, config)
    height, width, channels = task["image_shape"]
    evaluations = [evaluate_grid(task, config, rows, cols) for rows, cols in candidate_grids(config)]
    usable = [
        item for item in evaluations
        if item["memory_feasible"] and item["grid_max_pixels_feasible"]
    ]
    downgrade_category: str | None = None
    if not usable:
        if config["resource_limits"].get("allow_smaller_block_downgrade", True):
            max_side = int(config["grid"].get("max_grid_side", 8))
            finest = evaluate_grid(task, config, max_side, max_side)
            if finest["memory_feasible"]:
                usable = [finest]
                downgrade_category = "block_memory_or_grid_rule_to_finest_grid"
            else:
                raise AppError(
                    ErrorCode.RESOURCE_LIMIT,
                    "even the smallest configured block exceeds memory estimate",
                    {"resource": "max_block_memory_bytes",
                     "estimated": finest["estimated_block_memory_bytes"],
                     "limit": finest["memory_limit_bytes"]},
                )
        else:
            failed = sorted(evaluations, key=lambda item: item["estimated_block_memory_bytes"])[0]
            raise AppError(
                ErrorCode.RESOURCE_LIMIT,
                "no block grid satisfies memory/grid limits and downgrade is disabled",
                {"estimated": failed["estimated_block_memory_bytes"], "limit": failed["memory_limit_bytes"]},
            )

    priority_order = list(config["resolution_policy"].get(
        "priority_order",
        ["latency_budget", "consistency_strength", "block_partition"],
    ))

    def latency_key(item: dict[str, Any]):
        if item["latency_feasible"]:
            return (0, -int(item["center_block_pixels" if False else "block_pixels"]),
                    item["boundary_risk_score"])
        return (1, item["estimated_first_block_ms"] / max(1e-9, item["latency_budget_ms"]),
                item["boundary_risk_score"])

    def consistency_key(item: dict[str, Any]):
        return (item["boundary_risk_score"], -int(item["block_pixels"]))

    def partition_key(item: dict[str, Any]):
        return (item["partition_target_distance"], item["boundary_risk_score"])

    key_map = {
        "latency_budget": latency_key,
        "latency": latency_key,
        "consistency_strength": consistency_key,
        "consistency": consistency_key,
        "block_partition": partition_key,
        "partition": partition_key,
    }
    ordered_dimensions = [key_map[name] for name in priority_order if name in key_map]
    if not ordered_dimensions:
        ordered_dimensions = [latency_key, consistency_key, partition_key]

    def sort_key(item):
        return tuple(dimension(item) for dimension in ordered_dimensions)

    selected = sorted(usable, key=sort_key)[0]
    latency_feasible = bool(selected["latency_feasible"])
    rationale: list[str] = []
    reason_codes: list[str] = []
    rationale.append(
        f"priority_order={priority_order}; selected {selected['grid_text']} "
        f"with center block {selected['block_pixels']} pixels"
    )
    if priority_order and priority_order[0] in ("latency_budget", "latency"):
        feasible = [item for item in usable if item["latency_feasible"]]
        if feasible:
            coarsest = sorted(feasible, key=lambda item: item["boundary_risk_score"])[0]
            rationale.append(
                "latency first: retained the coarsest grid within budget to minimize boundary coupling states"
            )
            reason_codes.append("LATENCY_FIRST_COARSEST_FEASIBLE")
            if coarsest["grid_text"] != selected["grid_text"]:
                selected = coarsest
        else:
            finest = sorted(usable, key=lambda item: item["estimated_first_block_ms"])[0]
            rationale.append("no candidate meets latency budget; selected finest available grid and flagging budget risk")
            reason_codes.append("LATENCY_BUDGET_NOT_FEASIBLE_FINEST_FALLBACK")
            selected = finest
            downgrade_category = downgrade_category or "latency_budget_to_finer_blocks"
    elif priority_order[0] in ("consistency_strength", "consistency"):
        coarsest = sorted(usable, key=consistency_key)[0]
        selected = coarsest
        rationale.append("consistency first: minimized boundary links and selected bytewise verification")
        reason_codes.append("CONSISTENCY_FIRST_MIN_BOUNDARIES")
        if not selected["latency_feasible"]:
            rationale.append("latency budget conflicts with consistency-first choice; retaining stronger/coarser plan")
            reason_codes.append("LATENCY_CONFLICT_DEFERRED_TO_POLICY")
    elif priority_order[0] in ("block_partition", "partition"):
        rationale.append("partition first: minimized distance to target block pixels")
        reason_codes.append("PARTITION_TARGET_FIRST")

    if downgrade_category:
        reason_codes.append("RESOURCE_DOWNGRADE")
        rationale.append(f"downgrade category={downgrade_category}")
    elif any(not item["memory_feasible"] for item in usable + evaluations):
        memory_limited = [item["grid_text"] for item in evaluations if not item["memory_feasible"]]
        downgrade_category = "block_memory_limit"
        reason_codes.append("RESOURCE_DOWNGRADE")
        rationale.append(
            f"block memory cap excluded {memory_limited}; selected a smaller feasible grid"
        )

    blocks = build_block_descriptors(height, width, int(selected["rows"]), int(selected["cols"]))
    center_index = int(selected["center_block_index"])
    plan = {
        "schema": "block-generation-plan-v1",
        "task_id": task["task_id"],
        "config_checksum": config_checksum(config),
        "pipeline_version": config.get("pipeline_version", "det-cond-v1"),
        "grid": {"rows": int(selected["rows"]), "cols": int(selected["cols"])},
        "center_block_index": center_index,
        "generation_order": [center_index] + [b["block_index"] for b in blocks if b["block_index"] != center_index],
        "blocks": blocks,
        "selected_estimate": {k: v for k, v in selected.items() if k not in ("rows", "cols")},
        "candidate_evaluations": evaluations,
        "adjudication": {
            "priority_order": priority_order,
            "reason_codes": reason_codes,
            "rationale": rationale,
            "latency_budget_feasible": bool(selected["latency_feasible"]),
            "consistency_check_strength": config["resolution_policy"].get("consistency_check", "bytewise"),
            "downgrade_applied": downgrade_category is not None,
            "downgrade_category": downgrade_category,
        },
        "resource_usage": resource_usage,
    }
    plan["plan_checksum"] = sha256_json({k: v for k, v in plan.items() if k != "plan_checksum"})
    return plan
