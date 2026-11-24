"""Tile assembly, seam measurement and the JSON verification report."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .errors import DungeonError
from .models import RenderTask, TileRect
from .tiles import tile_index


def assemble(task: RenderTask, tile_arrays: dict[int, np.ndarray]) -> np.ndarray:
    canvas = np.zeros(
        (task.output_height, task.output_width), dtype=np.uint8
    )
    for index, image in sorted(tile_arrays.items()):
        tile = _tile_for_index(task, index)
        canvas[tile.y : tile.y + tile.height, tile.x : tile.x + tile.width] = image
    return canvas


def _tile_for_index(task: RenderTask, index: int) -> TileRect:
    row, col = divmod(index, task.tile_cols)
    width = task.output_width
    height = task.output_height
    x0 = col * width // task.tile_cols
    x1 = (col + 1) * width // task.tile_cols
    y0 = row * height // task.tile_rows
    y1 = (row + 1) * height // task.tile_rows
    return TileRect(col, row, x0, y0, x1 - x0, y1 - y0)


def _edge_deltas(image: np.ndarray, task: RenderTask) -> dict[int, int]:
    """Natural gray jump across each tile boundary edge for one image."""
    deltas: dict[int, int] = {}
    width = task.output_width
    height = task.output_height
    for row in range(task.tile_rows):
        y1 = (row + 1) * height // task.tile_rows
        for col in range(task.tile_cols):
            x1 = (col + 1) * width // task.tile_cols
            index = row * task.tile_cols + col
            local_max = 0
            if x1 < width:
                local_max = max(
                    local_max,
                    int(np.abs(
                        image[:, x1].astype(np.int16)
                        - image[:, x1 - 1].astype(np.int16)
                    ).max()),
                )
            if y1 < height:
                local_max = max(
                    local_max,
                    int(np.abs(
                        image[y1, :].astype(np.int16)
                        - image[y1 - 1, :].astype(np.int16)
                    ).max()),
                )
            deltas[index] = local_max
    return deltas


def tile_seam_deltas(
    assembled: np.ndarray, reference: np.ndarray, task: RenderTask
) -> dict[int, int]:
    """Tiling-induced seam magnitude.

    A dungeon edge (wall versus floor) naturally jumps in gray, so the raw
    cross-edge delta is not a seam defect.  The defect is the difference
    between the edge jump in the assembled image and the same edge in the
    whole-image reference; a correctly tiled render yields zero everywhere.
    """
    assembled_edges = _edge_deltas(assembled, task)
    reference_edges = _edge_deltas(reference, task)
    return {
        index: abs(assembled_edges[index] - reference_edges[index])
        for index in assembled_edges
    }


def compare_to_reference(
    actual: np.ndarray, reference: np.ndarray
) -> tuple[int, tuple[int, int] | None]:
    diff = np.abs(actual.astype(np.int16) - reference.astype(np.int16))
    max_delta = int(diff.max())
    first = None
    if max_delta:
        row, col = np.unravel_index(int(np.argmax(diff == max_delta)), diff.shape)
        first = (int(col), int(row))
    return max_delta, first


def containing_tile(task: RenderTask, x: int, y: int) -> tuple[int, int]:
    width = task.output_width
    height = task.output_height
    col = min(task.tile_cols - 1, x * task.tile_cols // max(width, 1))
    row = min(task.tile_rows - 1, y * task.tile_rows // max(height, 1))
    return int(col), int(row)


def build_report(
    task: RenderTask,
    tiles: list[TileRect],
    tile_status: dict[int, dict],
    gray_canvas: np.ndarray,
    outline_canvas: np.ndarray,
    gray_reference: np.ndarray,
    outline_reference: np.ndarray,
    business_codes: list[str],
    archived_versions: list[str],
) -> dict:
    gray_seams = tile_seam_deltas(gray_canvas, gray_reference, task)
    outline_seams = tile_seam_deltas(outline_canvas, outline_reference, task)
    gray_delta, gray_first = compare_to_reference(gray_canvas, gray_reference)
    outline_delta, outline_first = compare_to_reference(
        outline_canvas, outline_reference
    )

    tile_reports = []
    for tile in tiles:
        index = tile_index(task, tile)
        status = tile_status[index]
        tile_reports.append(
            {
                "index": index,
                "col": tile.col,
                "row": tile.row,
                "offset": [tile.x, tile.y],
                "coverage": [
                    tile.x,
                    tile.y,
                    tile.x + tile.width,
                    tile.y + tile.height,
                ],
                "declared_pixels": tile.pixel_count,
                "status": status.get("status"),
                "invalid_reason": status.get("invalid_reason"),
                "gray_seam_max_delta": gray_seams[index],
                "outline_seam_max_delta": outline_seams[index],
            }
        )

    seam_failures = [
        entry
        for entry in tile_reports
        if entry["gray_seam_max_delta"] > task.tolerance
        or entry["outline_seam_max_delta"] > task.tolerance
    ]
    mismatch_location = None
    if gray_first is not None:
        col, row = containing_tile(task, *gray_first)
        mismatch_location = {
            "image": "gray",
            "pixel": list(gray_first),
            "tile": [col, row],
            "max_delta": gray_delta,
        }
    elif outline_first is not None:
        col, row = containing_tile(task, *outline_first)
        mismatch_location = {
            "image": "outline",
            "pixel": list(outline_first),
            "tile": [col, row],
            "max_delta": outline_delta,
        }

    ok = (
        not seam_failures
        and gray_delta <= task.tolerance
        and outline_delta <= task.tolerance
    )
    return {
        "ok": ok,
        "business_codes": sorted(set(business_codes)),
        "archived_versions": archived_versions,
        "tolerance": task.tolerance,
        "tiles": tile_reports,
        "whole_image": {
            "gray_max_delta": gray_delta,
            "outline_max_delta": outline_delta,
            "first_mismatch": mismatch_location,
        },
        "seam_failures": seam_failures,
    }


def raise_if_seam_failure(report: dict) -> None:
    if report["ok"]:
        return
    first_tile = None
    first_pixel = None
    if report["seam_failures"]:
        failure = report["seam_failures"][0]
        first_tile = [failure["col"], failure["row"]]
    mismatch = report["whole_image"]["first_mismatch"]
    if mismatch is not None:
        first_pixel = mismatch["pixel"]
        first_tile = first_tile or mismatch["tile"]
    raise DungeonError(
        "E_SEAM",
        "assembled image differs from the whole-image reference beyond tolerance",
        {"first_pixel": first_pixel, "tile": first_tile},
    )


def write_report(output_dir: Path, report: dict) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "verification_report.json"
    from .persistence import atomic_write_json

    atomic_write_json(path, report)
    return path
