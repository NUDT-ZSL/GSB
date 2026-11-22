"""Deterministic geometric alignment, gain correction, and feather fusion."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

import numpy as np

from .cache import ContentAddressedCache
from .config import AppConfig
from .errors import BusinessError, ErrorCode
from .formats import parse_text
from .geometry import check_homography, distort_normalized
from .injections import (
    AttributionLedger,
    InjectionSpec,
    apply_scene_injection,
    apply_text_injection,
)
from .model import SceneParameters, ViewParameters, sha256_json
from .resources import ResourcePlan, plan_resources


def _array_hash(array: np.ndarray, dtype: str) -> str:
    contiguous = np.ascontiguousarray(array, dtype=dtype)
    digest = hashlib.sha256()
    digest.update(contiguous.dtype.str.encode("ascii"))
    digest.update(repr(tuple(contiguous.shape)).encode("ascii"))
    digest.update(contiguous.tobytes(order="C"))
    return digest.hexdigest()


@dataclass(frozen=True)
class GeometryLayer:
    camera_id: int
    image: np.ndarray
    mask: np.ndarray
    margin: np.ndarray
    cache_key: str
    cache_hit: bool = False

    def hash(self) -> str:
        return sha256_json(
            {
                "camera_id": self.camera_id,
                "image_sha": _array_hash(self.image, "<f8"),
                "mask_sha": _array_hash(self.mask, "|b1"),
                "margin_sha": _array_hash(self.margin, "<f8"),
            }
        )


@dataclass(frozen=True)
class RenderResult:
    scene_hash: str
    panorama_rgb: np.ndarray
    panorama_float: np.ndarray
    valid_mask: np.ndarray
    weight_sum: np.ndarray
    weight_map: np.ndarray
    view_ids: tuple[int, ...]
    layers: tuple[GeometryLayer, ...]
    ledger: AttributionLedger
    resource_plan: ResourcePlan
    cache_report: dict[str, Any]
    interpolation: str
    skipped_views: tuple[int, ...]
    geometry_hash: str
    panorama_hash: str
    mask_hash: str
    weight_hash: str

    def report(self) -> dict[str, Any]:
        return {
            "scene_hash": self.scene_hash,
            "view_ids": list(self.view_ids),
            "skipped_views": list(self.skipped_views),
            "interpolation": self.interpolation,
            "canvas": {
                "width": int(self.panorama_rgb.shape[1]),
                "height": int(self.panorama_rgb.shape[0]),
            },
            "valid_pixels": int(self.valid_mask.sum()),
            "invalid_pixels": int((~self.valid_mask).sum()),
            "max_weight_sum_error": float(
                np.max(np.abs(self.weight_sum[self.valid_mask] - 1.0))
                if self.valid_mask.any()
                else 0.0
            ),
            "geometry_hash": self.geometry_hash,
            "panorama_hash": self.panorama_hash,
            "mask_hash": self.mask_hash,
            "weight_hash": self.weight_hash,
            "resources": {
                "estimated_memory_bytes": self.resource_plan.estimated_memory_bytes,
                "memory_limit_bytes": self.resource_plan.memory_limit_bytes,
                "requested_parallel_tasks": self.resource_plan.requested_parallel_tasks,
                "effective_parallel_tasks": self.resource_plan.effective_parallel_tasks,
            },
            "cache": self.cache_report,
            "attribution": self.ledger.report(),
        }


def _panorama_grid(width: int, height: int) -> tuple[np.ndarray, np.ndarray]:
    yy, xx = np.mgrid[0:height, 0:width]
    return xx.astype(np.float64), yy.astype(np.float64)


def _bilinear_sample(
    image: np.ndarray,
    source_x: np.ndarray,
    source_y: np.ndarray,
    valid: np.ndarray,
) -> np.ndarray:
    height, width, _ = image.shape
    x0 = np.floor(source_x).astype(np.int64)
    y0 = np.floor(source_y).astype(np.int64)
    x1 = np.clip(x0 + 1, 0, width - 1)
    y1 = np.clip(y0 + 1, 0, height - 1)
    x0 = np.clip(x0, 0, width - 1)
    y0 = np.clip(y0, 0, height - 1)
    wx = source_x - x0
    wy = source_y - y0
    wx = np.where(valid, wx, 0.0)
    wy = np.where(valid, wy, 0.0)
    top_left = image[y0, x0]
    top_right = image[y0, x1]
    bottom_left = image[y1, x0]
    bottom_right = image[y1, x1]
    top = top_left * (1.0 - wx[:, :, None]) + top_right * wx[:, :, None]
    bottom = bottom_left * (1.0 - wx[:, :, None]) + bottom_right * wx[:, :, None]
    result = top * (1.0 - wy[:, :, None]) + bottom * wy[:, :, None]
    return np.where(valid[:, :, None], result, 0.0)


def warp_view(
    scene: SceneParameters,
    view: ViewParameters,
    source_image: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Inverse-warp one view into panorama coordinates."""

    if scene.interpolation != "bilinear":
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            f"unsupported interpolation mode {scene.interpolation!r}",
            stage="parse",
            field_path="interpolation",
            details={"supported": ["bilinear"]},
        )
    source = np.asarray(source_image)
    if source.ndim != 3 or source.shape[2] != 3:
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "view image must have shape HxWx3",
            stage="parse",
            field_path=f"views[camera_id={view.camera_id}].image",
            details={"shape": list(source.shape)},
        )
    source = source.astype(np.float64, copy=False)
    if source.size and source.max() > 1.0:
        source = source / 255.0
    inverse = check_homography(view.homography)
    xx, yy = _panorama_grid(scene.canvas_width, scene.canvas_height)
    denominator = inverse[2, 0] * xx + inverse[2, 1] * yy + inverse[2, 2]
    safe_denominator = np.where(np.abs(denominator) < 1e-15, np.nan, denominator)
    undistorted_x = (
        inverse[0, 0] * xx + inverse[0, 1] * yy + inverse[0, 2]
    ) / safe_denominator
    undistorted_y = (
        inverse[1, 0] * xx + inverse[1, 1] * yy + inverse[1, 2]
    ) / safe_denominator
    normalized_x = (undistorted_x - view.principal_x) / view.focal
    normalized_y = (undistorted_y - view.principal_y) / view.focal
    distorted_x, distorted_y = distort_normalized(
        normalized_x, normalized_y, view.distortion_chain
    )
    source_x = distorted_x * view.focal + view.principal_x
    source_y = distorted_y * view.focal + view.principal_y
    source_height, source_width = source.shape[:2]
    valid = (
        np.isfinite(source_x)
        & np.isfinite(source_y)
        & (source_x >= 0.0)
        & (source_x <= source_width - 1)
        & (source_y >= 0.0)
        & (source_y <= source_height - 1)
    )
    sampled = _bilinear_sample(source, source_x, source_y, valid)
    if not np.isfinite(sampled).all():
        finite = np.isfinite(sampled).all(axis=2)
        valid &= finite
        sampled = np.where(np.isfinite(sampled), sampled, 0.0)
    margin = np.minimum.reduce(
        [
            source_x,
            source_y,
            (source_width - 1) - source_x,
            (source_height - 1) - source_y,
        ]
    )
    margin = np.where(valid, np.maximum(margin, 0.0), 0.0)
    sampled = np.where(valid[:, :, None], sampled, 0.0)
    return sampled, valid, margin


