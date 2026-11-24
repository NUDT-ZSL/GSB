from __future__ import annotations

from collections import deque
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .config import TaskConfig
from .geometry import Box, Chunk, placement_slices
from .io_utils import array_digest, canonical_digest


NEIGHBORS_6 = (
    (-1, 0, 0),
    (1, 0, 0),
    (0, -1, 0),
    (0, 1, 0),
    (0, 0, -1),
    (0, 0, 1),
)


def aligned_seed_coordinates(
    box: Box, stride: Sequence[int]
) -> List[Tuple[int, int, int]]:
    """Return global seed lattice coordinates contained in ``box``."""
    coords: List[Tuple[int, int, int]] = []
    starts = []
    stops = []
    for low, high, step in (
        (box.z0, box.z1, stride[0]),
        (box.y0, box.y1, stride[1]),
        (box.x0, box.x1, stride[2]),
    ):
        first = ((low + step - 1) // step) * step
        starts.append(first)
        stops.append(high)
    for z in range(starts[0], stops[0], stride[0]):
        for y in range(starts[1], stops[1], stride[1]):
            for x in range(starts[2], stops[2], stride[2]):
                coords.append((z, y, x))
    return coords


def region_grow(
    volume: np.ndarray,
    threshold: Tuple[float, float],
    seed_stride: Sequence[int],
    growth_radius: int,
    target_box: Optional[Box] = None,
) -> np.ndarray:
    """Grow from deterministic global-coordinate seed candidates.

    When ``target_box`` is supplied, the returned array covers that box.
    A context halo of ``growth_radius`` voxels is read from the full global
    volume so boundary neighborhoods use true across-chunk values.
    """
    full_box = Box.from_shape(volume.shape)
    target = target_box or full_box
    context = target.expand(int(growth_radius), full_box)
    values = volume[
        context.z0 : context.z1,
        context.y0 : context.y1,
        context.x0 : context.x1,
    ]
    low, high = threshold
    qualified = (values >= low) & (values <= high)
    distances = np.full(values.shape, -1, dtype=np.int32)
    queue: deque[Tuple[int, int, int, int]] = deque()

    for gz, gy, gx in aligned_seed_coordinates(context, seed_stride):
        lz, ly, lx = gz - context.z0, gy - context.y0, gx - context.x0
        if qualified[lz, ly, lx] and distances[lz, ly, lx] < 0:
            distances[lz, ly, lx] = 0
            queue.append((lz, ly, lx, 0))

    while queue:
        z, y, x, distance = queue.popleft()
        if growth_radius >= 0 and distance >= growth_radius:
            continue
        next_distance = distance + 1
        for dz, dy, dx in NEIGHBORS_6:
            nz, ny, nx = z + dz, y + dy, x + dx
            if not (0 <= nz < context.z1 - context.z0):
                continue
            if not (0 <= ny < context.y1 - context.y0):
                continue
            if not (0 <= nx < context.x1 - context.x0):
                continue
            if distances[nz, ny, nx] >= 0 or not qualified[nz, ny, nx]:
                continue
            distances[nz, ny, nx] = next_distance
            queue.append((nz, ny, nx, next_distance))

    grown = distances >= 0
    if target == context:
        return grown
    slices = placement_slices(target, context)
    return grown[slices]


def binary_morphology(
    mask: np.ndarray, op: str, kernel: int, iterations: int = 1
) -> np.ndarray:
    current = mask.astype(bool, copy=True)
    radius = kernel // 2
    for _ in range(iterations):
        pad_value = False if op == "dilate" else True
        padded = np.pad(current, radius, mode="constant", constant_values=pad_value)
        if op == "dilate":
            result = np.zeros_like(current, dtype=bool)
        else:
            result = np.ones_like(current, dtype=bool)
        dz_size, dy_size, dx_size = current.shape
        for dz in range(-radius, radius + 1):
            z_sl = slice(radius + dz, radius + dz + dz_size)
            for dy in range(-radius, radius + 1):
                y_sl = slice(radius + dy, radius + dy + dy_size)
                for dx in range(-radius, radius + 1):
                    x_sl = slice(radius + dx, radius + dx + dx_size)
                    shifted = padded[z_sl, y_sl, x_sl]
                    if op == "dilate":
                        result |= shifted
                    else:
                        result &= shifted
        current = result
    return current


def apply_morphology(mask: np.ndarray, cfg: TaskConfig) -> np.ndarray:
    result = mask.astype(bool, copy=True)
    for operation in cfg.morph_operations:
        result = binary_morphology(
            result, operation.op, operation.kernel, operation.iterations
        )
    return result


def dependency_boxes(cfg: TaskConfig, chunk: Chunk) -> Tuple[Box, Box]:
    full = Box.from_shape(cfg.shape)
    raw_box = chunk.core.expand(cfg.morph_radius, full)
    data_box = chunk.core.expand(cfg.growth_radius + cfg.morph_radius, full)
    return raw_box, data_box


def buffer_slot_estimate(cfg: TaskConfig, chunk: Chunk) -> int:
    raw_box, data_box = dependency_boxes(cfg, chunk)
    return int(2 * data_box.numel + 3 * raw_box.numel + chunk.core.numel)


def logical_cost_ms(cfg: TaskConfig, chunk: Chunk) -> float:
    slots = buffer_slot_estimate(cfg, chunk)
    return max(1.0, round(slots / 4096.0, 3))


def segment_chunk_core(
    volume: np.ndarray,
    cfg: TaskConfig,
    chunk: Chunk,
    raw_edits: Optional[Tuple[np.ndarray, np.ndarray]] = None,
) -> Dict[str, np.ndarray | Box]:
    """Compute one core chunk using global-coordinate truth and halos."""
    raw_box, data_box = dependency_boxes(cfg, chunk)
    raw_padded = region_grow(
        volume,
        cfg.threshold,
        cfg.seed_stride,
        cfg.growth_radius,
        target_box=raw_box,
    )
    if raw_edits is not None:
        edit_mask, edit_values = raw_edits
        if edit_mask.shape != raw_padded.shape or edit_values.shape != raw_padded.shape:
            raise ValueError("raw edits must align to raw_box")
        raw_padded = raw_padded.copy()
        raw_padded[edit_mask] = edit_values[edit_mask].astype(bool)

    morphed_padded = apply_morphology(raw_padded, cfg)
    core_in_raw = placement_slices(chunk.core, raw_box)
    final_core = morphed_padded[core_in_raw].astype(np.uint8)
    raw_core = raw_padded[core_in_raw].astype(np.uint8)
    return {
        "mask": final_core,
        "raw": raw_core,
        "raw_box": raw_box,
        "data_box": data_box,
    }


def segment_full(
    volume: np.ndarray,
    cfg: TaskConfig,
    raw_override_global: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    raw = region_grow(
        volume,
        cfg.threshold,
        cfg.seed_stride,
        cfg.growth_radius,
        target_box=Box.from_shape(cfg.shape),
    )
    if raw_override_global is not None:
        if raw_override_global.shape != raw.shape:
            raise ValueError("global raw override has wrong shape")
        raw = raw_override_global.astype(bool, copy=True)
    mask = apply_morphology(raw, cfg).astype(np.uint8)
    return raw.astype(np.uint8), mask


def chunk_array_digest(
    chunk_id: str,
    core_box: Box,
    mask: np.ndarray,
    raw: np.ndarray,
    dependency: Optional[Dict[str, Any]] = None,
) -> str:
    return canonical_digest(
        {
            "chunk_id": chunk_id,
            "core_box": core_box.to_tuple(),
            "mask_digest": array_digest(mask),
            "raw_digest": array_digest(raw),
            "dependency_digest": canonical_digest(dependency) if dependency is not None else None,
        }
    )


def build_dependency_record(
    cfg: TaskConfig,
    chunk: Chunk,
    chunks: Sequence[Chunk],
    volume: np.ndarray,
    version_id: str,
) -> Dict[str, Any]:
    raw_box, data_box = dependency_boxes(cfg, chunk)
    data_values = volume[
        data_box.z0 : data_box.z1,
        data_box.y0 : data_box.y1,
        data_box.x0 : data_box.x1,
    ]
    neighbors: List[Dict[str, Any]] = []
    volume_providers: List[str] = []
    for other in chunks:
        if other.id == chunk.id:
            continue
        if raw_box.intersects(other.core):
            overlap = raw_box.intersection(other.core)
            neighbors.append(
                {
                    "chunk": other.id,
                    "kind": "raw_boundary_padding",
                    "range": overlap.to_tuple(),
                    "voxels": overlap.numel,
                }
            )
        if data_box.intersects(other.core):
            volume_providers.append(other.id)
    return {
        "chunk": chunk.id,
        "index": list(chunk.index),
        "core_box": chunk.core.to_tuple(),
        "raw_box": raw_box.to_tuple(),
        "data_box": data_box.to_tuple(),
        "data_digest": array_digest(data_values),
        "volume_provider_chunks": volume_providers,
        "neighbor_padding": neighbors,
        "growth": {
            "threshold": list(cfg.threshold),
            "seed_stride": list(cfg.seed_stride),
            "growth_radius": cfg.growth_radius,
        },
        "morphology": [op.to_dict() for op in cfg.morph_operations],
        "morph_radius": cfg.morph_radius,
        "version_id": version_id,
    }


def rasterize_revision_edits(
    cfg: TaskConfig,
    target_box: Box,
    revisions: Sequence[Any],
) -> Tuple[np.ndarray, np.ndarray]:
    """Rasterize core-aligned revisions into target-box mask and values."""
    edit_mask = np.zeros(target_box.shape, dtype=bool)
    edit_values = np.zeros(target_box.shape, dtype=np.uint8)
    for revision in revisions:
        core = revision.core_box if hasattr(revision, "core_box") else revision["core_box"]
        rev_mask = revision.edit_mask if hasattr(revision, "edit_mask") else revision["edit_mask"]
        values = revision.values if hasattr(revision, "values") else revision["values"]
        core_box = Box(*core) if isinstance(core, (list, tuple)) else core
        if not target_box.intersects(core_box):
            continue
        intersection = target_box.intersection(core_box)
        target_slices = placement_slices(intersection, target_box)
        core_slices = placement_slices(intersection, core_box)
        selected = rev_mask[core_slices].astype(bool)
        target_edit_region = edit_mask[target_slices]
        target_value_region = edit_values[target_slices]
        target_edit_region[selected] = True
        target_value_region[selected] = values[core_slices][selected]
    return edit_mask, edit_values
