"""Deterministic node implementations and blend consistency checks."""

from __future__ import annotations

from collections import deque
from typing import Any, Mapping

import numpy as np

from .config import PRECISION_DTYPES
from .errors import BusinessError, ErrorCode
from .hashing import first_difference


def mask_crop(mask: np.ndarray, bbox: tuple[int, int, int, int]) -> np.ndarray:
    y0, x0, y1, x1 = bbox
    return np.ascontiguousarray(mask[y0:y1, x0:x1], dtype=np.uint8)


def context_sample(
    base: np.ndarray,
    bbox: tuple[int, int, int, int],
) -> np.ndarray:
    y0, x0, y1, x1 = bbox
    return np.ascontiguousarray(base[y0:y1, x0:x1], dtype=np.float64)


def _work_dtype(precision: str) -> np.dtype[Any]:
    try:
        return np.dtype(PRECISION_DTYPES[precision])
    except KeyError as exc:
        raise BusinessError(
            ErrorCode.UNKNOWN_PRECISION,
            "unknown precision tier",
            field="style.precision",
            value=precision,
        ) from exc


def _seed_inside_from_boundary(
    color: np.ndarray,
    inside: np.ndarray,
) -> np.ndarray:
    """Copy nearest outside colors inward using deterministic 4-neighbour BFS."""

    height, width = inside.shape
    filled = ~inside
    result = color.copy()
    queue: deque[tuple[int, int]] = deque()

    seeds: list[tuple[int, int]] = []
    for row in range(height):
        for col in range(width):
            if not inside[row, col]:
                continue
            neighbours = []
            if row > 0 and not inside[row - 1, col]:
                neighbours.append(color[row - 1, col])
            if col > 0 and not inside[row, col - 1]:
                neighbours.append(color[row, col - 1])
            if row + 1 < height and not inside[row + 1, col]:
                neighbours.append(color[row + 1, col])
            if col + 1 < width and not inside[row, col + 1]:
                neighbours.append(color[row, col + 1])
            if neighbours:
                result[row, col] = np.mean(np.stack(neighbours), axis=0)
                filled[row, col] = True
                seeds.append((row, col))

    queue.extend(seeds)
    while queue:
        row, col = queue.popleft()
        value = result[row, col]
        for next_row, next_col in (
            (row - 1, col),
            (row, col - 1),
            (row + 1, col),
            (row, col + 1),
        ):
            if (
                0 <= next_row < height
                and 0 <= next_col < width
                and inside[next_row, next_col]
                and not filled[next_row, next_col]
            ):
                result[next_row, next_col] = value
                filled[next_row, next_col] = True
                queue.append((next_row, next_col))
    return result


def content_completion(
    context: np.ndarray,
    region_mask: np.ndarray,
    iterations: int,
    precision: str,
) -> np.ndarray:
    """Deterministically complete a masked region from boundary context."""

    if context.shape[:2] != region_mask.shape:
        raise BusinessError(
            ErrorCode.REGION_NODE_FAILURE,
            "context and region mask shapes differ",
            node="content_completion",
            field="mask.shape",
            context_shape=list(context.shape[:2]),
            mask_shape=list(region_mask.shape),
        )
    dtype = _work_dtype(precision)
    source = np.asarray(context, dtype=dtype)
    inside = np.asarray(region_mask, dtype=np.bool_)
    if not np.any(inside):
        return np.ascontiguousarray(source)
    if not np.any(~inside):
        mean = np.mean(source.reshape(-1, 3), axis=0)
        result = np.empty_like(source)
        result[:] = mean.astype(dtype)
    else:
        result = _seed_inside_from_boundary(source, inside)

    for _ in range(iterations):
        padded = np.pad(result, ((1, 1), (1, 1), (0, 0)), mode="edge")
        neighbour_mean = (
            padded[:-2, 1:-1, :]
            + padded[2:, 1:-1, :]
            + padded[1:-1, :-2, :]
            + padded[1:-1, 2:, :]
        ) / dtype.type(4.0)
        updated = result.copy()
        updated[inside] = (
            dtype.type(0.55) * result[inside]
            + dtype.type(0.45) * neighbour_mean[inside]
        )
        result = updated

    yy, xx = np.indices(inside.shape, dtype=dtype)
    phase = yy * dtype.type(0.173) + xx * dtype.type(0.119)
    texture = np.stack(
        (
            np.sin(phase) * dtype.type(1.1),
            np.sin(phase + dtype.type(0.8)) * dtype.type(0.9),
            np.sin(phase + dtype.type(1.7)) * dtype.type(1.0),
        ),
        axis=-1,
    ).astype(dtype)
    result[inside] = result[inside] + texture[inside]
    result[~inside] = source[~inside]
    return np.ascontiguousarray(result, dtype=dtype)