def _cache_key(
    scene: SceneParameters,
    view: ViewParameters,
    image_hash: str,
) -> str:
    return sha256_json(
        {
            "kind": "geometry-layer-v1",
            "scene_canvas": [scene.canvas_width, scene.canvas_height],
            "interpolation": scene.interpolation,
            "view": view.semantic_dict(),
            "image_sha": image_hash,
        }
    )


def _gain_fallback(view: ViewParameters) -> ViewParameters:
    return replace(
        view,
        exposure_gain=1.0,
        color_gain=(1.0, 1.0, 1.0),
        exposure_curve=replace(view.exposure_curve, gain=1.0, gamma=1.0),
    )


def _apply_gain(image: np.ndarray, view: ViewParameters) -> np.ndarray:
    result = np.clip(image, 0.0, 1.0)
    curve = view.exposure_curve
    gain = 1.0 if curve.mode == "unit" else curve.gain
    if curve.gamma != 1.0:
        result = np.power(result, 1.0 / curve.gamma)
    result = result / gain
    color_gain = np.asarray(view.color_gain, dtype=np.float64)
    result = result / color_gain[None, None, :]
    return np.clip(result, 0.0, 1.0)


def _positions(mask: np.ndarray, limit: int = 20) -> list[list[int]]:
    return [[int(y), int(x)] for y, x in np.argwhere(mask)[:limit]]


