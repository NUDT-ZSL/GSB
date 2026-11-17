from __future__ import annotations

import statistics
from typing import Any

from .config import ColumnConfig


def _overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def _width_stability(widths: list[int]) -> float:
    if not widths:
        return 0.0
    if len(widths) == 1:
        return 1.0
    mean = statistics.fmean(widths)
    if mean <= 0:
        return 0.0
    deviation = statistics.pstdev(widths)
    return round(max(0.0, min(1.0, 1.0 - deviation / mean)), 6)


def _disabled_single_column(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    if blocks:
        x0 = min(int(block["bbox"][0]) for block in blocks)
        x1 = max(int(block["bbox"][0]) + int(block["bbox"][2]) for block in blocks)
    else:
        x0 = x1 = 0
    members = [block["block_id"] for block in blocks]
    groups = [
        {
            "column_id": "c0",
            "x0": x0,
            "x1": x1,
            "width": x1 - x0,
            "block_count": len(members),
            "block_ids": members,
            "seed_block_ids": members,
            "width_stability": 1.0 if members else 0.0,
        }
    ]
    assignments = {
        block["block_id"]: {
            "column_group": "c0",
            "spanning": False,
            "covered_column_groups": ["c0"],
            "conflict": None,
        }
        for block in blocks
    }
    return {
        "enabled": False,
        "mode": "single_column_disabled",
        "groups": groups,
        "assignments": assignments,
        "conflicts": [],
        "merge_decisions": [],
        "spanning_block_ids": [],
    }


def _cluster_seed_groups(
    seeds: list[dict[str, Any]], config: ColumnConfig
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups: list[dict[str, Any]] = []
    for block in sorted(
        seeds,
        key=lambda item: (
            int(item["bbox"][1]),
            int(item["bbox"][0]),
            str(item["block_id"]),
        ),
    ):
        x0 = int(block["bbox"][0])
        width = int(block["bbox"][2])
        x1 = x0 + width
        candidates: list[tuple[float, int]] = []
        for index, group in enumerate(groups):
            overlap = _overlap(x0, x1, int(group["x0"]), int(group["x1"]))
            ratio = overlap / max(1, min(width, int(group["x1"]) - int(group["x0"])))
            if ratio >= config.overlap_threshold:
                candidates.append((ratio, index))
        if not candidates:
            groups.append(
                {
                    "x0": x0,
                    "x1": x1,
                    "seed_widths": [width],
                    "seed_block_ids": [block["block_id"]],
                }
            )
            continue
        candidates.sort(key=lambda item: (-item[0], item[1]))
        chosen = groups[candidates[0][1]]
        chosen["x0"] = min(int(chosen["x0"]), x0)
        chosen["x1"] = max(int(chosen["x1"]), x1)
        chosen["seed_widths"].append(width)
        chosen["seed_block_ids"].append(block["block_id"])

    groups.sort(key=lambda group: (int(group["x0"]), int(group["x1"])))
    merge_decisions: list[dict[str, Any]] = []
    merged: list[dict[str, Any]] = []
    for group in groups:
        if not merged:
            merged.append(group)
            continue
        previous = merged[-1]
        gap = int(group["x0"]) - int(previous["x1"])
        if gap < config.gap_threshold:
            merge_decisions.append(
                {
                    "type": "gap_merge",
                    "left_range": [int(previous["x0"]), int(previous["x1"])],
                    "right_range": [int(group["x0"]), int(group["x1"])],
                    "gap": gap,
                    "gap_threshold": config.gap_threshold,
                    "winner_range": [
                        min(int(previous["x0"]), int(group["x0"])),
                        max(int(previous["x1"]), int(group["x1"])),
                    ],
                    "reason": "gap_below_column_gap_threshold",
                }
            )
            previous["x0"] = min(int(previous["x0"]), int(group["x0"]))
            previous["x1"] = max(int(previous["x1"]), int(group["x1"]))
            previous["seed_widths"].extend(group["seed_widths"])
            previous["seed_block_ids"].extend(group["seed_block_ids"])
        else:
            merged.append(group)
    return merged, merge_decisions


def infer_columns(
    blocks: list[dict[str, Any]],
    role_records: list[dict[str, Any]],
    *,
    config: ColumnConfig,
) -> dict[str, Any]:
    if not config.enabled:
        return _disabled_single_column(blocks)
    if not blocks:
        return _disabled_single_column(blocks)

    role_by_id = {record["block_id"]: record["role"] for record in role_records}
    body_seeds = [
        block
        for block in blocks
        if role_by_id.get(block["block_id"], "body") == "body"
    ]
    if not body_seeds:
        body_seeds = [
            block
            for block in blocks
            if role_by_id.get(block["block_id"]) != "header_footer"
        ]
    clustered, merge_decisions = _cluster_seed_groups(body_seeds or blocks, config)
    for index, group in enumerate(clustered):
        group["column_id"] = f"c{index}"
        group["width"] = int(group["x1"]) - int(group["x0"])
        group["width_stability"] = _width_stability(group["seed_widths"])
        group["seed_block_ids"] = sorted(group["seed_block_ids"])
        group["seed_block_count"] = len(group["seed_block_ids"])
        group["block_ids"] = []
        group["block_count"] = 0

    if not clustered:
        return _disabled_single_column(blocks)

    content_x0 = min(int(group["x0"]) for group in clustered)
    content_x1 = max(int(group["x1"]) for group in clustered)
    content_width = max(1, content_x1 - content_x0)

    assignments: dict[str, dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    for block in sorted(
        blocks,
        key=lambda item: (
            int(item["bbox"][1]),
            int(item["bbox"][0]),
            str(item["block_id"]),
        ),
    ):
        block_id = block["block_id"]
        x0 = int(block["bbox"][0])
        x1 = x0 + int(block["bbox"][2])
        overlaps: list[dict[str, Any]] = []
        for group in clustered:
            overlap = _overlap(x0, x1, int(group["x0"]), int(group["x1"]))
            if overlap > 0:
                overlaps.append(
                    {
                        "column_id": group["column_id"],
                        "overlap_px": overlap,
                        "overlap_ratio": round(overlap / max(1, x1 - x0), 6),
                        "width_stability": group["width_stability"],
                        "seed_block_count": group["seed_block_count"],
                    }
                )

        width_ratio = (x1 - x0) / content_width
        may_span = (
            len(clustered) >= 2
            and width_ratio >= config.spanning_width_ratio
            and len(overlaps) >= 2
        )
        if may_span:
            covered = [item["column_id"] for item in overlaps]
            assignments[block_id] = {
                "column_group": "span",
                "spanning": True,
                "covered_column_groups": covered,
                "conflict": None,
            }
            continue

        if overlaps:
            if len(overlaps) > 1:
                ordered = sorted(
                    overlaps,
                    key=lambda item: (
                        -item["width_stability"],
                        -item["seed_block_count"],
                        -item["overlap_ratio"],
                        item["column_id"],
                    ),
                )
                conflict = {
                    "block_id": block_id,
                    "reason": "multiple_column_overlap",
                    "candidates": ordered,
                    "decision": "width_stability_then_seed_block_count_then_overlap_then_column_id",
                    "winner": ordered[0]["column_id"],
                }
                conflicts.append(conflict)
                winner = ordered[0]["column_id"]
                assignments[block_id] = {
                    "column_group": winner,
                    "spanning": False,
                    "covered_column_groups": [winner],
                    "conflict": conflict,
                }
            else:
                winner = overlaps[0]["column_id"]
                assignments[block_id] = {
                    "column_group": winner,
                    "spanning": False,
                    "covered_column_groups": [winner],
                    "conflict": None,
                }
        else:
            nearest = sorted(
                clustered,
                key=lambda group: (
                    max(int(group["x0"]) - x1, x0 - int(group["x1"]), 0),
                    -group["width_stability"],
                    -group["seed_block_count"],
                    group["column_id"],
                ),
            )[0]
            conflict = {
                "block_id": block_id,
                "reason": "no_column_overlap",
                "candidates": [
                    {
                        "column_id": group["column_id"],
                        "overlap_px": 0,
                        "overlap_ratio": 0.0,
                        "width_stability": group["width_stability"],
                        "seed_block_count": group["seed_block_count"],
                        "horizontal_distance": max(
                            int(group["x0"]) - x1, x0 - int(group["x1"]), 0
                        ),
                    }
                    for group in clustered
                ],
                "decision": "nearest_then_width_stability_then_seed_block_count_then_column_id",
                "winner": nearest["column_id"],
            }
            conflicts.append(conflict)
            assignments[block_id] = {
                "column_group": nearest["column_id"],
                "spanning": False,
                "covered_column_groups": [nearest["column_id"]],
                "conflict": conflict,
            }

    group_by_id = {group["column_id"]: group for group in clustered}
    for block in blocks:
        assignment = assignments[block["block_id"]]
        if not assignment["spanning"]:
            group_by_id[assignment["column_group"]]["block_ids"].append(block["block_id"])
    for group in clustered:
        group["block_ids"] = sorted(group["block_ids"])
        group["block_count"] = len(group["block_ids"])

    output_groups = []
    for group in clustered:
        output_groups.append(
            {
                "column_id": group["column_id"],
                "x0": int(group["x0"]),
                "x1": int(group["x1"]),
                "width": int(group["width"]),
                "block_count": int(group["block_count"]),
                "block_ids": list(group["block_ids"]),
                "seed_block_ids": list(group["seed_block_ids"]),
                "width_stability": float(group["width_stability"]),
            }
        )
    spanning_ids = sorted(
        block_id for block_id, value in assignments.items() if value["spanning"]
    )
    return {
        "enabled": True,
        "mode": "inferred",
        "groups": output_groups,
        "assignments": assignments,
        "conflicts": conflicts,
        "merge_decisions": merge_decisions,
        "spanning_block_ids": spanning_ids,
    }
