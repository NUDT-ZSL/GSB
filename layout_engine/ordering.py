from __future__ import annotations

from typing import Any, Iterable

from .config import ReadingConfig


def _region_band_index(blocks: list[dict[str, Any]], tolerance: int) -> dict[str, int]:
    ordered = sorted(
        blocks,
        key=lambda block: (
            int(block["bbox"][1]),
            int(block["bbox"][0]),
            str(block["block_id"]),
        ),
    )
    result: dict[str, int] = {}
    band = -1
    band_start = 0
    for block in ordered:
        y = int(block["bbox"][1])
        if band < 0 or y - band_start > tolerance:
            band += 1
            band_start = y
        result[block["block_id"]] = band
    return result


def _spanning_basis(entry: dict[str, Any], rule: str) -> str:
    covered = ",".join(entry["covered_column_groups"])
    return (
        f"{rule}:spanning_boundary;covered={covered};"
        f"y={entry['bbox'][1]};x={entry['bbox'][0]}"
    )


def order_blocks(
    blocks: list[dict[str, Any]],
    column_result: dict[str, Any],
    *,
    config: ReadingConfig,
) -> dict[str, Any]:
    group_index = {
        group["column_id"]: index
        for index, group in enumerate(column_result.get("groups", []))
    }
    entries: dict[str, dict[str, Any]] = {}
    spanning: list[dict[str, Any]] = []
    regular: list[dict[str, Any]] = []
    for block in blocks:
        assignment = column_result["assignments"][block["block_id"]]
        entry = {
            "block_id": block["block_id"],
            "bbox": list(block["bbox"]),
            "column_group": assignment["column_group"],
            "spanning": bool(assignment["spanning"]),
            "covered_column_groups": list(assignment["covered_column_groups"]),
        }
        entries[block["block_id"]] = entry
        if entry["spanning"]:
            spanning.append(entry)
        else:
            regular.append(entry)

    spanning.sort(
        key=lambda entry: (
            int(entry["bbox"][1]),
            int(entry["bbox"][0]),
            str(entry["block_id"]),
        )
    )
    regular_by_boundary_band: dict[int, list[dict[str, Any]]] = {
        index: [] for index in range(len(spanning) + 1)
    }
    for entry in regular:
        block_y = int(entry["bbox"][1])
        boundary_band = sum(1 for item in spanning if int(item["bbox"][1]) <= block_y)
        regular_by_boundary_band[boundary_band].append(entry)

    if config.rule == "column_first":
        for group_entries in regular_by_boundary_band.values():
            group_entries.sort(
                key=lambda entry: (
                    group_index.get(entry["column_group"], 0),
                    int(entry["bbox"][1]),
                    int(entry["bbox"][0]),
                    str(entry["block_id"]),
                )
            )
    else:
        for group_entries in regular_by_boundary_band.values():
            region_bands = _region_band_index(group_entries, config.region_band_tolerance)
            group_entries.sort(
                key=lambda entry: (
                    region_bands[entry["block_id"]],
                    int(entry["bbox"][0]),
                    int(entry["bbox"][1]),
                    str(entry["block_id"]),
                )
            )

    sequence: list[tuple[dict[str, Any], str]] = []
    for boundary_band in range(len(spanning) + 1):
        sequence.extend((entry, "regular") for entry in regular_by_boundary_band[boundary_band])
        if boundary_band < len(spanning):
            sequence.append((spanning[boundary_band], "spanning"))

    output_entries: list[dict[str, Any]] = []
    assignments: dict[str, dict[str, Any]] = {}
    for reading_index, (entry, kind) in enumerate(sequence, start=1):
        y = int(entry["bbox"][1])
        x = int(entry["bbox"][0])
        if kind == "spanning":
            basis = _spanning_basis(entry, config.rule)
        elif config.rule == "column_first":
            basis = (
                f"column_first:group={entry['column_group']};y={y};x={x};"
                "tie=left_top_coordinate_then_block_id"
            )
        else:
            basis = f"region_first:y_band_tolerance={config.region_band_tolerance};y={y};x={x};tie=x_then_y_then_block_id"
        output = {
            "reading_index": reading_index,
            "block_id": entry["block_id"],
            "bbox": entry["bbox"],
            "column_group": entry["column_group"],
            "spanning": entry["spanning"],
            "covered_column_groups": entry["covered_column_groups"],
            "basis": basis,
        }
        output_entries.append(output)
        assignments[entry["block_id"]] = {
            "reading_index": reading_index,
            "order_basis": basis,
        }
    return {
        "rule": config.rule,
        "entries": output_entries,
        "assignments": assignments,
        "spanning_boundary_count": len(spanning),
        "spanning_block_ids": [entry["block_id"] for entry in spanning],
    }