def style_transfer(
    inpainted: np.ndarray,
    region_mask: np.ndarray,
    style: Mapping[str, Any],
    precision: str,
) -> np.ndarray:
    """Apply a colour matrix and edge-preserving deterministic smoothing."""

    dtype = _work_dtype(precision)
    source = np.asarray(inpainted, dtype=dtype)
    inside = np.asarray(region_mask, dtype=np.bool_)
    strength = dtype.type(style["style_strength"])
    if float(strength) == 0.0:
        return np.ascontiguousarray(source)

    matrix = np.asarray(style["color_matrix"], dtype=dtype)
    colored = source + strength * (source @ matrix.T - source)
    edge_preserve = dtype.type(style["edge_preserve"])
    sigma = dtype.type(6.0) + dtype.type(30.0) * (dtype.type(1.0) - edge_preserve)
    denominator = dtype.type(2.0) * sigma * sigma

    for _ in range(int(style["iterations"])):
        padded = np.pad(colored, ((1, 1), (1, 1), (0, 0)), mode="edge")
        neighbours = (
            padded[:-2, 1:-1, :],
            padded[2:, 1:-1, :],
            padded[1:-1, :-2, :],
            padded[1:-1, 2:, :],
        )
        weighted_sum = np.zeros_like(colored)
        weight_total = np.zeros(colored.shape[:2], dtype=dtype)
        for neighbour in neighbours:
            delta = neighbour - colored
            delta_sq = np.sum(delta * delta, axis=2, dtype=dtype)
            weight = np.exp(-delta_sq / denominator)
            weighted_sum += neighbour * weight[..., None]
            weight_total += weight
        colored = (colored + weighted_sum) / (dtype.type(1.0) + weight_total[..., None])

    colored[~inside] = source[~inside]
    return np.ascontiguousarray(colored, dtype=dtype)


def feather_weights(mask: np.ndarray, feather_px: int) -> np.ndarray:
    """Inward-only feather weights; exactly zero outside the region mask."""

    binary = np.asarray(mask, dtype=np.bool_)
    if feather_px <= 0:
        return binary.astype(np.float64)
    height, width = binary.shape
    distance = np.zeros((height, width), dtype=np.int32)
    queue: deque[tuple[int, int]] = deque()

    for row in range(height):
        for col in range(width):
            if not binary[row, col]:
                continue
            has_outside = (
                row == 0
                or col == 0
                or row + 1 == height
                or col + 1 == width
                or not binary[row - 1, col]
                or not binary[row, col - 1]
                or not binary[row + 1, col]
                or not binary[row, col + 1]
            )
            if has_outside:
                distance[row, col] = 1
                queue.append((row, col))

    while queue:
        row, col = queue.popleft()
        next_distance = distance[row, col] + 1
        for next_row, next_col in (
            (row - 1, col),
            (row, col - 1),
            (row + 1, col),
            (row, col + 1),
        ):
            if (
                0 <= next_row < height
                and 0 <= next_col < width
                and binary[next_row, next_col]
                and distance[next_row, next_col] == 0
            ):
                distance[next_row, next_col] = next_distance
                queue.append((next_row, next_col))

    weights = np.zeros((height, width), dtype=np.float64)
    weights[binary] = np.clip(
        distance[binary].astype(np.float64) / float(feather_px),
        0.0,
        1.0,
    )
    return weights


