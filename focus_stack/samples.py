"""Deterministic analytic focus-stack sample generator.

The generator never relies on external data. A fixed seed drives every random
draw, and the analytic ground truth (ideal focal plane and reference sharpness)
is recorded directly for every pixel so quality reports can audit errors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

import numpy as np

from .io_stack import FocusStack


@dataclass(frozen=True)
class SampleSpec:
    stack_id: str
    height: int = 48
    width: int = 64
    n_planes: int = 6
    seed: int = 20261128
    noise_sigma: float = 3.0
    max_blur_radius: float = 4.0
    max_value: int = 4095  # 12-bit integer pixels


def _box_kernel_1d(radius: int) -> np.ndarray:
    if radius <= 0:
        return np.array([1.0], dtype=np.float64)
    size = 2 * radius + 1
    return np.full((size,), 1.0 / size, dtype=np.float64)


def _convolve_axis(image: np.ndarray, kernel: np.ndarray, axis: int) -> np.ndarray:
    radius = kernel.size // 2
    padded = np.pad(image, ((radius, radius), (0, 0)) if axis == 0 else ((0, 0), (radius, radius)),
                    mode="reflect")
    out = np.zeros_like(image)
    for offset, weight in enumerate(kernel):
        if axis == 0:
            out += weight * padded[offset:offset + image.shape[0], :]
        else:
            out += weight * padded[:, offset:offset + image.shape[1]]
    return out


def _blur(image: np.ndarray, radius: float) -> np.ndarray:
    """Separable box blur with fractional radius split between two box sizes."""
    if radius <= 0.0:
        return image.copy()
    lower = int(np.floor(radius))
    frac = radius - lower
    blurred = image
    if lower > 0:
        kernel = _box_kernel_1d(lower)
        blurred = _convolve_axis(blurred, kernel, 0)
        blurred = _convolve_axis(blurred, kernel, 1)
    if frac > 0.0:
        # Blend radius ``lower`` and ``lower+1`` for deterministic fractional blur.
        kernel_hi = _box_kernel_1d(lower + 1)
        hi = _convolve_axis(image, kernel_hi, 0)
        hi = _convolve_axis(hi, kernel_hi, 1)
        blurred = (1.0 - frac) * blurred + frac * hi
    return blurred


def _base_texture(spec: SampleSpec, rng: np.random.Generator) -> np.ndarray:
    h, w = spec.height, spec.width
    rows = np.arange(h, dtype=np.float64)[:, None]
    cols = np.arange(w, dtype=np.float64)[None, :]
    bands = np.sin(cols * 0.45) * 220.0 + np.sin(rows * 0.6) * 180.0
    stripes = np.where((cols.astype(int) // 4) % 2 == 0, 260.0, 90.0)
    blocks = np.where(((rows.astype(int) // 12) + (cols.astype(int) // 12)) % 2 == 0,
                      180.0, 60.0)
    noise = rng.normal(0.0, 28.0, size=(h, w))
    texture = bands + stripes * 0.5 + blocks * 0.6 + noise + 1500.0
    return np.clip(texture, 0.0, spec.max_value)


def _scene_depth(spec: SampleSpec, rng: np.random.Generator) -> np.ndarray:
    """Per-pixel ideal focus distance, arranged in smooth depth regions."""
    h, w = spec.height, spec.width
    rows = np.arange(h, dtype=np.float64)[:, None]
    cols = np.arange(w, dtype=np.float64)[None, :]
    depth_grid = ((rows / max(h - 1, 1)) + (cols / max(w - 1, 1))) * 0.5
    wobble = 0.12 * np.sin(rows * 0.35) * np.cos(cols * 0.25)
    jitter = rng.normal(0.0, 0.03, size=(h, w))
    depth = depth_grid + wobble + jitter
    return np.clip(depth, 0.0, 1.0)


def generate_stack(spec: SampleSpec) -> FocusStack:
    """Generate one stack plus analytic ideal plane and sharpness reference."""
    rng = np.random.default_rng(spec.seed)
    h, w = spec.height, spec.width
    n = spec.n_planes

    focus_distances = np.linspace(0.1, 0.9, n)
    # Magnification changes monotonically with focus distance.
    magnifications = 0.8 + 0.4 * focus_distances
    dof_ranges = []
    half_span = 0.5 / max(n - 1, 1) + 0.02
    for distance in focus_distances:
        dof_ranges.append([
            max(0.0, float(distance) - half_span),
            min(1.0, float(distance) + half_span),
        ])

    texture = _base_texture(spec, rng)
    scene_depth = _scene_depth(spec, rng)
    ideal_plane = np.argmin(
        np.abs(scene_depth[None, :, :] - focus_distances[:, None, None]),
        axis=0,
    ).astype(np.int64)

    frames: List[np.ndarray] = []
    sharpness_reference = np.empty((n, h, w), dtype=np.float64)
    for plane, distance in enumerate(focus_distances):
        gap = np.abs(scene_depth - float(distance))
        blur_radius = spec.max_blur_radius * gap
        # Vectorised per-pixel blur would require interpolation; approximate the
        # physical process with a small set of separable blur levels blended by
        # radius while remaining fully deterministic.
        levels = np.linspace(0.0, spec.max_blur_radius, 5)
        blurred_levels = [_blur(texture, level) for level in levels]
        upper_idx = np.searchsorted(levels, blur_radius, side="left")
        upper_idx = np.clip(upper_idx, 1, len(levels) - 1)
        lower_idx = upper_idx - 1
        denom = np.maximum(levels[upper_idx] - levels[lower_idx], 1e-12)
        frac = (blur_radius - levels[lower_idx]) / denom
        frame = (
            (1.0 - frac) * np.take_along_axis(
                np.stack(blurred_levels, axis=0),
                lower_idx[None, :, :],
                axis=0,
            ).squeeze(0)
            + frac * np.take_along_axis(
                np.stack(blurred_levels, axis=0),
                upper_idx[None, :, :],
                axis=0,
            ).squeeze(0)
        )
        sensor_noise = rng.normal(0.0, spec.noise_sigma, size=(h, w))
        frame = np.clip(np.rint(frame + sensor_noise), 0, spec.max_value)
        frames.append(frame.astype(np.uint16))

        # Analytic sharpness reference: 1.0 at the ideal plane, decaying with
        # the focus distance gap and the resulting blur.
        sharpness_reference[plane] = np.exp(-4.0 * gap)

    return FocusStack(
        spec.stack_id,
        frames,
        focus_distances,
        magnifications,
        dof_ranges,
        ideal_plane=ideal_plane,
        sharpness_reference=sharpness_reference,
    )


def generate_dataset(specs: Sequence[SampleSpec]) -> List[FocusStack]:
    return [generate_stack(spec) for spec in specs]


def default_dataset() -> List[FocusStack]:
    """Two stacks with different geometry and seeds for multi-group demos."""
    return generate_dataset(
        [
            SampleSpec("archival_microfilm", height=48, width=64, n_planes=6, seed=101),
            SampleSpec("macro_contemporary", height=40, width=56, n_planes=5, seed=202),
        ]
    )
