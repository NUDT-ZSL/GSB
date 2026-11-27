"""Deterministic synthetic underwater thumbnail generator.

The generator deliberately contains the three phenomena needed by the
analysis task:

* slow diver motion (small, smooth frame-to-frame appearance changes),
* jittering suspended particles (high-frequency texture changes), and
* one or more sustained turbidity influxes (a global grey/green veil).

It also inserts a one-frame exposure flash. This creates two adjacent raw
boundary peaks and is used to exercise the short-shot correction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .storage import save_npz_deterministic


@dataclass(frozen=True)
class GeneratedSample:
    frames: np.ndarray
    timestamps: np.ndarray
    truth: dict[str, Any]
    seed: int
    fps: float


def _coarse_field(
    rng: np.random.Generator, height: int, width: int, channels: int
) -> np.ndarray:
    """Create smooth deterministic texture without SciPy."""

    ch = max(2, height // 8)
    cw = max(2, width // 8)
    coarse = rng.normal(0.0, 1.0, size=(ch, cw, channels))
    ys = np.linspace(0, ch - 1, height)
    xs = np.linspace(0, cw - 1, width)
    y0 = np.floor(ys).astype(np.int32)
    x0 = np.floor(xs).astype(np.int32)
    y1 = np.minimum(y0 + 1, ch - 1)
    x1 = np.minimum(x0 + 1, cw - 1)
    wy = (ys - y0)[:, None, None]
    wx = (xs - x0)[None, :, None]
    top = coarse[y0][:, x0] * (1.0 - wx) + coarse[y0][:, x1] * wx
    bottom = coarse[y1][:, x0] * (1.0 - wx) + coarse[y1][:, x1] * wx
    return top * (1.0 - wy) + bottom * wy


def _ellipse_mask(
    height: int, width: int, cy: float, cx: float, ry: float, rx: float
) -> np.ndarray:
    yy, xx = np.ogrid[:height, :width]
    return ((yy - cy) / max(ry, 0.5)) ** 2 + ((xx - cx) / max(rx, 0.5)) ** 2 <= 1.0


def _line_mask(
    height: int,
    width: int,
    y0: float,
    x0: float,
    y1: float,
    x1: float,
    thickness: float,
) -> np.ndarray:
    yy, xx = np.ogrid[:height, :width]
    dy = y1 - y0
    dx = x1 - x0
    length_sq = max(dy * dy + dx * dx, 1e-6)
    t = np.clip(((yy - y0) * dy + (xx - x0) * dx) / length_sq, 0.0, 1.0)
    py = y0 + t * dy
    px = x0 + t * dx
    return (yy - py) ** 2 + (xx - px) ** 2 <= thickness * thickness


def _make_scene(
    rng: np.random.Generator, height: int, width: int
) -> tuple[np.ndarray, np.ndarray]:
    """Return a static scene and a high-frequency particle texture."""

    yy, xx = np.mgrid[0:height, 0:width]
    yn = yy / max(height - 1, 1)
    xn = xx / max(width - 1, 1)

    deep = np.array([18.0, 59.0, 79.0]) + rng.uniform(-8.0, 8.0, size=3)
    shallow = np.array([38.0, 111.0, 116.0]) + rng.uniform(-9.0, 9.0, size=3)
    image = (
        deep[None, None, :] * (1.0 - yn[:, :, None])
        + shallow[None, None, :] * yn[:, :, None]
    )
    image += 1.8 * np.sin(2.0 * np.pi * (1.8 * xn + 0.7 * yn))[:, :, None]

    smooth = _coarse_field(rng, height, width, 3)
    image += smooth * np.array([20.0, 24.0, 21.0])[None, None, :]
    image += rng.normal(0.0, 2.5, size=(height, width, 3))

    # Reef silhouettes make scene-specific edge patterns.
    for _ in range(13):
        cy = rng.uniform(0.68, 1.04) * height
        cx = rng.uniform(-0.06, 1.06) * width
        ry = rng.uniform(0.04, 0.15) * height
        rx = rng.uniform(0.05, 0.18) * width
        mask = _ellipse_mask(height, width, cy, cx, ry, rx)
        color = rng.uniform([16.0, 38.0, 38.0], [75.0, 105.0, 82.0])
        image[mask] = image[mask] * 0.34 + color[None, :] * 0.66
        rim = _ellipse_mask(height, width, cy - ry * 0.20, cx, ry * 0.78, rx * 0.84)
        image[rim] *= 1.06

    for _ in range(9):
        y0 = rng.uniform(0.45, 1.0) * height
        x0 = rng.uniform(0.0, 1.0) * width
        y1 = y0 + rng.uniform(-0.22, 0.04) * height
        x1 = x0 + rng.uniform(-0.12, 0.12) * width
        mask = _line_mask(
            height, width, y0, x0, y1, x1, rng.uniform(1.0, 2.8)
        )
        color = rng.uniform([70.0, 123.0, 74.0], [124.0, 164.0, 100.0])
        image[mask] = image[mask] * 0.18 + color[None, :] * 0.82

    particle_rng = np.random.default_rng(int(rng.integers(0, 2**31 - 1)))
    particles = np.zeros((height, width), dtype=np.float64)
    for _ in range(max(18, height * width // 180)):
        py = int(particle_rng.integers(0, height))
        px = int(particle_rng.integers(0, width))
        particles[py, px] += particle_rng.uniform(18.0, 54.0)
    particle_texture = _coarse_field(particle_rng, height, width, 1)[:, :, 0]
    particles += np.clip(particle_texture * 1.6, -4.0, 5.0)

    return np.clip(image, 0.0, 255.0), particles


def _draw_diver(
    image: np.ndarray,
    center_y: float,
    center_x: float,
    phase: float,
    scale: float,
) -> None:
    height, width = image.shape[:2]
    body_color = np.array([22.0, 29.0, 34.0])
    fin_color = np.array([30.0, 55.0, 62.0])
    tank_color = np.array([71.0, 82.0, 77.0])

    body = _ellipse_mask(
        height, width, center_y, center_x, 10.0 * scale, 4.6 * scale
    )
    head = _ellipse_mask(
        height,
        width,
        center_y - 1.0 * scale,
        center_x + 6.2 * scale,
        3.0 * scale,
        3.0 * scale,
    )
    image[body] = image[body] * 0.22 + body_color[None, :] * 0.78
    image[head] = image[head] * 0.18 + body_color[None, :] * 0.82

    tank = _ellipse_mask(
        height,
        width,
        center_y - 1.3 * scale,
        center_x - 2.1 * scale,
        2.4 * scale,
        5.5 * scale,
    )
    image[tank] = image[tank] * 0.35 + tank_color[None, :] * 0.65

    swing = 2.1 * scale * np.sin(phase)
    for side in (-1.0, 1.0):
        fin = _line_mask(
            height,
            width,
            center_y + side * 2.1 * scale,
            center_x - 3.0 * scale,
            center_y + side * (4.2 * scale + swing),
            center_x - 7.4 * scale,
            1.25 * scale,
        )
        image[fin] = image[fin] * 0.25 + fin_color[None, :] * 0.75

    arm = _line_mask(
        height,
        width,
        center_y,
        center_x + 4.9 * scale,
        center_y + 2.6 * scale,
        center_x + 8.4 * scale,
        1.08 * scale,
    )
    image[arm] = image[arm] * 0.30 + body_color[None, :] * 0.70


def _box_smooth_1d(values: np.ndarray, width: int) -> np.ndarray:
    if width <= 1:
        return values.copy()
    radius = width // 2
    padded = np.pad(values, radius, mode="edge")
    kernel = np.ones(width, dtype=np.float64) / width
    return np.convolve(padded, kernel, mode="valid")


def _make_shot_lengths(rng: np.random.Generator, n_frames: int) -> list[int]:
    count = int(rng.integers(5, 8))
    weights = rng.uniform(0.88, 1.14, size=count)
    raw = np.floor(n_frames * weights / weights.sum()).astype(np.int32)
    raw[-1] += n_frames - int(raw.sum())
    return [int(value) for value in raw]


def _make_haze_intervals(
    rng: np.random.Generator, n_frames: int, seed: int
) -> list[tuple[int, int]]:
    # Give the documented acceptance seed a stable single influx in shot 4.
    if seed == 20240517:
        start = min(258, max(3, n_frames - 65))
        end = min(n_frames - 12, start + 102)
        return [(start, end)] if end > start + 10 else []

    low = min(max(3, n_frames // 4), max(4, n_frames - 20))
    high = min(max(low + 1, n_frames // 2), max(low + 2, n_frames - 12))
    first_start = int(rng.integers(low, high))
    target_length = int(rng.integers(min(68, max(8, n_frames // 3)), 119))
    first_end = min(n_frames - 3, first_start + target_length)
    if first_end <= first_start + 2:
        return []
    intervals = [(first_start, first_end)]
    if seed % 2 == 0 and n_frames >= 420:
        second_start = min(n_frames - 85, first_end + int(rng.integers(52, 105)))
        second_end = min(n_frames - 20, second_start + int(rng.integers(55, 94)))
        if second_end - second_start >= 35 and second_start > first_end + 20:
            intervals.append((second_start, second_end))
    return intervals


def _haze_alpha(n_frames: int, intervals: list[tuple[int, int]]) -> np.ndarray:
    alpha = np.zeros(n_frames, dtype=np.float64)
    attack = 9
    release = 11
    for start, end in intervals:
        start = max(0, min(int(start), n_frames))
        end = max(start, min(int(end), n_frames))
        for frame in range(start, end):
            ramp_in = min(1.0, (frame - start + 1) / attack)
            ramp_out = min(1.0, (end - frame) / release)
            alpha[frame] = max(alpha[frame], 0.62 * min(ramp_in, ramp_out))
    return alpha


def generate_sample(
    seed: int,
    n_frames: int = 480,
    height: int = 72,
    width: int = 96,
    fps: float = 8.0,
) -> GeneratedSample:
    """Generate integer RGB thumbnails and their sampling timestamps."""

    if n_frames < 24:
        raise ValueError("n_frames must be at least 24")
    if height < 16 or width < 16:
        raise ValueError("frame dimensions must be at least 16x16")

    seed = int(seed)
    rng = np.random.default_rng(seed)
    lengths = _make_shot_lengths(rng, n_frames)
    shot_starts = np.concatenate([[0], np.cumsum(lengths)]).astype(np.int32)
    shot_starts[-1] = n_frames
    true_cut_frames = [int(value) for value in shot_starts[1:-1]]

    scenes: list[tuple[np.ndarray, np.ndarray]] = []
    for _ in lengths:
        scenes.append(_make_scene(rng, height, width))

    haze_intervals = _make_haze_intervals(rng, n_frames, seed)
    alpha = _haze_alpha(n_frames, haze_intervals)
    fog_field = _coarse_field(rng, height, width, 1)[:, :, 0]
    fog = (
        np.array([126.0, 143.0, 145.0])[None, None, :]
        + np.clip(fog_field, -1.5, 1.5)[:, :, None] * 9.0
    )

    # Keep the false cut away from real cuts and haze transitions.
    false_cut_frame = min(
        n_frames - 22, max(24, int(n_frames * 0.41) + int(rng.integers(-7, 8)))
    )
    while any(abs(false_cut_frame - cut) < 8 for cut in true_cut_frames):
        false_cut_frame += 3

    frames = np.empty((n_frames, height, width, 3), dtype=np.uint8)
    shot_index = 0
    for frame_index in range(n_frames):
        while frame_index >= int(shot_starts[shot_index + 1]):
            shot_index += 1
        start = int(shot_starts[shot_index])
        end = int(shot_starts[shot_index + 1])
        local = (frame_index - start) / max(end - start - 1, 1)
        scene, particles = scenes[shot_index]

        phase = 2.0 * np.pi * (1.2 * local + 0.08 * shot_index)
        center_x = width * (0.24 + 0.48 * local) + 3.0 * np.sin(phase)
        center_y = height * (0.46 + 0.06 * np.sin(phase * 0.7))
        scale = 0.82 + 0.08 * np.sin(phase * 0.5)

        illumination = 1.0 + 0.045 * np.sin(2.0 * np.pi * (local * 0.65 + 0.1))
        image = scene * illumination

        # Particle jitter is small in position but visible in successive frames.
        jitter_rng = np.random.default_rng(seed + frame_index * 131 + 17)
        dx = int(jitter_rng.integers(-2, 3))
        dy = int(jitter_rng.integers(-2, 3))
        shifted = np.roll(particles, shift=(dy, dx), axis=(0, 1))
        image += shifted[:, :, None] * np.array([0.55, 0.74, 0.68])[None, None, :]

        _draw_diver(image, center_y, center_x, phase, scale)
        if alpha[frame_index] > 0.0:
            a = float(alpha[frame_index])
            image = (1.0 - a) * image + a * fog

        image = np.clip(np.rint(image), 0.0, 255.0).astype(np.uint8)
        if frame_index == false_cut_frame:
            # A one-frame overexposure, not a real edit.
            image = np.clip(205 + image.astype(np.int16) // 7, 0, 255).astype(
                np.uint8
            )
        frames[frame_index] = image

    timestamps = np.arange(n_frames, dtype=np.float64) / float(fps)
    truth: dict[str, Any] = {
        "true_cut_frames": true_cut_frames,
        "true_cut_times": [float(timestamps[value]) for value in true_cut_frames],
        "true_shot_ranges": [
            {
                "start_frame": int(shot_starts[index]),
                "end_frame": int(shot_starts[index + 1]),
                "end_frame_exclusive": int(shot_starts[index + 1]),
                "start_time_s": float(timestamps[shot_starts[index]]),
                "end_time_s": float(timestamps[shot_starts[index + 1] - 1]),
            }
            for index in range(len(lengths))
        ],
        "true_haze_intervals": [
            {
                "start_frame": int(start),
                "end_frame": int(end),
                "end_frame_exclusive": int(end),
                "start_time_s": float(timestamps[start]),
                "end_time_s": float(timestamps[end - 1]),
            }
            for start, end in haze_intervals
        ],
        "false_cut_injection_frames": [int(false_cut_frame)],
        "generator": {
            "shape": [int(n_frames), int(height), int(width), 3],
            "dtype": "uint8",
            "fps": float(fps),
            "seed": seed,
        },
    }
    return GeneratedSample(
        frames=frames,
        timestamps=timestamps,
        truth=truth,
        seed=seed,
        fps=float(fps),
    )


def save_sample(sample: GeneratedSample, destination: str | Path) -> Path:
    truth_json = np.asarray(
        json.dumps(sample.truth, ensure_ascii=False, sort_keys=True)
    )
    return save_npz_deterministic(
        destination,
        {
            "frames": sample.frames,
            "timestamps": sample.timestamps,
            "truth_json": truth_json,
            "seed": np.asarray(sample.seed, dtype=np.int64),
            "fps": np.asarray(sample.fps, dtype=np.float64),
        },
    )
