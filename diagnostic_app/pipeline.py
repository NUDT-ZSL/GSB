from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import precision_dtype, validate_task
from .errors import AppError, ErrorCode
from .hashing import array_digest, sha256_json
from .vocab import condition_vector


PIPELINE_SEED = 0xC04D17


@dataclass
class GenerationContext:
    task: dict[str, Any]
    config: dict[str, Any]
    dtype: np.dtype
    shape: tuple[int, int, int]
    condition: np.ndarray
    condition_record: dict[str, Any]
    condition_field: np.ndarray
    image_pixels: np.ndarray
    initial_field: np.ndarray
    fusion_resolved: dict[str, Any]
    kernel: np.ndarray
    anchor_field: np.ndarray | None
    anchor_info: dict[str, Any]


def resolve_fusion(config: dict[str, Any]) -> dict[str, Any]:
    fusion = config["fusion"]
    image_weight = float(fusion["image_weight"])
    condition_weight = float(fusion["condition_weight"])
    total = image_weight + condition_weight
    if not math.isfinite(total) or total <= 0.0:
        raise AppError(ErrorCode.INVALID_REQUEST, "fusion weights must have a positive finite sum")
    resolved = {
        "raw_image_weight": image_weight,
        "raw_condition_weight": condition_weight,
        "image_weight": image_weight / total,
        "condition_weight": condition_weight / total,
        "bias": float(fusion.get("bias", 0.0)),
        "condition_gain": float(fusion.get("condition_gain", 1.0)),
        "normalization": fusion.get("normalization", "sum_to_one_v1"),
    }
    resolved["checksum"] = sha256_json({k: v for k, v in resolved.items() if k != "checksum"})
    return resolved


def resolve_kernel(config: dict[str, Any], dtype: np.dtype) -> tuple[np.ndarray, dict[str, Any]]:
    iteration = config["iteration"]
    radius = int(iteration.get("coupling_radius", 1))
    if radius != 1:
        raise AppError(ErrorCode.INVALID_REQUEST, "this deterministic build supports coupling_radius=1")
    strength = float(iteration["coupling_strength"])
    if not 0.0 <= strength <= 1.0:
        raise AppError(ErrorCode.INVALID_REQUEST, "coupling_strength must be in [0, 1]")
    neighbor = strength / 4.0
    center = 1.0 - strength
    kernel = np.zeros((3, 3), dtype=np.float64)
    kernel[1, 1] = center
    kernel[0, 1] = neighbor
    kernel[2, 1] = neighbor
    kernel[1, 0] = neighbor
    kernel[1, 2] = neighbor
    record = {
        "radius": radius,
        "strength": strength,
        "center_weight": center,
        "neighbor_weight_each": neighbor,
        "normalized_sum": float(kernel.sum()),
        "boundary": iteration.get("boundary", "edge_clamp"),
        "update_order": iteration.get("update_order", "row_major_jacobi_double_buffer"),
        "checksum": sha256_json(kernel.tolist()),
    }
    return kernel.astype(dtype), record


def build_condition_field(condition: np.ndarray, shape: tuple[int, int, int],
                          config: dict[str, Any], dtype: np.dtype) -> np.ndarray:
    height, width, channels = shape
    dim = int(config["condition"]["dim"])
    rng = np.random.default_rng(PIPELINE_SEED)
    projection = rng.normal(0.0, 0.5, size=(dim, channels, 6)).astype(np.float64)

    x_values = np.linspace(-1.0, 1.0, width, dtype=np.float64)
    y_values = np.linspace(-1.0, 1.0, height, dtype=np.float64)
    x_grid, y_grid = np.meshgrid(x_values, y_values, indexing="xy")
    basis = np.stack([
        np.ones_like(x_grid),
        x_grid,
        y_grid,
        x_grid * y_grid,
        x_grid * x_grid - y_grid * y_grid,
        np.sin(math.pi * x_grid),
    ], axis=-1)
    coefficients = np.einsum("d,dck->ck", condition.astype(np.float64), projection, optimize=False)
    field = np.einsum("ck,hwk->hwc", coefficients, basis, optimize=False)
    return np.tanh(field).astype(dtype)


