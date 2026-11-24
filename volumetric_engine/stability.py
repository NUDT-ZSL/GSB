"""Dither/step/cutoff stability evaluation."""

from __future__ import annotations

import dataclasses
import math
from typing import Any

from .model import Scene
from .renderer import ImageResult, render_image_in_memory
from .interrupts import CancellationToken


def _compare(reference: list[list[int]], candidate: list[list[int]]) -> dict[str, float | int]:
    height = len(reference)
    width = len(reference[0]) if height else 0
    max_abs = 0
    squared_sum = 0.0
    changed = 0
    for y in range(height):
        for x in range(width):
            delta = int(candidate[y][x]) - int(reference[y][x])
            absolute = abs(delta)
            max_abs = max(max_abs, absolute)
            squared_sum += float(delta * delta)
            changed += 1 if delta else 0
    pixels = width * height
    mse = squared_sum / pixels if pixels else 0.0
    return {
        "max_abs_pixel_deviation": max_abs,
        "mse": mse,
        "rmse": math.sqrt(mse),
        "changed_pixels": changed,
    }


def _variant(scene: Scene, *, step_size: float | None = None, cutoff: float | None = None) -> Scene:
    render = dataclasses.replace(
        scene.render,
        step_size=scene.render.step_size if step_size is None else step_size,
        transmittance_cutoff=scene.render.transmittance_cutoff if cutoff is None else cutoff,
    )
    return dataclasses.replace(scene, render=render)


def _trend_step(points: list[dict[str, Any]]) -> str:
    ordered = sorted(points, key=lambda item: float(item["step_size"]))
    values = [float(item["rmse"]) for item in ordered]
    if all(abs(value) <= 1e-15 for value in values):
        return "exact_invariant"
    if all(values[index] <= values[index + 1] + 1e-12 for index in range(len(values) - 1)):
        return "converges_as_step_size_decreases"
    return "non_monotonic_or_diverges"


def _trend_cutoff(points: list[dict[str, Any]]) -> str:
    ordered = sorted(points, key=lambda item: float(item["transmittance_cutoff"]))
    values = [float(item["rmse"]) for item in ordered]
    if all(abs(value) <= 1e-15 for value in values):
        return "exact_invariant"
    if all(values[index] <= values[index + 1] + 1e-12 for index in range(len(values) - 1)):
        return "diverges_as_cutoff_increases"
    return "stable_or_non_monotonic"


def evaluate_stability(
    scene: Scene,
    baseline: ImageResult,
    *,
    token: CancellationToken | None = None,
) -> dict[str, Any]:
    """Re-render each configured step/cutoff scale and compare to baseline."""

    step_points: list[dict[str, Any]] = []
    cutoff_points: list[dict[str, Any]] = []
    base_step = scene.render.step_size
    base_cutoff = scene.render.transmittance_cutoff

    for scale in scene.jitter.step_scales:
        step_size = base_step * scale
        image = render_image_in_memory(
            _variant(scene, step_size=step_size),
            collect_trace=False,
            token=token,
        )
        if abs(scale - 1.0) <= 1e-15 and image.gray != baseline.gray:
            raise ValueError("stability baseline re-render differs from the primary render")
        comparison = _compare(baseline.gray, image.gray)
        step_points.append(
            {
                "scale": scale,
                "step_size": step_size,
                "reused_baseline": False,
                **comparison,
                "threshold_early_terminated_pixels": image.stats["early_cutoff_pixels"],
            }
        )

    for scale in scene.jitter.cutoff_scales:
        cutoff = base_cutoff * scale
        if not (0.0 < cutoff < 1.0):
            raise ValueError(f"cutoff scale {scale} produces invalid cutoff {cutoff}")
        image = render_image_in_memory(
            _variant(scene, cutoff=cutoff),
            collect_trace=False,
            token=token,
        )
        if abs(scale - 1.0) <= 1e-15 and image.gray != baseline.gray:
            raise ValueError("stability baseline re-render differs from the primary render")
        comparison = _compare(baseline.gray, image.gray)
        cutoff_points.append(
            {
                "scale": scale,
                "transmittance_cutoff": cutoff,
                "reused_baseline": False,
                **comparison,
                "threshold_early_terminated_pixels": image.stats["early_cutoff_pixels"],
            }
        )

    return {
        "enabled": True,
        "baseline": {
            "step_size": base_step,
            "transmittance_cutoff": base_cutoff,
            "early_terminated_pixels": baseline.stats["early_cutoff_pixels"],
        },
        "step": {
            "configurations": step_points,
            "trend": _trend_step(step_points),
            "conclusion": (
                "逐像素偏差随步长减小而收敛"
                if _trend_step(step_points) == "converges_as_step_size_decreases"
                else "步长偏差未呈现单调收敛"
            ),
        },
        "cutoff": {
            "configurations": cutoff_points,
            "trend": _trend_cutoff(cutoff_points),
            "conclusion": (
                "逐像素偏差随截断阈值增大（更早剔除）而发散"
                if _trend_cutoff(cutoff_points) == "diverges_as_cutoff_increases"
                else "截断阈值偏差稳定或非单调"
            ),
            "early_termination_explanation": (
                "计数表示任一样本因定点路径先于 float64 路径低于截断阈值而提前结束的像素数。"
            ),
        },
    }