def blend_region(
    styled_context: np.ndarray,
    mask_context: np.ndarray,
    region_bbox: tuple[int, int, int, int],
    context_bbox: tuple[int, int, int, int],
    feather_px: int,
    precision: str,
) -> np.ndarray:
    """Return an RGBA region artifact where A is the explicit feather weight."""

    dtype = _work_dtype(precision)
    ry0, rx0, ry1, rx1 = region_bbox
    cy0, cx0, _, _ = context_bbox
    local_y0 = ry0 - cy0
    local_x0 = rx0 - cx0
    local_y1 = local_y0 + (ry1 - ry0)
    local_x1 = local_x0 + (rx1 - rx0)

    styled = np.asarray(styled_context[local_y0:local_y1, local_x0:local_x1], dtype=dtype)
    mask = np.asarray(mask_context[local_y0:local_y1, local_x0:local_x1], dtype=np.bool_)
    weight = feather_weights(mask, feather_px).astype(dtype)

    outside = np.logical_and(~mask, weight != 0)
    if np.any(outside):
        row, col = (int(v) for v in np.argwhere(outside)[0])
        raise BusinessError(
            ErrorCode.BLEND_MISMATCH,
            "a non-zero feather weight crossed outside its region mask",
            coordinate=[row, col],
            region_id=None,
        )
    if float(np.max(weight, initial=0.0)) > 1.0:
        row, col = (int(v) for v in np.argwhere(weight > 1.0)[0])
        raise BusinessError(
            ErrorCode.BLEND_MISMATCH,
            "feather weight exceeds one",
            coordinate=[row, col],
            region_id=None,
        )

    artifact = np.empty((styled.shape[0], styled.shape[1], 4), dtype=dtype)
    artifact[..., :3] = styled
    artifact[..., 3] = weight
    return np.ascontiguousarray(artifact)


def _blend_array(spec: Mapping[str, Any]) -> np.ndarray:
    array = np.asarray(spec["array"])
    if array.ndim != 3 or array.shape[2] != 4:
        raise BusinessError(
            ErrorCode.BLEND_MISMATCH,
            "blend artifact must be HxWx4",
            region_id=spec.get("region_id"),
            shape=list(array.shape),
        )
    return array


def compose_regionwise(
    base: np.ndarray,
    blends: Mapping[int, Mapping[str, Any]],
) -> np.ndarray:
    """Compose cached region artifacts one by one onto the base image."""

    canvas = np.asarray(base, dtype=np.float64).copy()
    for region_id in sorted(blends):
        spec = blends[region_id]
        artifact = _blend_array(spec)
        y0, x0, y1, x1 = (int(v) for v in spec["bbox"])
        weight = artifact[..., 3:4].astype(np.float64)
        color = artifact[..., :3].astype(np.float64)
        canvas[y0:y1, x0:x1] = (
            canvas[y0:y1, x0:x1] * (1.0 - weight) + color * weight
        )
    return _quantize(canvas)


def compose_global(
    base: np.ndarray,
    blends: Mapping[int, Mapping[str, Any]],
) -> tuple[np.ndarray, dict[str, Any]]:
    """Perform one whole-canvas blend from all region artifacts."""

    height, width = base.shape[:2]
    total_weight = np.zeros((height, width), dtype=np.float64)
    color_sum = np.zeros((height, width, 3), dtype=np.float64)
    owner = np.full((height, width), -1, dtype=np.int64)
    max_weight = 0.0

    for region_id in sorted(blends):
        spec = blends[region_id]
        artifact = _blend_array(spec)
        y0, x0, y1, x1 = (int(v) for v in spec["bbox"])
        weight = artifact[..., 3].astype(np.float64)
        color = artifact[..., :3].astype(np.float64)
        total_weight[y0:y1, x0:x1] += weight
        color_sum[y0:y1, x0:x1] += color * weight[..., None]
        owner[y0:y1, x0:x1] = np.where(weight > 0.0, region_id, owner[y0:y1, x0:x1])
        max_weight = max(max_weight, float(np.max(weight, initial=0.0)))

    over = np.argwhere(total_weight > 1.0 + 1e-12)
    if len(over):
        row, col = (int(v) for v in over[0])
        raise BusinessError(
            ErrorCode.BLEND_MISMATCH,
            "sum of region feather weights exceeds one",
            coordinate=[row, col],
            region_id=int(owner[row, col]) if owner[row, col] >= 0 else None,
            weight_sum=float(total_weight[row, col]),
        )

    canvas = (
        np.asarray(base, dtype=np.float64) * (1.0 - total_weight[..., None])
        + color_sum
    )
    max_total_weight = float(np.max(total_weight, initial=0.0))
    return _quantize(canvas), {
        "max_region_weight": max_weight,
        "max_total_weight": max_total_weight,
        "min_base_weight": 1.0 - max_total_weight,
        "per_pixel_weight_sum_ok": True,
        "weight_rule": "region_weight + (1 - region_weight) == 1",
        "outside_mask_weight_zero": True,
    }


