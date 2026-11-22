"""Deterministic local handwriting-like digit image generation."""

from __future__ import annotations

import math
from typing import List, Mapping, Tuple

import numpy as np

from .errors import E_INVALID_INPUT, BusinessError
from .models import (
    CONTROLLED_LABELS,
    DistanceMetric,
    ImageSample,
    NormalizationConfig,
    Prototype,
    PrototypeSet,
    RecognitionConfig,
    ThresholdConfig,
)


# Explicit normalized stroke polylines. The seven-segment geometry is expanded
# into sampled points during rendering; no external font or image asset is used.
SEGMENTS: Mapping[str, Tuple[Tuple[float, float], Tuple[float, float]]] = {
    "A": ((0.14, 0.10), (0.86, 0.10)),
    "B": ((0.86, 0.10), (0.86, 0.50)),
    "C": ((0.86, 0.50), (0.86, 0.90)),
    "D": ((0.14, 0.90), (0.86, 0.90)),
    "E": ((0.14, 0.50), (0.14, 0.90)),
    "F": ((0.14, 0.10), (0.14, 0.50)),
    "G": ((0.14, 0.50), (0.86, 0.50)),
}

DIGIT_STROKES: Mapping[int, Tuple[str, ...]] = {
    0: ("A", "B", "C", "D", "E", "F"),
    1: ("B", "C"),
    2: ("A", "B", "G", "E", "D"),
    3: ("A", "B", "G", "C", "D"),
    4: ("F", "G", "B", "C"),
    5: ("A", "F", "G", "C", "D"),
    6: ("A", "F", "E", "G", "C", "D"),
    7: ("A", "B", "C"),
    8: ("A", "B", "C", "D", "E", "F", "G"),
    9: ("A", "B", "C", "D", "F", "G"),
}


def _seed_sequence(seed: int, class_id: int, variant: int, channel: int) -> np.random.SeedSequence:
    # Split a signed or large integer into stable 32-bit words. The entropy is
    # explicit; system time and OS entropy are never used.
    value = int(seed) & ((1 << 64) - 1)
    words = [
        value & 0xFFFFFFFF,
        (value >> 32) & 0xFFFFFFFF,
        int(class_id) & 0xFFFFFFFF,
        int(variant) & 0xFFFFFFFF,
        int(channel) & 0xFFFFFFFF,
        0x44494749,  # "DIGI"
    ]
    return np.random.SeedSequence(words)


def _stamp(
    canvas: np.ndarray,
    x: float,
    y: float,
    amplitude: float,
    sigma: float,
) -> None:
    height, width = canvas.shape
    x0 = max(0, int(math.floor(x)) - 2)
    x1 = min(width - 1, int(math.floor(x)) + 2)
    y0 = max(0, int(math.floor(y)) - 2)
    y1 = min(height - 1, int(math.floor(y)) + 2)
    if x1 < x0 or y1 < y0:
        return
    xs = np.arange(x0, x1 + 1, dtype=np.float64)
    ys = np.arange(y0, y1 + 1, dtype=np.float64)
    grid_x, grid_y = np.meshgrid(xs, ys)
    distance_sq = (grid_x - x) ** 2 + (grid_y - y) ** 2
    canvas[y0 : y1 + 1, x0 : x1 + 1] += amplitude * np.exp(
        -distance_sq / (2.0 * sigma * sigma)
    )


def render_digit(
    class_id: int,
    *,
    seed: int,
    variant: int = 0,
    jitter: float = 0.0,
    slant: float = 0.0,
    width: int = 28,
    height: int = 28,
    margin: float = 3.0,
) -> np.ndarray:
    """Render one digit with explicit stoke rules and deterministic entropy."""

    if class_id not in DIGIT_STROKES:
        raise BusinessError(
            E_INVALID_INPUT,
            "digit class must be in 0..9",
            category="generator",
        )
    if jitter < 0.0:
        raise BusinessError(
            E_INVALID_INPUT,
            "jitter must be non-negative",
            category="generator",
        )

    rng = np.random.Generator(
        np.random.PCG64(_seed_sequence(seed, class_id, variant, 1))
    )
    canvas = np.zeros((height, width), dtype=np.float64)
    usable_w = max(1.0, width - 2.0 * margin)
    usable_h = max(1.0, height - 2.0 * margin)
    stroke_names = DIGIT_STROKES[class_id]

    for stroke_index, name in enumerate(stroke_names):
        p0, p1 = SEGMENTS[name]
        x0 = p0[0] + float(rng.uniform(-jitter, jitter))
        y0 = p0[1] + float(rng.uniform(-jitter, jitter))
        x1 = p1[0] + float(rng.uniform(-jitter, jitter))
        y1 = p1[1] + float(rng.uniform(-jitter, jitter))
        dx = x1 - x0
        dy = y1 - y0
        length = math.hypot(dx, dy)
        steps = max(8, int(math.ceil(length * max(width, height) * 1.35)))
        normal_x = -dy / length if length > 0.0 else 0.0
        normal_y = dx / length if length > 0.0 else 0.0
        wobble_amplitude = float(rng.uniform(-0.55, 0.55)) * jitter
        wobble_phase = float(rng.uniform(0.0, 2.0 * math.pi))
        sigma = float(rng.uniform(0.82, 1.05))
        amplitude = float(rng.uniform(34.0, 42.0))

        for step in range(steps + 1):
            t = step / float(steps)
            wobble = (
                math.sin(math.pi * t) * wobble_amplitude
                + math.sin(2.0 * math.pi * t + wobble_phase)
                * wobble_amplitude
                * 0.25
            )
            nx = x0 + dx * t + normal_x * wobble
            ny = y0 + dy * t + normal_y * wobble
            nx += slant * (ny - 0.5)
            pixel_x = margin + nx * usable_w
            pixel_y = margin + ny * usable_h
            _stamp(canvas, pixel_x, pixel_y, amplitude, sigma)

        # A deterministic tiny endpoint overshoot makes the strokes look less
        # mechanical without changing the declared geometry.
        overshoot = float(rng.uniform(-0.25, 0.25)) * jitter
        if overshoot != 0.0:
            _stamp(
                canvas,
                margin + (x1 + dx * overshoot) * usable_w,
                margin + (y1 + dy * overshoot) * usable_h,
                amplitude * 0.45,
                sigma,
            )

    # Deterministic scanner-like background never exceeds the foreground range.
    grain = rng.integers(0, 4, size=(height, width), dtype=np.uint8)
    canvas += grain.astype(np.float64)
    quantized = np.clip(np.rint(canvas), 0.0, 255.0).astype(np.uint8)
    return quantized