def build_image_pixels(task: dict[str, Any], dtype: np.dtype) -> np.ndarray:
    height, width, channels = tuple(task["image_shape"])
    seed = int(task["image_seed"])
    rng = np.random.default_rng(seed)
    noise_scale = float(task["noise_scale"])

    x_values = np.linspace(-1.0, 1.0, width, dtype=np.float64)
    y_values = np.linspace(-1.0, 1.0, height, dtype=np.float64)
    x_grid, y_grid = np.meshgrid(x_values, y_values, indexing="xy")
    channel_phase = np.linspace(0.0, math.pi / 2.0, channels)
    base = np.empty((height, width, channels), dtype=np.float64)
    for channel in range(channels):
        base[:, :, channel] = (
            0.52
            + 0.16 * np.sin(math.pi * x_grid + channel_phase[channel])
            + 0.12 * y_grid
            + 0.06 * x_grid * y_grid
        )
    noise = rng.normal(0.0, noise_scale, size=(height, width, channels))
    return np.clip(base + noise, 0.0, 1.0).astype(dtype)


def coerce_reference(reference: Any, expected_shape: tuple[int, int, int]) -> np.ndarray:
    if isinstance(reference, (str,)):
        raise AppError(ErrorCode.INVALID_REQUEST, "reference paths must be loaded before coercion")
    array = np.asarray(reference)
    if array.shape != expected_shape:
        raise AppError(
            ErrorCode.REFERENCE_SHAPE,
            "reference image shape does not match generation canvas",
            {"expected": list(expected_shape), "actual": list(array.shape)},
        )
    if np.issubdtype(array.dtype, np.integer):
        array = array.astype(np.float64) / 255.0
    else:
        array = array.astype(np.float64)
        if array.size and float(np.nanmax(array)) > 1.5:
            array = array / 255.0
    if array.shape[2] == 1 and expected_shape[2] == 3:
        array = np.repeat(array, 3, axis=2)
    return np.clip(array, 0.0, 1.0)


def validate_anchor_request(strength: Any, strength_config: dict[str, Any],
                            reference: Any, expected_shape: tuple[int, int, int]) -> float | None:
    if strength is None:
        strength = strength_config.get("default_strength", 0.0) if reference is not None else 0.0
    try:
        strength = float(strength)
    except (TypeError, ValueError):
        raise AppError(ErrorCode.ANCHOR_STRENGTH, "anchor strength must be numeric",
                       {"value": strength})
    minimum = float(strength_config.get("strength_min", 0.0))
    maximum = float(strength_config.get("strength_max", 1.0))
    if not math.isfinite(strength) or strength < minimum or strength > maximum:
        raise AppError(
            ErrorCode.ANCHOR_STRENGTH,
            f"anchor_strength must be in [{minimum}, {maximum}]",
            {"value": strength, "minimum": minimum, "maximum": maximum},
        )
    if reference is not None:
        # Shape validation is performed by coercion; callers that only want
        # strength validation can pass reference=None.
        coerce_reference(reference, expected_shape)
    return strength