def validate_fusion(
    weight_sum: np.ndarray,
    expected_mask: np.ndarray,
    rgb: np.ndarray,
    tolerance: float,
    *,
    valid_mask: np.ndarray | None = None,
) -> None:
    valid = expected_mask if valid_mask is None else valid_mask
    gap = expected_mask & (weight_sum <= 0.0)
    if gap.any():
        raise BusinessError(
            ErrorCode.OVERLAP_GAP,
            "a covered panorama pixel has no contributing view",
            stage="blend",
            field_path="weight_sum",
            details={"positions": _positions(gap), "count": int(gap.sum())},
        )
    invalid_filled = (~valid) & np.any(np.asarray(rgb) != 0.0, axis=2)
    if invalid_filled.any():
        raise BusinessError(
            ErrorCode.INVALID_FILLED,
            "an invalid panorama pixel contains non-zero color data",
            stage="blend",
            field_path="panorama",
            details={
                "positions": _positions(invalid_filled),
                "count": int(invalid_filled.sum()),
            },
        )
    errors = np.abs(weight_sum[valid] - 1.0)
    if errors.size and float(errors.max()) > tolerance:
        bad = valid & (np.abs(weight_sum - 1.0) > tolerance)
        raise BusinessError(
            ErrorCode.WEIGHT_SUM,
            "normalized overlap weights do not sum to one",
            stage="blend",
            field_path="weight_sum",
            details={
                "positions": _positions(bad),
                "max_error": float(errors.max()),
                "tolerance": tolerance,
            },
        )


def _stage_injections(
    scene: SceneParameters,
    specs: Sequence[InjectionSpec],
    stage: str,
    ledger: AttributionLedger,
) -> SceneParameters:
    result = scene
    for spec in specs:
        if spec.stage != stage:
            continue
        result, expected_code = apply_scene_injection(result, spec)
        ledger.injection(
            spec,
            expected_code,
            context={"applied_to_stage": stage, "view_id": spec.view_id},
        )
    return result


def _record_business(
    ledger: AttributionLedger,
    error: BusinessError,
    *,
    context: dict[str, Any] | None = None,
    severity: str = "degraded",
) -> None:
    ledger.stage_error(error, context=context, severity=severity)


