"""Deterministic per-pixel volumetric integration.

The final image is generated exclusively from the fixed-point path.  A
float64 path runs alongside it and is recorded for diagnostics only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .geometry import build_interval_chain, light_transmittance
from .model import MediaBox, Scene, Vec3, _add, _cross, _dot, _mul, _normalize


TRACE_COLUMNS = (
    "x",
    "y",
    "sample",
    "step",
    "medium_id",
    "medium_index",
    "segment_start",
    "sample_t",
    "segment_length",
    "segment_optical_depth",
    "accumulated_optical_depth",
    "float_transmittance",
    "fixed_transmittance",
    "abs_difference",
    "fixed_rounding_difference",
    "fixed_active",
)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def _quantize_factor(optical_depth: float, fixed_scale: int) -> int:
    exact = math.exp(-optical_depth)
    return max(0, min(fixed_scale, _half_up(exact * fixed_scale)))


def _fixed_multiply(left: int, right: int, fixed_scale: int) -> int:
    return (left * right + fixed_scale // 2) // fixed_scale


def _tone_map(radiance: float, exposure: float) -> int:
    if not math.isfinite(radiance) or radiance <= 0.0:
        return 0
    mapped = 1.0 - math.exp(-exposure * radiance)
    return max(0, min(255, _half_up(255.0 * mapped)))


def _radiance_to_unit(radiance: float, exposure: float) -> float:
    if not math.isfinite(radiance) or radiance <= 0.0:
        return 0.0
    return 1.0 - math.exp(-exposure * radiance)


def _make_ray(scene: Scene, pixel_x: int, pixel_y: int, offset_x: float, offset_y: float) -> tuple[Vec3, Vec3]:
    camera = scene.camera
    right = _normalize(_cross(camera.up, camera.forward))
    true_up = _normalize(_cross(camera.forward, right))
    tan_half = math.tan(math.radians(camera.vertical_fov_deg) * 0.5)
    ndc_x = (2.0 * (pixel_x + offset_x) / scene.render.width - 1.0) * tan_half * camera.aspect_ratio
    ndc_y = (1.0 - 2.0 * (pixel_y + offset_y) / scene.render.height) * tan_half
    direction = _normalize(
        _add(
            camera.forward,
            _add(_mul(right, ndc_x), _mul(true_up, ndc_y)),
        )
    )
    return camera.position, direction


def _local_scattering(scene: Scene, point: Vec3, medium: MediaBox) -> float:
    scattering = medium.scattering_scalar
    if scattering <= 0.0:
        return 0.0
    total = 0.0
    for light in scene.lights:
        visibility, distance, _ = light_transmittance(
            point,
            light.position,
            scene.media,
            epsilon=scene.render.ray_epsilon,
        )
        if distance <= scene.render.ray_epsilon:
            continue
        distance_sq = max(distance * distance, scene.render.ray_epsilon * scene.render.ray_epsilon)
        total += scattering * light.intensity * visibility / (4.0 * math.pi * distance_sq)
    return total


@dataclass
class SampleResult:
    fixed_radiance: float = 0.0
    float_radiance: float = 0.0
    depth: float = -1.0
    max_abs_difference: float = 0.0
    max_fixed_rounding_difference: float = 0.0
    first_exceed: dict[str, Any] | None = None
    early_cutoff: bool = False
    fixed_stop_step: int | None = None
    float_stop_step: int | None = None
    fixed_stopped_by_cutoff: bool = False
    float_stopped_by_cutoff: bool = False
    steps: int = 0
    trace_rows: list[tuple[Any, ...]] = field(default_factory=list)


@dataclass
class PixelResult:
    x: int
    y: int
    gray: int
    reference_gray: int
    depth: float
    max_abs_difference: float
    max_fixed_rounding_difference: float
    diagnostic_exceeded: bool
    fixed_verdict: str
    first_exceed: dict[str, Any] | None
    early_cutoff: bool
    fixed_stopped_by_cutoff: bool
    float_stopped_by_cutoff: bool
    fixed_stop_step: int | None
    float_stop_step: int | None
    samples: int
    steps: int


def trace_pixel_sample(
    scene: Scene,
    pixel_x: int,
    pixel_y: int,
    sample_index: int,
    origin: Vec3,
    direction: Vec3,
    *,
    collect_trace: bool,
) -> SampleResult:
    render = scene.render
    chain = build_interval_chain(
        origin,
        direction,
        scene.media,
        t_min=0.0,
        epsilon=render.ray_epsilon,
    )
    if not chain:
        return SampleResult()

    result = SampleResult(depth=chain[0].start)
    float_t = 1.0
    fixed_t = render.fixed_scale
    fixed_product_t = 1.0
    fixed_active = True
    float_active = True
    accumulated_tau = 0.0
    step_index = 0

    for interval in chain:
        if not fixed_active and not float_active:
            break
        length = interval.length
        if length <= render.ray_epsilon:
            continue
        segment_count = max(1, int(math.ceil(length / render.step_size)))
        segment_length = length / segment_count
        medium = interval.medium
        sigma_t = medium.extinction_scalar

        for local_index in range(segment_count):
            if step_index >= render.max_steps:
                break
            segment_start = interval.start + local_index * segment_length
            sample_t = segment_start + segment_length * 0.5
            point = (
                origin[0] + direction[0] * sample_t,
                origin[1] + direction[1] * sample_t,
                origin[2] + direction[2] * sample_t,
            )
            tau_step = sigma_t * segment_length
            tau_mid = tau_step * 0.5
            tau_at_sample = accumulated_tau + tau_mid
            step_index += 1

            float_mid = float_t * math.exp(-tau_mid) if float_active else float_t
            if fixed_active:
                factor_mid_fixed = _quantize_factor(tau_mid, render.fixed_scale)
                fixed_mid = _fixed_multiply(fixed_t, factor_mid_fixed, render.fixed_scale)
                fixed_product_mid = fixed_product_t * (factor_mid_fixed / render.fixed_scale)
                fixed_mid_unit = fixed_mid / render.fixed_scale
                fixed_rounding_difference = abs(fixed_mid_unit - fixed_product_mid)
            else:
                fixed_mid = fixed_t
                fixed_mid_unit = fixed_t / render.fixed_scale
                fixed_product_mid = fixed_product_t
                fixed_rounding_difference = 0.0

            if fixed_active or float_active:
                local = _local_scattering(scene, point, medium)
                if fixed_active:
                    result.fixed_radiance += fixed_mid_unit * local * segment_length
                if float_active:
                    result.float_radiance += float_mid * local * segment_length

            abs_difference = abs(fixed_mid_unit - float_mid)
            result.max_abs_difference = max(result.max_abs_difference, abs_difference)
            result.max_fixed_rounding_difference = max(
                result.max_fixed_rounding_difference, fixed_rounding_difference
            )
            if (
                fixed_active
                and float_active
                and abs_difference > render.transmittance_tolerance
                and result.first_exceed is None
            ):
                result.first_exceed = {
                    "pixel_x": pixel_x,
                    "pixel_y": pixel_y,
                    "sample": sample_index,
                    "step": step_index,
                    "medium_id": medium.id,
                    "medium_index": medium.declaration_index,
                    "segment_start": segment_start,
                    "sample_t": sample_t,
                    "segment_length": segment_length,
                    "segment_optical_depth": tau_step,
                    "accumulated_optical_depth": tau_at_sample,
                    "float_transmittance": float_mid,
                    "fixed_transmittance": fixed_mid_unit,
                    "abs_difference": abs_difference,
                    "fixed_rounding_difference": fixed_rounding_difference,
                }

            if collect_trace:
                result.trace_rows.append(
                    (
                        pixel_x,
                        pixel_y,
                        sample_index,
                        step_index,
                        medium.id,
                        medium.declaration_index,
                        segment_start,
                        sample_t,
                        segment_length,
                        tau_step,
                        tau_at_sample,
                        float_mid,
                        fixed_mid_unit,
                        abs_difference,
                        fixed_rounding_difference,
                        1 if fixed_active else 0,
                    )
                )

            if fixed_active:
                factor_fixed = _quantize_factor(tau_step, render.fixed_scale)
                fixed_t = _fixed_multiply(fixed_t, factor_fixed, render.fixed_scale)
                fixed_product_t *= factor_fixed / render.fixed_scale
                result.max_fixed_rounding_difference = max(
                    result.max_fixed_rounding_difference,
                    abs(fixed_t / render.fixed_scale - fixed_product_t),
                )
            if float_active:
                float_t *= math.exp(-tau_step)
            accumulated_tau += tau_step

            if fixed_active and fixed_t / render.fixed_scale < render.transmittance_cutoff:
                fixed_active = False
                result.fixed_stopped_by_cutoff = True
                result.fixed_stop_step = step_index
            if float_active and float_t < render.transmittance_cutoff:
                float_active = False
                result.float_stopped_by_cutoff = True
                result.float_stop_step = step_index
            if not fixed_active and not float_active:
                break
        if step_index >= render.max_steps:
            break

    result.steps = step_index
    result.early_cutoff = result.fixed_stopped_by_cutoff and (
        result.float_stop_step is None or (
            result.fixed_stop_step is not None and result.fixed_stop_step < result.float_stop_step
        )
    )
    return result


def _finalize_pixel(
    scene: Scene,
    pixel_x: int,
    pixel_y: int,
    sample_results: Sequence[SampleResult],
) -> PixelResult:
    render = scene.render
    fixed_radiance = sum(item.fixed_radiance for item in sample_results) / len(sample_results)
    float_radiance = sum(item.float_radiance for item in sample_results) / len(sample_results)
    depth_values = [item.depth for item in sample_results if item.depth >= 0.0]
    max_abs = max(item.max_abs_difference for item in sample_results)
    max_fixed_rounding = max(item.max_fixed_rounding_difference for item in sample_results)
    first_exceed: dict[str, Any] | None = None
    for sample_index, item in enumerate(sample_results):
        if item.first_exceed is None:
            continue
        candidate = dict(item.first_exceed)
        candidate["sample"] = sample_index
        if first_exceed is None or (candidate["step"], sample_index) < (
            first_exceed["step"],
            first_exceed["sample"],
        ):
            first_exceed = candidate
    fixed_stop_steps = [
        item.fixed_stop_step for item in sample_results if item.fixed_stop_step is not None
    ]
    float_stop_steps = [
        item.float_stop_step for item in sample_results if item.float_stop_step is not None
    ]
    return PixelResult(
        x=pixel_x,
        y=pixel_y,
        gray=_tone_map(fixed_radiance, render.exposure),
        reference_gray=_tone_map(float_radiance, render.exposure),
        depth=(sum(depth_values) / len(depth_values)) if depth_values else -1.0,
        max_abs_difference=max_abs,
        max_fixed_rounding_difference=max_fixed_rounding,
        # The final verdict is derived only from rounding inside the fixed path.
        # The float64 delta is separately exposed as a diagnostic.
        fixed_verdict="exceeded" if max_fixed_rounding > render.transmittance_tolerance else "pass",
        diagnostic_exceeded=max_abs > render.transmittance_tolerance,
        first_exceed=first_exceed,
        early_cutoff=any(item.early_cutoff for item in sample_results),
        fixed_stopped_by_cutoff=any(item.fixed_stopped_by_cutoff for item in sample_results),
        float_stopped_by_cutoff=any(item.float_stopped_by_cutoff for item in sample_results),
        fixed_stop_step=min(fixed_stop_steps, default=None),
        float_stop_step=min(float_stop_steps, default=None),
        samples=len(sample_results),
        steps=sum(item.steps for item in sample_results),
    )


def render_pixel(
    scene: Scene,
    pixel_x: int,
    pixel_y: int,
    *,
    collect_trace: bool,
) -> tuple[PixelResult, list[tuple[Any, ...]]]:
    sample_results: list[SampleResult] = []
    rows: list[tuple[Any, ...]] = []
    for sample_index, (offset_x, offset_y) in enumerate(scene.jitter.sample_offsets):
        origin, direction = _make_ray(scene, pixel_x, pixel_y, offset_x, offset_y)
        sample = trace_pixel_sample(
            scene,
            pixel_x,
            pixel_y,
            sample_index,
            origin,
            direction,
            collect_trace=collect_trace,
        )
        sample_results.append(sample)
        if collect_trace:
            rows.extend(sample.trace_rows)
    return _finalize_pixel(scene, pixel_x, pixel_y, sample_results), rows


@dataclass
class TileGeometry:
    index: int
    x0: int
    y0: int
    width: int
    height: int

    @property
    def pixel_count(self) -> int:
        return self.width * self.height


def tile_geometries(scene: Scene) -> list[TileGeometry]:
    render = scene.render
    tiles: list[TileGeometry] = []
    index = 0
    for y0 in range(0, render.height, render.tile_height):
        for x0 in range(0, render.width, render.tile_width):
            width = min(render.tile_width, render.width - x0)
            height = min(render.tile_height, render.height - y0)
            tiles.append(TileGeometry(index=index, x0=x0, y0=y0, width=width, height=height))
            index += 1
    return tiles


@dataclass
class TileResult:
    tile: TileGeometry
    pixels: list[PixelResult]
    trace_rows: list[tuple[Any, ...]]

    @property
    def gray(self) -> list[list[int]]:
        return [
            [self.pixels[y * self.tile.width + x].gray for x in range(self.tile.width)]
            for y in range(self.tile.height)
        ]

    @property
    def reference_gray(self) -> list[list[int]]:
        return [
            [self.pixels[y * self.tile.width + x].reference_gray for x in range(self.tile.width)]
            for y in range(self.tile.height)
        ]

    @property
    def depth(self) -> list[list[float]]:
        return [
            [self.pixels[y * self.tile.width + x].depth for x in range(self.tile.width)]
            for y in range(self.tile.height)
        ]


def render_tile(scene: Scene, tile: TileGeometry, *, collect_trace: bool) -> TileResult:
    pixels: list[PixelResult] = []
    trace_rows: list[tuple[Any, ...]] = []
    for local_y in range(tile.height):
        for local_x in range(tile.width):
            pixel, pixel_rows = render_pixel(
                scene,
                tile.x0 + local_x,
                tile.y0 + local_y,
                collect_trace=collect_trace,
            )
            pixels.append(pixel)
            if collect_trace:
                trace_rows.extend(pixel_rows)
    return TileResult(tile=tile, pixels=pixels, trace_rows=trace_rows)


@dataclass
class ImageResult:
    gray: list[list[int]]
    reference_gray: list[list[int]]
    depth: list[list[float]]
    pixels: list[PixelResult]
    trace_rows: list[tuple[Any, ...]]
    stats: dict[str, Any]


def _image_stats(scene: Scene, pixels: Sequence[PixelResult]) -> dict[str, Any]:
    total = len(pixels)
    fixed_pass = sum(1 for pixel in pixels if pixel.fixed_verdict == "pass")
    diagnostic_exceeded = sum(1 for pixel in pixels if pixel.diagnostic_exceeded)
    first_exceed = sum(1 for pixel in pixels if pixel.first_exceed is not None)
    early_cutoff = sum(1 for pixel in pixels if pixel.early_cutoff)
    fixed_cutoff = sum(1 for pixel in pixels if pixel.fixed_stopped_by_cutoff)
    max_abs = max((pixel.max_abs_difference for pixel in pixels), default=0.0)
    max_fixed = max((pixel.max_fixed_rounding_difference for pixel in pixels), default=0.0)
    gray_values = [pixel.gray for pixel in pixels]
    reference_values = [pixel.reference_gray for pixel in pixels]
    depth_values = [pixel.depth for pixel in pixels if pixel.depth >= 0.0]
    return {
        "pixels": total,
        "samples": sum(pixel.samples for pixel in pixels),
        "steps": sum(pixel.steps for pixel in pixels),
        "fixed_verdict_pass_pixels": fixed_pass,
        "fixed_verdict_exceeded_pixels": total - fixed_pass,
        "diagnostic_tolerance_exceeded_pixels": diagnostic_exceeded,
        "first_exceed_pixels": first_exceed,
        "early_cutoff_pixels": early_cutoff,
        "fixed_cutoff_pixels": fixed_cutoff,
        "max_abs_transmittance_difference": max_abs,
        "max_fixed_rounding_difference": max_fixed,
        "transmittance_tolerance": scene.render.transmittance_tolerance,
        "gray_min": min(gray_values, default=0),
        "gray_max": max(gray_values, default=0),
        "gray_mean": (sum(gray_values) / len(gray_values)) if gray_values else 0.0,
        "reference_gray_min": min(reference_values, default=0),
        "reference_gray_max": max(reference_values, default=0),
        "depth_min_world": min(depth_values) if depth_values else -1.0,
        "depth_max_world": max(depth_values) if depth_values else -1.0,
    }


def assemble_image(
    scene: Scene,
    tiles: Sequence[TileResult],
    *,
    collect_trace: bool,
    extra_stats: dict[str, Any] | None = None,
) -> ImageResult:
    render = scene.render
    gray = [[0 for _ in range(render.width)] for _ in range(render.height)]
    reference_gray = [[0 for _ in range(render.width)] for _ in range(render.height)]
    depth = [[-1.0 for _ in range(render.width)] for _ in range(render.height)]
    pixels: list[PixelResult] = []
    for tile in sorted(tiles, key=lambda item: item.tile.index):
        for local_y in range(tile.tile.height):
            for local_x in range(tile.tile.width):
                pixel = tile.pixels[local_y * tile.tile.width + local_x]
                gray[pixel.y][pixel.x] = pixel.gray
                reference_gray[pixel.y][pixel.x] = pixel.reference_gray
                depth[pixel.y][pixel.x] = pixel.depth
                pixels.append(pixel)
    stats = _image_stats(scene, pixels)
    if extra_stats:
        stats.update(extra_stats)
    trace_rows = sorted(
        (row for tile in tiles for row in tile.trace_rows),
        key=lambda row: (row[0], row[1], row[2], row[3]),
    )
    return ImageResult(
        gray=gray,
        reference_gray=reference_gray,
        depth=depth,
        pixels=pixels,
        trace_rows=trace_rows,
        stats=stats,
    )


def render_image_in_memory(
    scene: Scene,
    *,
    collect_trace: bool = False,
    token: Any | None = None,
) -> ImageResult:
    from .scheduler import render_blocks_in_memory

    return render_blocks_in_memory(scene, collect_trace=collect_trace, token=token)


def render_image_serial(scene: Scene, *, collect_trace: bool = False) -> ImageResult:
    tiles = [
        render_tile(scene, tile, collect_trace=collect_trace)
        for tile in tile_geometries(scene)
    ]
    return assemble_image(scene, tiles, collect_trace=collect_trace)