def build_anchor_field(reference: np.ndarray, dtype: np.dtype) -> tuple[np.ndarray, dict[str, Any]]:
    ref01 = coerce_reference(reference, reference.shape if hasattr(reference, "shape") else None)  # type: ignore[arg-type]
    field = (2.0 * ref01 - 1.0).astype(np.float64)
    channels = field.shape[2]
    mean = field.mean(axis=(0, 1)).reshape(1, 1, channels)
    std = field.std(axis=(0, 1)).reshape(1, 1, channels)
    normalized = (field - mean) / (std + 1e-6)
    # A fixed statistic target: reference channel mean plus bounded standardized
    # deviations. Blending it with the pixels makes the regression toward the
    # reference's first/second-order statistics explicit and block-safe.
    statistic_field = mean + 0.55 * np.tanh(normalized)
    target_float = 0.72 * field + 0.28 * statistic_field
    target = np.clip(target_float, -1.0, 1.0).astype(dtype)
    info = {
        "mode": "reference_pixel_and_channel_statistics_v1",
        "pixel_weight": 0.72,
        "statistic_weight": 0.28,
        "channel_mean": [float(x) for x in mean.reshape(-1)],
        "channel_std": [float(x) for x in std.reshape(-1)],
        "reference_checksum": array_digest(target),
    }
    return target, info


def build_context(task: dict[str, Any], config: dict[str, Any],
                  reference: np.ndarray | None = None,
                  anchor_strength: float | None = None,
                  condition_override: np.ndarray | None = None) -> GenerationContext:
    validate_task(task)
    dtype = np.dtype(precision_dtype(task["precision_profile"]))
    shape = tuple(int(v) for v in task["image_shape"])  # type: ignore[assignment]
    fusion_resolved = resolve_fusion(config)
    kernel, kernel_record = resolve_kernel(config, dtype)
    fusion_resolved["kernel"] = kernel_record

    if condition_override is not None:
        condition = np.asarray(condition_override, dtype=np.float64)
        condition_record = {
            "source": "independent_condition_vector_override",
            "dim": int(condition.size),
            "vector_checksum": sha256_json([round(float(v), 12) for v in condition]),
        }
    else:
        condition, condition_record = condition_vector(
            str(task["text"]), str(task.get("prompt", "")), config["condition"]
        )

    expected_dim = int(config["condition"]["dim"])
    if condition.size != expected_dim:
        raise AppError(ErrorCode.INVALID_REQUEST, "condition vector dimension does not match config")

    condition_field = build_condition_field(condition, shape, config, dtype)
    image_pixels = build_image_pixels(task, dtype)
    initial_field = (
        fusion_resolved["image_weight"] * (2.0 * image_pixels.astype(np.float64) - 1.0)
        + fusion_resolved["condition_weight"]
        * fusion_resolved["condition_gain"]
        * condition_field.astype(np.float64)
        + fusion_resolved["bias"]
    ).astype(dtype)

    anchor_field = None
    anchor_info: dict[str, Any] = {"enabled": False, "strength": 0.0}
    if reference is not None:
        strength = validate_anchor_request(
            anchor_strength, config["anchor"], reference, shape  # type: ignore[arg-type]
        )
        coerced = coerce_reference(reference, shape)
        anchor_field, reference_info = build_anchor_field(coerced, dtype)
        initial_mix = float(config["anchor"].get("initial_mix", 0.35)) * strength
        if initial_mix > 0.0:
            initial_field = (
                (1.0 - initial_mix) * initial_field.astype(np.float64)
                + initial_mix * anchor_field.astype(np.float64)
            ).astype(dtype)
        anchor_info = {
            "enabled": strength > 0.0,
            "strength": strength,
            "initial_mix_effective": initial_mix,
            **reference_info,
        }

    fusion_resolved["checksum"] = resolve_fusion(config)["checksum"]
    return GenerationContext(
        task=task,
        config=config,
        dtype=dtype,
        shape=shape,
        condition=condition,
        condition_record=condition_record,
        condition_field=condition_field,
        image_pixels=image_pixels,
        initial_field=initial_field,
        fusion_resolved=fusion_resolved,
        kernel=kernel,
        anchor_field=anchor_field,
        anchor_info=anchor_info,
    )


