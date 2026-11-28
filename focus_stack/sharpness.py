"""Per-pixel multi-plane sharpness, focal candidate set and confidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

from .config import CoreConfig
from .filters import box_filter, gradient_energy, laplacian_energy
from .io_stack import FocusStack


@dataclass
class SelectionResult:
    sharpness: np.ndarray  # (planes, h, w)
    valid: np.ndarray  # bool (planes, h, w) candidate mask
    initial_map: np.ndarray  # int64 (h, w)
    confidence: np.ndarray  # float64 (h, w) normalised peak margin
    peak: np.ndarray
    second_peak: np.ndarray
    noise_floor: float
    radii: Tuple[int, int]


# Planes within this fraction of a pixel's peak sharpness remain candidates.
# This is a purely local criterion so single-frame changes propagate only
# through spatial neighbourhoods, never through a global threshold.
CANDIDATE_RELATIVE_FLOOR = 0.05


def _window_radii(stack: FocusStack, config: CoreConfig) -> Tuple[int, int]:
    return stack.adaptive_radii(config)


def _local_normalise(
    energy: np.ndarray, context_radius: int, box_fn
) -> np.ndarray:
    """Per-pixel local energy normalisation.

    Each smoothed energy is divided by a local neighbourhood mean (with a small
    stabiliser). Because every output pixel depends only on a finite spatial
    window, changing one frame can only alter sharpness inside that window's
    reach, which is what makes bounded incremental recomputation possible.
    ``box_fn`` lets full and region runs share identical arithmetic.
    """
    local_mean = box_fn(energy, context_radius)
    # A fixed constant stabiliser (rather than one scaled by the local mean)
    # keeps each output dependent on a bounded window union. A mean-scaled term
    # would let a single changed pixel perturb normalisation across the whole
    # context window and defeat bounded incremental recomputation.
    return energy / (local_mean + 1.0)


def _plane_energy(stack: FocusStack, plane: int) -> Tuple[np.ndarray, np.ndarray]:
    image = stack.frame_float(plane)
    return laplacian_energy(image), gradient_energy(image)


def plane_sharpness_full(
    stack: FocusStack, plane: int, radii: Tuple[int, int], config: CoreConfig
) -> np.ndarray:
    """Fused sharpness map for one plane over the whole image."""
    lap_energy, grad_energy = _plane_energy(stack, plane)
    radius_small, radius_large = radii
    lap_small = box_filter(lap_energy, radius_small)
    lap_large = box_filter(lap_energy, radius_large)
    grad_small = box_filter(grad_energy, radius_small)
    grad_large = box_filter(grad_energy, radius_large)
    context = radius_large + 2
    lap_map = 0.5 * (
        _local_normalise(lap_small, context, box_filter)
        + _local_normalise(lap_large, context, box_filter)
    )
    grad_map = 0.5 * (
        _local_normalise(grad_small, context, box_filter)
        + _local_normalise(grad_large, context, box_filter)
    )
    return (
        config.laplacian_weight * lap_map
        + config.gradient_weight * grad_map
    )


def compute_sharpness_cube(
    stack: FocusStack, config: CoreConfig
) -> Tuple[np.ndarray, Tuple[int, int]]:
    radii = _window_radii(stack, config)
    cube = np.empty((stack.n_planes,) + stack.shape, dtype=np.float64)
    for plane in range(stack.n_planes):
        cube[plane] = plane_sharpness_full(stack, plane, radii, config)
    return cube, radii


def candidate_mask(sharpness: np.ndarray) -> Tuple[np.ndarray, float]:
    """Candidate focal planes per pixel.

    A plane is a candidate when its sharpness is within
    :data:`CANDIDATE_RELATIVE_FLOOR` of that pixel's peak. The criterion is
    evaluated independently at every pixel, which keeps incremental influence
    local. A global quantile noise floor is still returned for quality
    reporting, but it does not gate selection.
    """
    # Robust global floor used only by reports / diagnostics.
    floor = float(np.quantile(sharpness, 0.05))
    peak_per_pixel = sharpness.max(axis=0, keepdims=True)
    valid = sharpness >= (
        peak_per_pixel * (1.0 - CANDIDATE_RELATIVE_FLOOR)
    )
    # Explicit guarantee that the winning plane is always a candidate.
    winner = np.argmax(sharpness, axis=0)
    rows, cols = np.indices(sharpness.shape[1:])
    valid[winner, rows, cols] = True
    return valid, floor


def initial_selection(
    sharpness: np.ndarray, valid: np.ndarray, config: CoreConfig
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Pick the peak plane and confidence within each pixel's candidate set."""
    masked = np.where(valid, sharpness, -np.inf)
    peak_plane = np.argmax(masked, axis=0).astype(np.int64)
    peak = np.take_along_axis(
        sharpness, peak_plane[None, :, :], axis=0
    ).squeeze(0)

    # Second peak among candidates excluding the winning plane.
    masked_second = masked.copy()
    np.put_along_axis(
        masked_second, peak_plane[None, :, :], -np.inf, axis=0
    )
    second_plane = np.argmax(masked_second, axis=0)
    second_peak = np.take_along_axis(
        sharpness, second_plane[None, :, :], axis=0
    ).squeeze(0)
    # Pixels with only one candidate have no meaningful second peak.
    single_candidate = valid.sum(axis=0) <= 1
    second_peak = np.where(single_candidate, 0.0, second_peak)

    confidence = (peak - second_peak) / (
        np.maximum(peak, second_peak) + config.peak_margin_eps
    )
    confidence = np.clip(confidence, 0.0, 1.0)
    return peak_plane, confidence, peak, second_peak


def select_planes(
    stack: FocusStack, config: CoreConfig, sharpness: Optional[np.ndarray] = None
) -> SelectionResult:
    if sharpness is None:
        sharpness, radii = compute_sharpness_cube(stack, config)
    else:
        radii = _window_radii(stack, config)
    valid, floor = candidate_mask(sharpness)
    initial_map, confidence, peak, second_peak = initial_selection(
        sharpness, valid, config
    )
    return SelectionResult(
        sharpness=sharpness,
        valid=valid,
        initial_map=initial_map,
        confidence=confidence,
        peak=peak,
        second_peak=second_peak,
        noise_floor=floor,
        radii=radii,
    )


