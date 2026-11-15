"""Deterministic CPU reference ray caster and error report generation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import numpy as np

from .camera import Camera, validate_ray_directions
from .config import SamplingConfig
from .errors import BusinessCode, VolumeRenderError
from .raycast import intersect_aabb, linear_to_srgb, trilinear_sample
from .transfer_function import TransferFunction, TransferFunctionLUT
from .volume import Volume


FLOAT32_EPSILON = float(np.finfo(np.float32).eps)
FLOAT64_EPSILON = float(np.finfo(np.float64).eps)


def hash_rgba(buffer: np.ndarray) -> str:
    """SHA-256 of a C-contiguous little-endian float32 RGBA buffer."""

    array = np.asarray(buffer)
    if array.dtype != np.float32:
        array = array.astype(np.float32)
    array = np.ascontiguousarray(array.astype("<f4", copy=False))
    return hashlib.sha256(array.tobytes(order="C")).hexdigest()


def rgba_to_uint8_srgb(rgba_linear: np.ndarray) -> np.ndarray:
    encoded = np.array(rgba_linear, dtype=np.float64, copy=True)
    encoded[..., :3] = linear_to_srgb(encoded[..., :3])
    encoded = np.clip(np.floor(encoded * 255.0 + 0.5), 0.0, 255.0)
    return encoded.astype(np.uint8)


def _stats(values: np.ndarray) -> dict[str, float]:
    array = np.asarray(values, dtype=np.float64).reshape(-1)
    if array.size == 0:
        return {"min": 0.0, "max": 0.0, "mean": 0.0, "p95": 0.0}
    return {
        "min": float(np.min(array)),
        "max": float(np.max(array)),
        "mean": float(np.mean(array)),
        "p95": float(np.percentile(array, 95.0, method="linear")),
    }


@dataclass
class RenderResult:
    rgba_linear: np.ndarray
    rgba_srgb: np.ndarray
    report: dict[str, Any]
    business_code: BusinessCode

    @property
    def ok(self) -> bool:
        return self.business_code == BusinessCode.OK

    @property
    def rgba(self) -> np.ndarray:
        return self.rgba_srgb if self.report["outputs"]["srgb_selected"] else self.rgba_linear

    @property
    def rgba_sha256(self) -> str:
        return self.report["outputs"]["rgba_linear_sha256"]

    @property
    def rgba_srgb_sha256(self) -> str:
        return self.report["outputs"]["rgba_srgb_sha256"]

    def image_uint8(self) -> np.ndarray:
        return rgba_to_uint8_srgb(self.rgba_linear)

    def to_envelope(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "ok": self.ok,
            "status": self.business_code.value,
            "business_code": self.business_code.value,
            "report": self.report,
        }

    def report_json(self) -> str:
        return json.dumps(self.to_envelope(), sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass
class _RayState:
    ids: np.ndarray
    t_enter: np.ndarray
    block_width: np.ndarray
    block_count: np.ndarray
    block_index: np.ndarray
    sub_index: np.ndarray
    subdivisions: np.ndarray
    color: np.ndarray
    transmittance: np.ndarray
    max_local_error: np.ndarray
    accumulated_error: np.ndarray
    rounding_bound: np.ndarray
    sample_count: np.ndarray

    def compact(self, keep: np.ndarray) -> None:
        self.ids = self.ids[keep]
        self.t_enter = self.t_enter[keep]
        self.block_width = self.block_width[keep]
        self.block_count = self.block_count[keep]
        self.block_index = self.block_index[keep]
        self.sub_index = self.sub_index[keep]
        self.subdivisions = self.subdivisions[keep]
        self.color = self.color[keep]
        self.transmittance = self.transmittance[keep]
        self.max_local_error = self.max_local_error[keep]
        self.accumulated_error = self.accumulated_error[keep]
        self.rounding_bound = self.rounding_bound[keep]
        self.sample_count = self.sample_count[keep]


def _make_state(
    ids: np.ndarray,
    t_enter: np.ndarray,
    t_exit: np.ndarray,
    base_step: float,
) -> _RayState:
    lengths = t_exit - t_enter
    counts = np.maximum(1, np.ceil(lengths / base_step - 1.0e-12).astype(np.int64))
    widths = lengths / counts.astype(np.float64)
    size = ids.size
    return _RayState(
        ids=ids,
        t_enter=t_enter,
        block_width=widths,
        block_count=counts,
        block_index=np.zeros(size, dtype=np.int64),
        sub_index=np.zeros(size, dtype=np.int64),
        subdivisions=np.zeros(size, dtype=np.int64),
        color=np.zeros((size, 3), dtype=np.float64),
        transmittance=np.ones(size, dtype=np.float64),
        max_local_error=np.zeros(size, dtype=np.float64),
        accumulated_error=np.zeros(size, dtype=np.float64),
        rounding_bound=np.zeros(size, dtype=np.float64),
        sample_count=np.zeros(size, dtype=np.int64),
    )


class VolumeRenderer:
    """Single-threaded reference implementation of the volume ray casting pipeline."""

    def __init__(
        self,
        volume: Volume,
        camera: Camera,
        transfer_function: TransferFunction,
        config: SamplingConfig,
    ) -> None:
        self.volume = volume
        self.camera = camera
        self.transfer_function = transfer_function
        self.config = config
        minimum_step = 0.1 * volume.min_spacing
        finest_step = config.base_step / float(1 << config.max_refinement) if config.adaptive else config.base_step
        if config.base_step < minimum_step - 1.0e-15:
            raise VolumeRenderError(
                BusinessCode.E_STEP_TOO_SMALL,
                "base_step is smaller than 0.1 times the minimum voxel spacing",
                details={
                    "base_step": config.base_step,
                    "minimum_voxel_spacing": volume.min_spacing,
                    "minimum_allowed_step": minimum_step,
                },
            )
        if config.adaptive and finest_step < minimum_step - 1.0e-15:
            raise VolumeRenderError(
                BusinessCode.E_STEP_TOO_SMALL,
                "adaptive subdivision would create a step smaller than 0.1 times the minimum voxel spacing",
                details={
                    "finest_step": finest_step,
                    "minimum_voxel_spacing": volume.min_spacing,
                    "minimum_allowed_step": minimum_step,
                    "max_refinement": config.max_refinement,
                },
            )
        self.lut = transfer_function.build_lut()

    def render(self, width: int, height: int) -> RenderResult:
        if width <= 0 or height <= 0:
            raise VolumeRenderError(
                BusinessCode.E_RAY_DIR,
                "Render resolution must be positive",
                details={"width": width, "height": height},
            )
        directions = self.camera.ray_directions(width, height)
        validate_ray_directions(directions)
        origins = np.broadcast_to(np.asarray(self.camera.position, dtype=np.float64), directions.shape).copy()
        bounds_min = self.volume.bounds_min
        bounds_max = self.volume.bounds_max
        t_enter, t_exit, hit = intersect_aabb(origins, directions, bounds_min, bounds_max)
        hit_ids = np.flatnonzero(hit)

        rgba_linear = np.zeros((height, width, 4), dtype=np.float32)
        pixel_error = np.zeros(height * width, dtype=np.float64)
        accumulated_error_map = np.zeros(height * width, dtype=np.float64)
        max_local_error_map = np.zeros(height * width, dtype=np.float64)
        float_error_map = np.zeros(height * width, dtype=np.float64)
        float64_error_map = np.zeros(height * width, dtype=np.float64)
        sample_count_map = np.zeros(height * width, dtype=np.int64)
        rgba_flat = rgba_linear.reshape(-1, 4)

        traversal_stats: dict[str, float | int] = {
            "min_step_world": 0.0,
            "max_step_world": 0.0,
            "step_sum_world": 0.0,
            "sample_total": 0,
        }
        if hit_ids.size:
            state = _make_state(
                hit_ids,
                t_enter[hit_ids],
                t_exit[hit_ids],
                self.config.base_step,
            )
            if self.config.adaptive:
                traversal_stats = self._render_blocks(
                    state,
                    directions,
                    origins,
                    rgba_flat,
                    accumulated_error_map,
                    max_local_error_map,
                    float_error_map,
                    float64_error_map,
                    sample_count_map,
                    adaptive=True,
                )
            else:
                traversal_stats = self._render_blocks(
                    state,
                    directions,
                    origins,
                    rgba_flat,
                    accumulated_error_map,
                    max_local_error_map,
                    float_error_map,
                    float64_error_map,
                    sample_count_map,
                    adaptive=False,
                )

        pixel_error = accumulated_error_map + float_error_map + float64_error_map

        rgba_srgb = rgba_linear.copy()
        rgba_srgb[..., :3] = linear_to_srgb(rgba_linear[..., :3]).astype(np.float32)
        rgba_linear = np.ascontiguousarray(rgba_linear)
        rgba_srgb = np.ascontiguousarray(rgba_srgb)

        total_pixels = width * height
        over_limit = 0
        over_ratio = 0.0
        if self.config.error_threshold is not None:
            exceeded = pixel_error > self.config.error_threshold
            over_limit = int(np.count_nonzero(exceeded))
            over_ratio = over_limit / float(total_pixels)
        business_code = (
            BusinessCode.E_ERROR_BOUND
            if self.config.error_threshold is not None and over_limit > 0
            else BusinessCode.OK
        )

        report = self._build_report(
            width,
            height,
            rgba_linear,
            rgba_srgb,
            hit,
            t_enter,
            t_exit,
            pixel_error,
            accumulated_error_map,
            max_local_error_map,
            float_error_map,
            float64_error_map,
            sample_count_map,
            traversal_stats,
            over_limit,
            over_ratio,
            business_code,
        )
        return RenderResult(rgba_linear, rgba_srgb, report, business_code)

    def render_safe(self, width: int, height: int) -> tuple[RenderResult | None, dict[str, Any]]:
        try:
            result = self.render(width, height)
            return result, result.to_envelope()
        except VolumeRenderError as exc:
            return None, exc.to_dict()

    def _render_blocks(
        self,
        state: _RayState,
        directions_all: np.ndarray,
        origins_all: np.ndarray,
        rgba_flat: np.ndarray,
        accumulated_error_map: np.ndarray,
        max_local_error_map: np.ndarray,
        float_error_map: np.ndarray,
        float64_error_map: np.ndarray,
        sample_count_map: np.ndarray,
        *,
        adaptive: bool,
    ) -> dict[str, float | int]:
        early_threshold = self.config.early_termination_threshold
        min_step = float("inf")
        max_step = 0.0
        step_sum = 0.0
        sample_total = 0

        while state.ids.size:
            directions = directions_all[state.ids]
            origins = origins_all[state.ids]

            if adaptive:
                needs_prepare = state.subdivisions == 0
                if np.any(needs_prepare):
                    prepare_ids = np.flatnonzero(needs_prepare)
                    probe_t = (
                        state.t_enter[prepare_ids]
                        + (state.block_index[prepare_ids].astype(np.float64) + 0.5) * state.block_width[prepare_ids]
                    )
                    probe_positions = origins[prepare_ids] + directions[prepare_ids] * probe_t[:, None]
                    probe_values, _, _, _ = trilinear_sample(
                        self.volume.data,
                        self.volume.bounds_min,
                        np.asarray(self.volume.spacing, dtype=np.float64),
                        probe_positions,
                    )
                    indices = self.lut.lookup_indices(probe_values)
                    gradient_norm = self.lut.gradient_norm[indices].astype(np.float64)
                    if self.lut.max_gradient_norm > 0.0:
                        ratio = gradient_norm / self.lut.max_gradient_norm
                        levels = np.zeros_like(ratio, dtype=np.float64)
                        active = ratio >= self.config.gradient_threshold
                        levels[active] = np.maximum(1.0, np.ceil(np.log2(ratio[active] / self.config.gradient_threshold)))
                        levels = np.minimum(levels, float(self.config.max_refinement)).astype(np.int64)
                    else:
                        levels = np.zeros(prepare_ids.size, dtype=np.int64)
                    state.subdivisions[prepare_ids] = 1 << levels
                    state.sub_index[prepare_ids] = 0

                relative_t = (
                    state.block_index.astype(np.float64)
                    + (state.sub_index.astype(np.float64) + 0.5) / state.subdivisions.astype(np.float64)
                )
                step = state.block_width / state.subdivisions.astype(np.float64)
            else:
                relative_t = state.block_index.astype(np.float64) + 0.5
                step = state.block_width

            t = state.t_enter + relative_t * state.block_width
            positions = origins + directions * t[:, None]
            values, gx, gy, gz = trilinear_sample(
                self.volume.data,
                self.volume.bounds_min,
                np.asarray(self.volume.spacing, dtype=np.float64),
                positions,
            )
            lut_indices = self.lut.lookup_indices(values)
            rgba = self.lut.rgba[lut_indices].astype(np.float64)
            gradient_l1 = self.lut.gradient_l1[lut_indices].astype(np.float64)

            gradient_norm = np.sqrt(gx * gx + gy * gy + gz * gz)
            dc_bound = 0.5 * step * gradient_norm
            local_error = gradient_l1 * dc_bound
            trans_before = state.transmittance
            opacity = rgba[:, 3]
            contribution_rgb = trans_before[:, None] * opacity[:, None] * rgba[:, :3]
            contribution_alpha = trans_before * opacity
            rounding_increment = np.sum(np.abs(contribution_rgb), axis=1) + np.abs(contribution_alpha)

            state.color += contribution_rgb
            state.transmittance = trans_before * (1.0 - opacity)
            state.max_local_error = np.maximum(state.max_local_error, local_error)
            state.accumulated_error += trans_before * local_error
            state.rounding_bound += rounding_increment
            state.sample_count += 1
            min_step = min(min_step, float(np.min(step)))
            max_step = max(max_step, float(np.max(step)))
            step_sum += float(np.sum(step))
            sample_total += int(step.size)

            if adaptive:
                state.sub_index += 1
                block_complete = state.sub_index >= state.subdivisions
                state.block_index = state.block_index + block_complete.astype(np.int64)
                state.sub_index[block_complete] = 0
                state.subdivisions[block_complete] = 0
            else:
                state.block_index += 1

            finished = state.block_index >= state.block_count
            if early_threshold is not None:
                terminated = state.transmittance <= early_threshold
                state.transmittance[terminated] = 0.0
                finished |= terminated

            if np.any(finished):
                done_ids = state.ids[finished]
                rgba_flat[done_ids, :3] = state.color[finished].astype(np.float32)
                rgba_flat[done_ids, 3] = (1.0 - state.transmittance[finished]).astype(np.float32)
                accumulated_error_map[done_ids] = state.accumulated_error[finished]
                max_local_error_map[done_ids] = state.max_local_error[finished]
                float_error_map[done_ids] = 0.5 * FLOAT32_EPSILON * state.rounding_bound[finished]
                float64_error_map[done_ids] = FLOAT64_EPSILON * state.sample_count[finished] * state.rounding_bound[finished]
                sample_count_map[done_ids] = state.sample_count[finished]

            keep = ~finished
            if state.ids.size:
                state.compact(keep)

        return {
            "min_step_world": 0.0 if not np.isfinite(min_step) else min_step,
            "max_step_world": max_step,
            "step_sum_world": step_sum,
            "sample_total": sample_total,
        }

    def _build_report(
        self,
        width: int,
        height: int,
        rgba_linear: np.ndarray,
        rgba_srgb: np.ndarray,
        hit: np.ndarray,
        t_enter: np.ndarray,
        t_exit: np.ndarray,
        pixel_error: np.ndarray,
        accumulated_error: np.ndarray,
        max_local_error: np.ndarray,
        float_error: np.ndarray,
        float64_error: np.ndarray,
        sample_count: np.ndarray,
        traversal_stats: dict[str, float | int],
        over_limit: int,
        over_ratio: float,
        business_code: BusinessCode,
    ) -> dict[str, Any]:
        hit_count = int(np.count_nonzero(hit))
        hit_lengths = (t_exit[hit] - t_enter[hit]).astype(np.float64)
        hit_samples = sample_count[hit].astype(np.float64)
        total_samples = int(np.sum(sample_count, dtype=np.int64))
        sample_total = int(traversal_stats["sample_total"])
        step_stats = {
            "actual_min_world": float(traversal_stats["min_step_world"]),
            "actual_mean_world": (
                float(traversal_stats["step_sum_world"]) / sample_total if sample_total else 0.0
            ),
            "actual_max_world": float(traversal_stats["max_step_world"]),
        }

        spacing_min = self.volume.min_spacing
        total_pixels = width * height
        report: dict[str, Any] = {
            "schema_version": "1.0.0",
            "renderer": "medvol-raycast/0.1.0",
            "backend": "cpu_reference",
            "status": business_code.value,
            "business_code": business_code.value,
            "outputs": {
                "width": width,
                "height": height,
                "dtype": "float32",
                "layout": "RGBA",
                "linear_color_space": "linear-sRGB",
                "row_order": "top-to-bottom",
                "srgb_selected": self.config.srgb,
                "rgba_linear_sha256": hash_rgba(rgba_linear),
                "rgba_srgb_sha256": hash_rgba(rgba_srgb),
            },
            "config": {
                "sampling": self.config.metadata(),
                "camera": self.camera.metadata(),
                "volume": self.volume.metadata(),
                "transfer_function": self.transfer_function.metadata(),
                "float32_epsilon": FLOAT32_EPSILON,
                "determinism": {
                    "random_seed": self.config.random_seed,
                    "jitter_enabled": False,
                    "threading": "single-threaded NumPy ufunc sequence",
                    "accumulation_precision": "float64 interior, one deterministic float32 cast",
                    "hash_covers": "float32 RGBA linear buffer in C order",
                },
            },
            "sampling": {
                "hit_rays": hit_count,
                "total_rays": total_pixels,
                "total_samples": total_samples,
                "samples_per_hit_ray": _stats(hit_samples),
                "samples_per_pixel_including_background": _stats(sample_count.astype(np.float64)),
                "hit_ray_length_world": _stats(hit_lengths),
                "requested_base_step_world": self.config.base_step,
                "requested_base_step_min_voxels": self.config.base_step / spacing_min,
                "effective_finest_step_world": self.config.finest_step if self.config.adaptive else self.config.base_step,
                "step_world": step_stats,
                "step_voxels": {
                    "requested_base": self.config.base_step / spacing_min,
                    "actual_min": step_stats["actual_min_world"] / spacing_min,
                    "actual_mean": step_stats["actual_mean_world"] / spacing_min,
                },
                "adaptive": {
                    "enabled": self.config.adaptive,
                    "max_refinement": self.config.max_refinement,
                    "gradient_threshold_normalized": self.config.gradient_threshold,
                    "decision_metric": "max-normalized LUT RGBA gradient norm at each base block midpoint",
                },
                "early_termination_threshold": self.config.early_termination_threshold,
            },
            "error_analysis": {
                "definition_version": "trilinear-gradient-lipschitz/1",
                "method": (
                    "Analytic trilinear derivatives give |grad(c)|. Each sample bounds scalar variation "
                    "over a half step by 0.5*h*|grad(c)|; the LUT RGBA gradient L1 norm maps this through "
                    "the nonlinear transfer function. Contributions are accumulated with transmittance. "
                    "A separate float32 cast bound is reported."
                ),
                "total_error": _stats(pixel_error),
                "accumulated_error": _stats(accumulated_error),
                "max_local_error": _stats(max_local_error),
                "float32_cast_error_bound": _stats(float_error),
                "float64_accumulation_error_bound": _stats(float64_error),
                "error_threshold": self.config.error_threshold,
                "exceeded_pixel_count": over_limit,
                "exceeded_pixel_ratio": over_ratio,
                "threshold_exceeded": over_limit > 0,
            },
            "integration": {
                "fbo_texture_available": False,
                "rgba_buffer": "owned NumPy array",
                "note": "The CPU reference buffer can be uploaded to an external UI texture.",
            },
        }
        return report