def apply_coupling(field: np.ndarray, kernel: np.ndarray, boundary: str = "edge_clamp") -> np.ndarray:
    if boundary != "edge_clamp":
        raise AppError(ErrorCode.INVALID_REQUEST, f"unsupported boundary mode: {boundary}")
    padded = np.pad(
        field,
        pad_width=((1, 1), (1, 1), (0, 0)),
        mode="edge",
    )
    center = kernel[1, 1]
    north = kernel[0, 1]
    south = kernel[2, 1]
    west = kernel[1, 0]
    east = kernel[1, 2]
    return (
        center * field
        + north * padded[:-2, 1:-1, :]
        + south * padded[2:, 1:-1, :]
        + west * padded[1:-1, :-2, :]
        + east * padded[1:-1, 2:, :]
    )


def iterate_field(initial_field: np.ndarray, condition_field: np.ndarray,
                  kernel: np.ndarray, config: dict[str, Any],
                  steps: int, anchor_field: np.ndarray | None = None,
                  anchor_strength: float = 0.0) -> tuple[np.ndarray, list[np.ndarray]]:
    iteration = config["iteration"]
    step_size = float(iteration["step_size"])
    condition_gain = float(iteration.get("condition_gain", 0.1))
    boundary = str(iteration.get("boundary", "edge_clamp"))
    field = initial_field
    history: list[np.ndarray] = []
    for _ in range(int(steps)):
        coupled = apply_coupling(field, kernel, boundary)
        candidate = (
            field
            + step_size * ((coupled - field) + condition_gain * condition_field)
        )
        if anchor_field is not None and anchor_strength > 0.0:
            candidate = candidate + anchor_strength * (anchor_field - candidate)
        field = candidate.astype(initial_field.dtype, copy=False)
        history.append(field.copy())
    return field, history


