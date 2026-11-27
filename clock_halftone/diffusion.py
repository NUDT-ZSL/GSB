"""Adaptive, mask-aware error diffusion with exact residual reassignment."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np


KERNEL_CLASS_COUNT = 17  # 1 isotropic + 8 direction bins x 2 gradient strengths


@dataclass(slots=True)
class DiffusionContext:
    width: int
    height: int
    mask: np.ndarray
    ink: np.ndarray
    dither: np.ndarray
    kernel_class: np.ndarray
    kernels: dict[int, list[np.ndarray]]
    gradient_magnitude: np.ndarray
    gradient_tangent: np.ndarray
    background_label: int
    serpentine: bool


@dataclass(slots=True)
class DiffusionResult:
    binary: np.ndarray
    incoming_error: np.ndarray
    quant_error: np.ndarray
    weight_clip_map: np.ndarray
    unallocated_map: np.ndarray
    kernel_class: np.ndarray
    dither: np.ndarray
    stats: dict[str, Any]

    def density_for_label(self, mask: np.ndarray, label: int) -> float:
        selected = mask == int(label)
        if not np.any(selected):
            return 0.0
        return float(self.binary[selected].mean())


def _direction_weights(
    base: np.ndarray,
    offsets: list[tuple[int, int]],
    tangent_angle: float,
    strength: float,
) -> np.ndarray:
    if strength <= 0.0:
        weights = base.astype(np.float64, copy=True)
    else:
        tangent_x = math.cos(tangent_angle)
        tangent_y = math.sin(tangent_angle)
        factors = np.empty(4, dtype=np.float64)
        for index, (dy, dx) in enumerate(offsets):
            length = math.hypot(dx, dy)
            alignment = (dx * tangent_x + dy * tangent_y) / length
            factors[index] = math.exp(strength * alignment)
        weights = base * factors
    weights = np.maximum(weights, 0.0)
    total = float(weights.sum())
    return weights / total if total > 0 else np.full(4, 0.25, dtype=np.float64)


def _build_kernels(base_weights: list[float], direction_strength: float) -> dict[int, list[np.ndarray]]:
    base = np.asarray(base_weights, dtype=np.float64)
    scan_offsets = {
        1: [(0, 1), (1, -1), (1, 0), (1, 1)],
        -1: [(0, -1), (1, 1), (1, 0), (1, -1)],
    }
    kernels: dict[int, list[np.ndarray]] = {}
    for scan_direction, offsets in scan_offsets.items():
        per_class: list[np.ndarray] = []
        per_class.append(_direction_weights(base, offsets, 0.0, 0.0))
        for strength in (0.55, 1.0):
            for bin_index in range(8):
                angle = bin_index * math.pi / 4.0
                per_class.append(
                    _direction_weights(base, offsets, angle, strength * direction_strength)
                )
        kernels[scan_direction] = per_class
    return kernels


def build_diffusion_context(
    gray: np.ndarray,
    mask: np.ndarray,
    dither_offsets: np.ndarray,
    config: dict[str, Any],
    *,
    background_label: int = 0,
) -> DiffusionContext:
    gray_f = gray.astype(np.float64) / 255.0
    ink = 1.0 - gray_f
    gy, gx = np.gradient(gray_f)
    magnitude = np.hypot(gx, gy)
    normal_angle = np.arctan2(gy, gx)
    tangent_angle = (normal_angle + 0.5 * math.pi) % (2.0 * math.pi)

    ed_cfg = config["error_diffusion"]
    low = float(ed_cfg["gradient_low"])
    high = float(ed_cfg["gradient_high"])
    direction_bin = np.floor(tangent_angle / (math.pi / 4.0)).astype(np.int16) % 8
    kernel_class = np.zeros(mask.shape, dtype=np.uint8)
    middle = (magnitude >= low) & (magnitude < high)
    strong = magnitude >= high
    kernel_class[middle] = 1 + direction_bin[middle]
    kernel_class[strong] = 9 + direction_bin[strong]

    return DiffusionContext(
        width=int(mask.shape[1]),
        height=int(mask.shape[0]),
        mask=mask.astype(np.int64, copy=False),
        ink=ink,
        dither=dither_offsets.astype(np.float64, copy=False),
        kernel_class=kernel_class,
        kernels=_build_kernels(
            [float(x) for x in ed_cfg["base_weights"]],
            float(ed_cfg["direction_strength"]),
        ),
        gradient_magnitude=magnitude,
        gradient_tangent=tangent_angle,
        background_label=int(background_label),
        serpentine=bool(ed_cfg.get("serpentine", True)),
    )


def diffuse(
    context: DiffusionContext,
    threshold_offsets: dict[int, float],
    *,
    only_label: int | None = None,
    frozen_binary: np.ndarray | None = None,
    frozen_incoming_error: np.ndarray | None = None,
    frozen_weight_clip: np.ndarray | None = None,
    frozen_unallocated: np.ndarray | None = None,
) -> DiffusionResult:
    """Quantize in scan order.

    Residuals are never passed across a mask boundary. This keeps mask-edge
    tone explicit in the error report and makes a single-part recomputation
    local: changing a part cannot perturb pixels of another label.

    Density correction is supplied as a threshold offset and applied as the
    equivalent tone shift ``ink - offset`` inside the feedback loop. A global
    threshold shift alone is swallowed by error feedback and would not change
    steady-state dot density.
    """
    height, width = context.height, context.width
    mask = context.mask
    ink = context.ink
    dither = context.dither
    kernel_class = context.kernel_class
    binaries = np.zeros((height, width), dtype=np.uint8)
    incoming_error = np.zeros((height, width), dtype=np.float64)
    quant_error_map = np.zeros((height, width), dtype=np.float64)
    weight_clip_map = np.zeros((height, width), dtype=np.float64)
    unallocated_map = np.zeros((height, width), dtype=np.float64)
    work_error = np.zeros((height, width), dtype=np.float64)

    if frozen_binary is not None:
        if frozen_binary.shape != (height, width):
            raise ValueError("frozen_binary shape mismatch")
        binaries[:] = frozen_binary
    if frozen_incoming_error is not None:
        if frozen_incoming_error.shape != (height, width):
            raise ValueError("frozen_incoming_error shape mismatch")
        incoming_error[:] = frozen_incoming_error
        if only_label is None:
            work_error[:] = frozen_incoming_error
    if frozen_weight_clip is not None:
        if frozen_weight_clip.shape != (height, width):
            raise ValueError("frozen_weight_clip shape mismatch")
        weight_clip_map[:] = frozen_weight_clip
    if frozen_unallocated is not None:
        if frozen_unallocated.shape != (height, width):
            raise ValueError("frozen_unallocated shape mismatch")
        unallocated_map[:] = frozen_unallocated

    selected_mask = None if only_label is None else (mask == int(only_label))
    if only_label is not None:
        incoming_error[selected_mask] = 0.0
        quant_error_map[selected_mask] = 0.0
        weight_clip_map[selected_mask] = 0.0
        unallocated_map[selected_mask] = 0.0
        work_error[selected_mask] = 0.0

    requested_mass = 0.0
    output_mass = 0
    quant_error_sum = 0.0
    scanned_pixels = 0
    scan_offsets = {
        1: [(0, 1), (1, -1), (1, 0), (1, 1)],
        -1: [(0, -1), (1, 1), (1, 0), (1, -1)],
    }

    for y in range(height):
        if context.serpentine and (y & 1):
            scan_direction = -1
            columns = range(width - 1, -1, -1)
        else:
            scan_direction = 1
            columns = range(width)
        kernels = context.kernels[scan_direction]
        offsets = scan_offsets[scan_direction]
        for x in columns:
            label = int(mask[y, x])
            if selected_mask is not None and not selected_mask[y, x]:
                continue
            scanned_pixels += 1
            arrived = work_error[y, x]
            incoming_error[y, x] = arrived
            offset = float(threshold_offsets.get(label, 0.0))
            effective_tone = ink[y, x] - offset
            requested_mass += effective_tone
            threshold = 0.5 + dither[y, x]
            value = effective_tone + arrived
            output = 1 if value >= threshold else 0
            binaries[y, x] = output
            residual = value - float(output)
            quant_error_map[y, x] = residual
            quant_error_sum += residual
            output_mass += output

            class_index = int(kernel_class[y, x])
            raw_weights = kernels[class_index]
            available: list[tuple[int, int, float]] = []
            available_weight = 0.0
            for kernel_index, (dy, dx) in enumerate(offsets):
                ny = y + dy
                nx = x + dx
                if 0 <= ny < height and 0 <= nx < width and int(mask[ny, nx]) == label:
                    weight = float(raw_weights[kernel_index])
                    available.append((ny, nx, weight))
                    available_weight += weight
            if not available:
                unallocated_map[y, x] = residual
            else:
                clipped = max(0.0, 1.0 - available_weight)
                weight_clip_map[y, x] = clipped
                if available_weight > 0.0:
                    for ny, nx, weight in available:
                        work_error[ny, nx] += residual * weight / available_weight
                else:
                    unallocated_map[y, x] = residual

    if selected_mask is None:
        weight_clipped_mass = float(weight_clip_map.sum())
        unallocated_residual = float(unallocated_map.sum())
    else:
        weight_clipped_mass = float(weight_clip_map[selected_mask].sum())
        unallocated_residual = float(unallocated_map[selected_mask].sum())
    residual_balance = requested_mass - output_mass - unallocated_residual
    conservation_error = residual_balance
    stats = {
        "scanned_pixels": int(scanned_pixels),
        "requested_ink_mass": requested_mass,
        "output_ink_mass": int(output_mass),
        "quantization_error_sum": float(quant_error_sum),
        "residual_balance": float(residual_balance),
        "boundary_abs_loss": float(abs(unallocated_residual)),
        "weight_clipped_mass": float(weight_clipped_mass),
        "unallocated_residual_sum": float(unallocated_residual),
        "residual_conservation_error": float(conservation_error),
        "residual_conservation_tolerance": float(max(1e-6, scanned_pixels * 1e-10)),
        "residual_conserved": bool(abs(conservation_error) <= max(1e-6, scanned_pixels * 1e-10)),
        "max_abs_incoming_error": float(np.max(np.abs(incoming_error))),
    }
    return DiffusionResult(
        binary=binaries,
        incoming_error=incoming_error.astype(np.float32),
        quant_error=quant_error_map.astype(np.float32),
        weight_clip_map=weight_clip_map.astype(np.float32),
        unallocated_map=unallocated_map.astype(np.float32),
        kernel_class=kernel_class,
        dither=dither,
        stats=stats,
    )


def kernel_usage_summary(
    result: DiffusionResult,
    mask: np.ndarray,
    parts: dict[int, dict[str, Any]],
    background_label: int = 0,
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    labels = [background_label, *sorted(parts)]
    for label in labels:
        selected = mask == label
        if not np.any(selected):
            continue
        counts = np.bincount(result.kernel_class[selected].ravel(), minlength=KERNEL_CLASS_COUNT)
        key = "background" if label == background_label else str(label)
        summary[key] = {
            "isotropic": int(counts[0]),
            "directional_mid": int(counts[1:9].sum()),
            "directional_strong": int(counts[9:17].sum()),
            "total": int(selected.sum()),
            "direction_bin_counts": [int(value) for value in counts[1:9]],
            "orientation_strength_bin_counts": [int(value) for value in counts[9:17]],
        }
    return summary
