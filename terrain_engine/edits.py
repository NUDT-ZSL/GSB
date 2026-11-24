"""Terrain edit batches and the tile invalidation closure.

Edits arrive in ordered batches.  Applying them to the integer height field
is deterministic, and every edit records which committed tiles it invalidates
by intersecting its rectangle with each tile's persisted dependency range
(the global grid indices read for shading/normal evaluation).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

from . import errors
from .model import Edit, Terrain
from .validate import _load_json


def load_edits(path: Path) -> List[Edit]:
    data = _load_json(path)
    raw = data.get("edits")
    if not isinstance(raw, list):
        raise errors.fail(errors.E_SCHEMA,
                          "edit file must contain an 'edits' list")
    edits: List[Edit] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise errors.fail(errors.E_SCHEMA,
                              "each edit must be an object", index=index)
        op = item.get("op")
        if op not in ("raise", "lower", "smooth"):
            raise errors.fail(errors.E_SCHEMA,
                              "edit op must be raise, lower or smooth",
                              index=index, got=op)
        batch = item.get("batch")
        if not isinstance(batch, int) or isinstance(batch, bool) or batch < 0:
            raise errors.fail(errors.E_SCHEMA,
                              "edit batch must be a non-negative integer",
                              index=index)
        coords = [item.get(key) for key in ("x0", "y0", "x1", "y1")]
        if any(not isinstance(value, int) or isinstance(value, bool)
               for value in coords):
            raise errors.fail(errors.E_SCHEMA,
                              "edit rectangle coordinates must be integers",
                              index=index)
        amount = item.get("amount", 1)
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            raise errors.fail(errors.E_SCHEMA,
                              "edit amount must be a non-negative integer",
                              index=index)
        edits.append(Edit(int(batch), index, op,
                          int(coords[0]), int(coords[1]),
                          int(coords[2]), int(coords[3]), int(amount)))
    edits.sort(key=lambda edit: (edit.batch, edit.seq))
    return edits


def rectangles_intersect(rect_a: Tuple[int, int, int, int],
                         rect_b: Tuple[int, int, int, int]) -> bool:
    a0, b0, a1, b1 = rect_a
    c0, d0, c1, d1 = rect_b
    return a0 <= c1 and c0 <= a1 and b0 <= d1 and d0 <= b1


def validate_edit_bounds(edit: Edit, terrain: Terrain) -> None:
    """Raise ``E_BOUNDS`` for malformed or out-of-grid edit rectangles."""
    if not (edit.x0 < edit.x1 and edit.y0 < edit.y1):
        raise errors.fail(
            errors.E_BOUNDS,
            "edit rectangle must be non-empty and half-open",
            edit=edit.seq, rect=edit.as_rect())
    if edit.x0 < 0 or edit.y0 < 0 \
            or edit.x1 > terrain.rows_nx or edit.y1 > terrain.rows_ny:
        raise errors.fail(
            errors.E_BOUNDS,
            "edit rectangle exceeds terrain grid bounds",
            edit=edit.seq, rect=edit.as_rect(),
            grid=(terrain.rows_nx, terrain.rows_ny))


def apply_edit(terrain: Terrain, edit: Edit) -> None:
    """Mutate the height field in place using global grid indices."""
    validate_edit_bounds(edit, terrain)
    target = terrain.heights[edit.y0:edit.y1, edit.x0:edit.x1]
    if edit.op == "raise":
        target += edit.amount
    elif edit.op == "lower":
        target -= edit.amount
    else:  # smooth: integer 3x3 box average, borders use available neighbours
        heights = terrain.heights
        for row in range(edit.y0, edit.y1):
            for column in range(edit.x0, edit.x1):
                r0, r1 = max(0, row - 1), min(heights.shape[0], row + 2)
                c0, c1 = max(0, column - 1), min(heights.shape[1], column + 2)
                neighbourhood = heights[r0:r1, c0:c1].astype(np.float64)
                target[row - edit.y0, column - edit.x0] = int(
                    np.rint(neighbourhood.mean()))


def apply_batches(terrain: Terrain, edits: List[Edit],
                  up_to_batch: int) -> List[Dict[str, Any]]:
    """Apply every edit whose batch is ``<= up_to_batch`` in order."""
    applied: List[Dict[str, Any]] = []
    for edit in edits:
        if edit.batch > up_to_batch:
            continue
        apply_edit(terrain, edit)
        applied.append({
            "seq": edit.seq, "batch": edit.batch, "op": edit.op,
            "rect": edit.as_rect(), "amount": edit.amount,
        })
    return applied


def invalidate_for_edit(edit: Edit,
                        tile_dependencies: Dict[str, Dict[str, int]]
                        ) -> List[Dict[str, Any]]:
    """Return per-tile invalidation reasons for one edit rectangle."""
    rect = edit.as_rect()
    reasons: List[Dict[str, Any]] = []
    for tile_id in sorted(tile_dependencies):
        dep = tile_dependencies[tile_id]
        dep_rect = (dep["min_i"], dep["min_j"],
                    dep["max_i"], dep["max_j"])
        if rectangles_intersect(rect, dep_rect):
            reasons.append({
                "tile": tile_id,
                "reason": "edit rectangle intersects tile dependency range",
                "edit_seq": edit.seq,
                "edit_batch": edit.batch,
                "edit_rect": list(rect),
                "dependency_rect": list(dep_rect),
            })
    return reasons