def aggregate_output(history: list[np.ndarray], config: dict[str, Any],
                     initial_field: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    iteration = config["iteration"]
    aggregation = iteration.get("aggregation", "mean_of_iterates_v1")
    arrays = list(history)
    if iteration.get("aggregate_initial", False) and initial_field is not None:
        arrays = [initial_field] + arrays
    if aggregation == "last_iterate_v1" or aggregation == "last":
        aggregated = arrays[-1].astype(np.float64)
        weights = [0.0] * (len(arrays) - 1) + [1.0]
    elif aggregation in ("mean_of_iterates_v1", "mean"):
        accumulator = np.zeros_like(arrays[-1], dtype=np.float64)
        for item in arrays:
            accumulator += item.astype(np.float64)
        aggregated = accumulator / float(len(arrays))
        weights = [1.0 / len(arrays)] * len(arrays)
    else:
        raise AppError(ErrorCode.INVALID_REQUEST, f"unsupported aggregation: {aggregation}")
    image = np.clip(0.5 * (aggregated + 1.0), 0.0, 1.0)
    image_u8 = np.rint(image * 255.0).astype(np.uint8)
    record = {"aggregation": aggregation, "weights": weights, "iterates": len(arrays)}
    return image, image_u8, record


def run_global(task: dict[str, Any], config: dict[str, Any],
               reference: np.ndarray | None = None,
               anchor_strength: float | None = None,
               context: GenerationContext | None = None,
               condition_override: np.ndarray | None = None,
               history: bool = False) -> dict[str, Any]:
    ctx = context or build_context(task, config, reference, anchor_strength, condition_override)
    steps = int(task["sampler_steps"])
    final_field, step_history = iterate_field(
        ctx.initial_field,
        ctx.condition_field,
        ctx.kernel,
        ctx.config,
        steps,
        ctx.anchor_field,
        float(ctx.anchor_info.get("strength", 0.0)),
    )
    image_raw, image_u8, aggregation_record = aggregate_output(step_history, ctx.config, ctx.initial_field)
    image = image_raw.astype(ctx.dtype, copy=False)
    result = {
        "task_id": task["task_id"],
        "image": image,
        "image_u8": image_u8,
        "final_field": final_field,
        "image_digest": array_digest(image),
        "image_u8_digest": array_digest(image_u8),
        "final_field_digest": array_digest(final_field),
        "context": ctx,
        "aggregation": aggregation_record,
        "intermediate_digests": [array_digest(item) for item in step_history],
    }
    if history:
        result["history"] = step_history
    return result


def extract_halo(source: np.ndarray, row_start: int, row_end: int,
                 col_start: int, col_end: int, halo: int) -> np.ndarray:
    if halo <= 0:
        return source[row_start:row_end, col_start:col_end, :]
    padded = np.pad(
        source,
        pad_width=((halo, halo), (halo, halo), (0, 0)),
        mode="edge",
    )
    return padded[row_start:row_end + 2 * halo, col_start:col_end + 2 * halo, :]


def extract_real_region(source: np.ndarray, row_start: int, row_end: int,
                        col_start: int, col_end: int, halo: int) -> tuple[np.ndarray, int, int]:
    """Extract the real dependency closure, clipped at global boundaries.

    Global edge cells are not duplicated into fake evolving halo cells.  When
    the extracted region touches row/column zero or the canvas end, the local
    Jacobi pad uses edge clamp exactly as global generation does.
    """
    height, width = source.shape[:2]
    real_row_start = max(0, row_start - halo)
    real_row_end = min(height, row_end + halo)
    real_col_start = max(0, col_start - halo)
    real_col_end = min(width, col_end + halo)
    region = source[real_row_start:real_row_end, real_col_start:real_col_end, :]
    return region, row_start - real_row_start, col_start - real_col_start


def generate_block_pixels(ctx: GenerationContext, row_start: int, row_end: int,
                          col_start: int, col_end: int) -> dict[str, Any]:
    """Generate one block using the same recurrence as the global image.

    A Jacobi/double-buffer step expands its dependency set by one cell.  With
    ``sampler_steps`` halo cells, the fixed central interval is mathematically
    identical to the corresponding global interval after every step.
    """
    steps = int(ctx.task["sampler_steps"])
    radius = int(ctx.config["iteration"].get("coupling_radius", 1))
    halo = steps * radius
    block_height = row_end - row_start
    block_width = col_end - col_start

    local_initial, target_row_start, target_col_start = extract_real_region(
        ctx.initial_field, row_start, row_end, col_start, col_end, halo
    )
    local_initial = local_initial.copy()
    local_condition, _, _ = extract_real_region(
        ctx.condition_field, row_start, row_end, col_start, col_end, halo
    )
    local_anchor = None
    anchor_strength = float(ctx.anchor_info.get("strength", 0.0))
    if ctx.anchor_field is not None and anchor_strength > 0.0:
        local_anchor, _, _ = extract_real_region(
            ctx.anchor_field, row_start, row_end, col_start, col_end, halo
        )

    final_local, local_history = iterate_field(
        local_initial,
        local_condition,
        ctx.kernel,
        ctx.config,
        steps,
        local_anchor,
        anchor_strength,
    )
    target_row_end = target_row_start + block_height
    target_col_end = target_col_start + block_width
    target_history = [
        item[target_row_start:target_row_end, target_col_start:target_col_end, :]
        for item in local_history
    ]
    image_raw, image_u8, aggregation_record = aggregate_output(
        target_history, ctx.config,
        ctx.initial_field[row_start:row_end, col_start:col_end, :] if ctx.config["iteration"].get("aggregate_initial", False) else None,
    )
    image = image_raw.astype(ctx.dtype, copy=False)
    return {
        "image": image,
        "image_u8": image_u8,
        "final_field": final_local[target_row_start:target_row_end, target_col_start:target_col_end, :],
        "bounds": {
            "row_start": row_start,
            "row_end": row_end,
            "col_start": col_start,
            "col_end": col_end,
        },
        "halo": halo,
        "target_local_start": [target_row_start, target_col_start],
        "aggregation": aggregation_record,
        "image_digest": array_digest(image),
        "image_u8_digest": array_digest(image_u8),
    }