def render_scene(
    scene: SceneParameters,
    images: Mapping[int, np.ndarray],
    *,
    config: AppConfig | None = None,
    cache: ContentAddressedCache | None = None,
    injections: Sequence[InjectionSpec] = (),
    ledger: AttributionLedger | None = None,
) -> RenderResult:
    config = config or AppConfig()
    ledger = ledger or AttributionLedger()
    plan = plan_resources(
        scene, config.resources, config.render.requested_parallel_tasks
    )
    for notice in plan.notices:
        ledger.stage_code(
            notice["stage"],
            notice["code"],
            "resource limit was applied with degradation",
            context=notice["details"],
            severity="degraded",
        )
        ledger.degrade(
            notice["stage"],
            notice["category"],
            notice["upstream_code"],
            details=notice["details"],
        )
    cache = cache or ContentAddressedCache(
        config.cache_dir, config.resources.max_cache_entries
    )
    missing_images = sorted(
        view.camera_id for view in scene.views if view.camera_id not in images
    )
    if missing_images:
        error = BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "source image is missing for one or more views",
            stage="parse",
            field_path="images",
            details={"missing_camera_ids": missing_images},
        )
        _record_business(ledger, error, severity="fatal")
        raise error
    geometry_scene = _stage_injections(scene, injections, "geometry", ledger)
    layers: list[GeometryLayer] = []
    skipped: list[int] = []
    image_hashes = {
        camera_id: _array_hash(np.asarray(image), "<f8")
        for camera_id, image in images.items()
    }
    for view in geometry_scene.sorted_views:
        key = _cache_key(geometry_scene, view, image_hashes[view.camera_id])
        cached = cache.get(key)
        if cached is not None:
            layers.append(
                GeometryLayer(
                    view.camera_id,
                    cached.image,
                    cached.mask,
                    cached.margin,
                    key,
                    cache_hit=True,
                )
            )
            continue
        try:
            image, mask, margin = warp_view(
                geometry_scene, view, images[view.camera_id]
            )
        except BusinessError as error:
            _record_business(
                ledger,
                error,
                context={"camera_id": view.camera_id},
                severity="degraded",
            )
            ledger.degrade(
                "geometry",
                "skip_view",
                error.code,
                view_id=view.camera_id,
                details={"reason": "view omitted from downstream fusion"},
            )
            skipped.append(view.camera_id)
            continue
        write = cache.put(key, image, mask, margin)
        if write.evicted:
            ledger.stage_code(
                "resource",
                ErrorCode.RESOURCE_LIMIT,
                "cache capacity reached; deterministic oldest entries evicted",
                context={
                    "max_entries": cache.max_entries,
                    "evicted": list(write.evicted),
                },
            )
            ledger.degrade(
                "resource",
                "cache_entry_evicted",
                ErrorCode.RESOURCE_LIMIT,
                details={
                    "max_entries": cache.max_entries,
                    "evicted": list(write.evicted),
                },
            )
        layers.append(
            GeometryLayer(view.camera_id, image, mask, margin, key, False)
        )
    gain_scene = _stage_injections(geometry_scene, injections, "gain", ledger)
    gain_views = {view.camera_id: view for view in gain_scene.views}
    corrected_layers: list[np.ndarray] = []
    for layer in layers:
        view = gain_views[layer.camera_id]
        curve = view.exposure_curve
        if (
            curve.mode not in {"linear_gain", "unit"}
            or curve.gain <= 0.0
            or curve.gamma <= 0.0
            or any(value <= 0.0 for value in view.color_gain)
            or not np.isfinite(
                [curve.gain, curve.gamma, *view.color_gain]
            ).all()
        ):
            error = BusinessError(
                ErrorCode.GAIN_INVALID,
                "exposure or color gain is invalid",
                stage="gain",
                field_path=f"views[camera_id={view.camera_id}].exposure",
                details={
                    "gain": curve.gain,
                    "gamma": curve.gamma,
                    "color_gain": list(view.color_gain),
                    "mode": curve.mode,
                },
            )
            _record_business(
                ledger,
                error,
                context={"camera_id": view.camera_id},
                severity="degraded",
            )
            ledger.degrade(
                "gain",
                "unit_gain_fallback",
                error.code,
                view_id=view.camera_id,
                details={"fallback_gain": 1.0, "fallback_color_gain": [1, 1, 1]},
            )
            view = _gain_fallback(view)
        corrected_layers.append(_apply_gain(layer.image, view))
    blend_scene = _stage_injections(gain_scene, injections, "blend", ledger)
    feather_radius = blend_scene.feather_radius
    if feather_radius < 0.0 or not np.isfinite(feather_radius):
        error = BusinessError(
            ErrorCode.FEATHER_INVALID,
            "feather radius must be finite and non-negative",
            stage="blend",
            field_path="feather_radius",
            details={"feather_radius": feather_radius},
        )
        _record_business(ledger, error, severity="degraded")
        ledger.degrade(
            "blend",
            "radius_zero_fallback",
            error.code,
            details={"fallback_feather_radius": 0.0},
        )
        feather_radius = 0.0
    height = blend_scene.canvas_height
    width = blend_scene.canvas_width
    weight_stack = np.zeros((len(layers), height, width), dtype=np.float64)
    correction_views = {view.camera_id: view for view in blend_scene.views}
    for index, layer in enumerate(layers):
        if feather_radius <= 0.0:
            feather = np.ones((height, width), dtype=np.float64)
        else:
            feather = np.clip(layer.margin / feather_radius, 0.0, 1.0)
            feather = np.maximum(feather, 1e-12)
        overlap = max(0.0, correction_views[layer.camera_id].overlap_weight)
        weight_stack[index] = np.where(layer.mask, feather * overlap, 0.0)
    expected_mask = (
        np.logical_or.reduce([layer.mask for layer in layers])
        if layers
        else np.zeros((height, width), dtype=bool)
    )
    total_unchecked = weight_stack.sum(axis=0)
    covered_mask = expected_mask & (total_unchecked > 0.0)
    if (expected_mask & ~covered_mask).any():
        gap = expected_mask & ~covered_mask
        error = BusinessError(
            ErrorCode.OVERLAP_GAP,
            "a covered panorama pixel has no positive-weight view",
            stage="blend",
            field_path="weight_sum",
            details={"positions": _positions(gap), "count": int(gap.sum())},
        )
        _record_business(ledger, error, severity="fatal")
        raise error
    safe_total = np.where(total_unchecked > 0.0, total_unchecked, 1.0)
    normalized = weight_stack / safe_total[None, :, :]
    normalized = np.where(weight_stack > 0.0, normalized, 0.0)
    weight_sum = normalized.sum(axis=0)
    panorama_float = np.zeros((height, width, 3), dtype=np.float64)
    for index, layer in enumerate(corrected_layers):
        panorama_float += normalized[index][:, :, None] * layer
    panorama_float = np.where(covered_mask[:, :, None], panorama_float, 0.0)
    panorama_float = np.clip(panorama_float, 0.0, 1.0)
    try:
        validate_fusion(
            weight_sum,
            expected_mask,
            panorama_float,
            blend_scene.weight_tolerance,
            valid_mask=covered_mask,
        )
    except BusinessError as error:
        _record_business(ledger, error, severity="fatal")
        error.details["ledger"] = ledger.report()
        raise
    if config.render.require_full_coverage and (~covered_mask).any():
        error = BusinessError(
            ErrorCode.OVERLAP_GAP,
            "full panorama coverage is required but unavailable",
            stage="blend",
            field_path="panorama.valid_mask",
            details={
                "positions": _positions(~covered_mask),
                "count": int((~covered_mask).sum()),
            },
        )
        _record_business(ledger, error, severity="fatal")
        raise error
    panorama_rgb = np.floor(panorama_float * 255.0 + 0.5).astype(np.uint8)
    panorama_rgb = np.where(covered_mask[:, :, None], panorama_rgb, 0).astype(np.uint8)
    geometry_hash = sha256_json(
        {"layers": [layer.hash() for layer in layers], "skipped": skipped}
    )
    return RenderResult(
        scene_hash=blend_scene.semantic_hash,
        panorama_rgb=panorama_rgb,
        panorama_float=panorama_float,
        valid_mask=covered_mask,
        weight_sum=weight_sum,
        weight_map=normalized,
        view_ids=tuple(layer.camera_id for layer in layers),
        layers=tuple(layers),
        ledger=ledger,
        resource_plan=plan,
        cache_report=cache.report(),
        interpolation=blend_scene.interpolation,
        skipped_views=tuple(skipped),
        geometry_hash=geometry_hash,
        panorama_hash=_array_hash(panorama_rgb, "|u1"),
        mask_hash=_array_hash(covered_mask, "|b1"),
        weight_hash=_array_hash(weight_sum, "<f8"),
    )