def _quantize(canvas: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(canvas), 0, 255).astype(np.uint8)


def region_for_pixel(
    masks: Mapping[int, np.ndarray],
    row: int,
    col: int,
) -> int | None:
    for region_id in sorted(masks):
        mask = masks[region_id]
        if 0 <= row < mask.shape[0] and 0 <= col < mask.shape[1] and mask[row, col]:
            return int(region_id)
    return None


def assert_blend_consistency(
    regionwise: np.ndarray,
    global_blend: np.ndarray,
    masks: Mapping[int, np.ndarray],
) -> dict[str, Any]:
    difference = first_difference(regionwise, global_blend)
    if difference is not None:
        row, col, channel, left, right = difference
        raise BusinessError(
            ErrorCode.BLEND_MISMATCH,
            "regionwise blend differs from one-pass global blend",
            coordinate=[row, col],
            x=col,
            y=row,
            channel=channel,
            region_id=region_for_pixel(masks, row, col),
            regionwise_value=left,
            global_value=right,
        )
    return {"ok": True, "first_difference": None}


def blend_deviation_spectrum(
    incremental: np.ndarray,
    full: np.ndarray,
    masks: Mapping[int, np.ndarray],
    blends: Mapping[int, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Per-region difference spectrum between incremental and full output."""

    spectrum: list[dict[str, Any]] = []
    for region_id in sorted(masks):
        mask = masks[region_id]
        if region_id in blends:
            y0, x0, y1, x1 = (int(v) for v in blends[region_id]["bbox"])
            artifact = _blend_array(blends[region_id])
            weight = artifact[..., 3]
            band = np.logical_and(weight > 0.0, weight < 1.0)
        else:
            y0, x0, y1, x1 = (int(v) for v in _bbox(mask))
            band = np.zeros_like(mask, dtype=np.bool_)
        left = incremental[y0:y1, x0:x1].astype(np.int16)
        right = full[y0:y1, x0:x1].astype(np.int16)
        delta = np.abs(left - right)
        per_pixel = np.max(delta, axis=2) if delta.ndim == 3 else delta
        bins = [
            int(np.count_nonzero(per_pixel == 0)),
            int(np.count_nonzero(per_pixel == 1)),
            int(np.count_nonzero((per_pixel >= 2) & (per_pixel <= 3))),
            int(np.count_nonzero((per_pixel >= 4) & (per_pixel <= 7))),
            int(np.count_nonzero((per_pixel >= 8) & (per_pixel <= 15))),
            int(np.count_nonzero((per_pixel >= 16) & (per_pixel <= 31))),
            int(np.count_nonzero((per_pixel >= 32) & (per_pixel <= 63))),
            int(np.count_nonzero(per_pixel >= 64)),
        ]
        spectrum.append(
            {
                "region_id": region_id,
                "bbox": [y0, x0, y1, x1],
                "mask_pixels": int(np.count_nonzero(mask)),
                "blend_band_pixels": int(np.count_nonzero(band)),
                "differing_pixels": int(np.count_nonzero(per_pixel)),
                "max_abs_channel_delta": int(np.max(delta, initial=0)),
                "delta_histogram_0_1_2_3_4_7_8_15_16_31_32_63_64plus": bins,
            }
        )
    return spectrum


def _bbox(mask: np.ndarray) -> tuple[int, int, int, int]:
    rows = np.flatnonzero(np.any(mask, axis=1))
    cols = np.flatnonzero(np.any(mask, axis=0))
    if not len(rows) or not len(cols):
        return (0, 0, 0, 0)
    return (int(rows[0]), int(cols[0]), int(rows[-1] + 1), int(cols[-1] + 1))