def build_default_normalization(version: int = 1) -> NormalizationConfig:
    return NormalizationConfig(
        version=version,
        canvas_height=28,
        canvas_width=28,
        input_min=0.0,
        input_max=255.0,
        output_min=0.0,
        output_max=1.0,
        projection_bins=8,
        cell_grid_size=4,
        feature_dim=32,
    )


def build_default_thresholds(version: int = 1) -> ThresholdConfig:
    return ThresholdConfig(
        version=version,
        reject_threshold=0.300,
        high_max=0.120,
        medium_max=0.220,
    )


def generate_prototypes(
    *,
    seed: int = 20261123,
    version: int = 1,
) -> PrototypeSet:
    members: List[Prototype] = []
    for class_id in CONTROLLED_LABELS:
        pixels = render_digit(
            class_id,
            seed=seed,
            variant=0,
            jitter=0.020,
            slant=0.006 * ((class_id % 3) - 1),
        )
        members.append(
            Prototype(
                class_id=class_id,
                version=1,
                pixels=tuple(int(value) for value in pixels.reshape(-1)),
            )
        )
    return PrototypeSet(version=version, prototypes=tuple(members))


def build_default_config(
    *,
    seed: int = 20261123,
    normalization_version: int = 1,
    prototype_version: int = 1,
    threshold_version: int = 1,
) -> RecognitionConfig:
    return RecognitionConfig(
        normalization=build_default_normalization(normalization_version),
        prototypes=generate_prototypes(seed=seed, version=prototype_version),
        thresholds=build_default_thresholds(threshold_version),
        distance_metric=DistanceMetric(name="euclidean", version=1),
    )


def generate_samples(
    *,
    seed: int = 20261123,
    per_class: int = 3,
    uncertain_period: int = 7,
) -> List[ImageSample]:
    """Generate a balanced deterministic dataset.

    IDs are fixed-width sample indices so ascending image_id is ascending
    sample number. Each class receives exactly ``per_class`` samples.
    """

    if per_class < 1:
        raise BusinessError(
            E_INVALID_INPUT,
            "per_class must be at least 1",
            category="generator",
        )
    samples: List[ImageSample] = []
    index = 0
    for class_id in CONTROLLED_LABELS:
        for variant in range(per_class):
            sample_seed = (
                int(seed)
                + 104729 * (index + 1)
                + 7919 * class_id
                + 101 * variant
            )
            image = render_digit(
                class_id,
                seed=sample_seed,
                variant=variant,
                jitter=0.030 + 0.006 * ((index * 7 + class_id) % 6),
                slant=0.010 * (((index + class_id) % 5) - 2),
            )
            uncertain = ((sample_seed ^ (index * 2654435761)) & 0xFFFF) % max(
                2, uncertain_period
            ) == 0
            samples.append(
                ImageSample.from_array(
                    "img-{0:04d}".format(index),
                    image,
                    seed=sample_seed,
                    label=class_id,
                    uncertain=uncertain,
                )
            )
            index += 1
    return samples


def shift_prototype(
    prototypes: PrototypeSet,
    class_id: int,
    *,
    dx: int,
    dy: int,
) -> PrototypeSet:
    """Replace one class prototype by shifting its pixels deterministically."""

    source = prototypes.member(class_id)
    image = np.frombuffer(
        bytes(bytearray(source.pixels)), dtype=np.uint8
    ).reshape(source.canvas_height, source.canvas_width)
    shifted = np.zeros_like(image)
    source_y0 = max(0, -dy)
    source_y1 = min(source.canvas_height, source.canvas_height - dy)
    source_x0 = max(0, -dx)
    source_x1 = min(source.canvas_width, source.canvas_width - dx)
    dest_y0 = max(0, dy)
    dest_y1 = dest_y0 + (source_y1 - source_y0)
    dest_x0 = max(0, dx)
    dest_x1 = dest_x0 + (source_x1 - source_x0)
    shifted[dest_y0:dest_y1, dest_x0:dest_x1] = image[
        source_y0:source_y1, source_x0:source_x1
    ]
    replacement = Prototype(
        class_id=class_id,
        version=source.version + 1,
        pixels=tuple(int(value) for value in shifted.reshape(-1)),
        canvas_height=source.canvas_height,
        canvas_width=source.canvas_width,
    )
    return prototypes.replace_member(replacement)