def render_text(
    text: str,
    images: Mapping[int, np.ndarray],
    *,
    config: AppConfig | None = None,
    cache: ContentAddressedCache | None = None,
    injections: Sequence[InjectionSpec] = (),
) -> RenderResult:
    config = config or AppConfig()
    ledger = AttributionLedger()
    parse_specs = [spec for spec in injections if spec.stage == "parse"]
    modified = text
    for spec in parse_specs:
        modified = apply_text_injection(modified, spec)
    try:
        parsed = parse_text(modified, config.parse_defaults)
    except BusinessError as error:
        _record_business(ledger, error, severity="fatal")
        for spec in parse_specs:
            ledger.injection(
                spec,
                error.code,
                context={
                    "field_path": error.field_path,
                    "char_offset": error.char_offset,
                    "line": error.line,
                    "column": error.column,
                },
            )
        error.details["ledger"] = ledger.report()
        raise
    for spec in parse_specs:
        ledger.injection(
            spec,
            "E_INJECTION_DID_NOT_TRIGGER",
            context={"applied": False},
            applied=False,
        )
    try:
        return render_scene(
            parsed.scene,
            images,
            config=config,
            cache=cache,
            injections=injections,
            ledger=ledger,
        )
    except BusinessError as error:
        if not any(
            record.stage == error.stage and record.code == error.code
            for record in ledger.stage_records
        ):
            ledger.stage_error(error, severity="fatal")
        error.details["ledger"] = ledger.report()
        raise
    except Exception as error:
        ledger.unattributed_error(
            "render",
            error,
            context={"scene_hash": parsed.scene.semantic_hash},
        )
        wrapped = BusinessError(
            ErrorCode.UNATTRIBUTED,
            "an unexpected exception was not attributed to a pipeline stage",
            stage="unattributed",
            details={
                "exception_type": type(error).__name__,
                "message": str(error),
                "ledger": ledger.report(),
            },
        )
        raise wrapped from error

