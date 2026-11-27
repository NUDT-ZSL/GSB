"""Visibility estimation from local contrast and the dark-channel prior."""

from __future__ import annotations

import numpy as np


def _min_filter(image: np.ndarray, radius: int) -> np.ndarray:
    radius = max(0, int(radius))
    if radius == 0:
        return image.copy()
    padded = np.pad(image, radius, mode="edge")
    height, width = image.shape
    result = np.full_like(image, np.inf)
    for dy in range(2 * radius + 1):
        for dx in range(2 * radius + 1):
            result = np.minimum(
                result, padded[dy : dy + height, dx : dx + width]
            )
    return result


def _local_contrast(gray: np.ndarray, block: int) -> tuple[float, np.ndarray]:
    height, width = gray.shape
    block = max(2, int(block))
    bh = height // block
    bw = width // block
    if bh == 0 or bw == 0:
        return float(gray.std()), np.asarray([float(gray.std())])
    cropped = gray[: bh * block, : bw * block]
    tiles = cropped.reshape(bh, block, bw, block).transpose(0, 2, 1, 3)
    tile_std = tiles.reshape(bh, bw, -1).std(axis=2)
    return float(tile_std.mean()), tile_std.ravel()


def frame_visibility_statistics(
    frame: np.ndarray,
    config,
) -> dict[str, float]:
    array = np.asarray(frame)
    if array.ndim != 3 or array.shape[2] not in (1, 3):
        raise ValueError("a frame must have shape (height, width, channels)")
    rgb = array.astype(np.float64) / 255.0
    if rgb.shape[2] == 1:
        rgb = np.repeat(rgb, 3, axis=2)
    gray = (
        0.2126 * rgb[:, :, 0]
        + 0.7152 * rgb[:, :, 1]
        + 0.0722 * rgb[:, :, 2]
    )
    contrast, tile_contrast = _local_contrast(gray, config.contrast_block)
    dark_channel = _min_filter(np.min(rgb, axis=2), config.dark_channel_radius)
    dark_mean = float(dark_channel.mean())
    dark_p90 = float(np.percentile(dark_channel, 90.0))

    contrast_norm = float(np.clip(contrast / config.contrast_scale, 0.0, 1.0))
    dark_score = float(
        np.clip(
            (config.dark_reference - dark_mean) / config.dark_scale,
            0.0,
            1.0,
        )
    )
    score = 100.0 * (
        config.contrast_weight * contrast_norm
        + config.dark_weight * dark_score
    )
    saturation = float(np.mean(rgb.max(axis=2) - rgb.min(axis=2)))
    return {
        "score": float(np.clip(score, 0.0, 100.0)),
        "local_contrast": contrast,
        "local_contrast_norm": contrast_norm,
        "local_contrast_block_std": float(tile_contrast.mean()),
        "dark_channel_mean": dark_mean,
        "dark_channel_p90": dark_p90,
        "dark_channel_score": dark_score,
        "saturation_mean": saturation,
        "luminance_mean": float(gray.mean()),
        "luminance_std": float(gray.std()),
    }


def frame_visibility_series(frames: np.ndarray, config) -> np.ndarray:
    scores = np.empty(frames.shape[0], dtype=np.float64)
    for index in range(frames.shape[0]):
        scores[index] = frame_visibility_statistics(frames[index], config)["score"]
    return scores


def visibility_grade(score: float, config) -> tuple[int, str]:
    if score >= config.clear_score:
        return 0, "clear"
    if score >= config.light_score:
        return 1, "light_turbidity"
    if score >= config.moderate_score:
        return 2, "moderate_turbidity"
    return 3, "heavy_turbidity"


def score_shot(
    frames: np.ndarray,
    start_frame: int,
    end_frame: int,
    config,
) -> dict:
    statistics = [
        frame_visibility_statistics(frames[index], config)
        for index in range(start_frame, end_frame)
    ]
    scores = np.asarray([item["score"] for item in statistics], dtype=np.float64)
    contrasts = np.asarray(
        [item["local_contrast"] for item in statistics], dtype=np.float64
    )
    dark_means = np.asarray(
        [item["dark_channel_mean"] for item in statistics], dtype=np.float64
    )
    dark_p90 = np.asarray(
        [item["dark_channel_p90"] for item in statistics], dtype=np.float64
    )
    saturations = np.asarray(
        [item["saturation_mean"] for item in statistics], dtype=np.float64
    )
    score = float(scores.mean())
    grade, grade_name = visibility_grade(score, config)
    return {
        "score": round(score, 6),
        "score_std": round(float(scores.std()), 6),
        "score_median": round(float(np.median(scores)), 6),
        "score_min": round(float(scores.min()), 6),
        "score_max": round(float(scores.max()), 6),
        "score_p10": round(float(np.percentile(scores, 10.0)), 6),
        "score_p90": round(float(np.percentile(scores, 90.0)), 6),
        "turbidity_level": grade,
        "turbidity_level_name": grade_name,
        "frames_used": int(end_frame - start_frame),
        "local_contrast_mean": round(float(contrasts.mean()), 6),
        "local_contrast_norm_mean": round(
            float(
                np.mean(
                    [item["local_contrast_norm"] for item in statistics]
                )
            ),
            6,
        ),
        "dark_channel_mean": round(float(dark_means.mean()), 6),
        "dark_channel_p90_mean": round(float(dark_p90.mean()), 6),
        "dark_channel_score_mean": round(
            float(
                np.mean(
                    [item["dark_channel_score"] for item in statistics]
                )
            ),
            6,
        ),
        "saturation_mean": round(float(saturations.mean()), 6),
    }
