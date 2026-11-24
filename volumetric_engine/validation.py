"""Strict JSON scene parsing and business-code validation."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from .errors import EngineError, error
from .model import (
    Camera,
    JitterConfig,
    MediaBox,
    PointLight,
    RenderConfig,
    Scene,
    Vec3,
    _normalize,
    _sub,
)
from .memory import tile_buffer_bytes
from .util import finite_number, read_json


def _require_object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise error("E_SCHEMA", f"{path} must be an object", path)
    return value


def _require_array(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise error("E_SCHEMA", f"{path} must be an array", path)
    return value


def _require_str(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise error("E_SCHEMA", f"{path} must be a non-empty string", path)
    return value


def _require_number(value: Any, path: str, code: str = "E_SCHEMA") -> float:
    if not finite_number(value):
        raise error(code, f"{path} must be a finite number", path)
    return float(value)


def _require_int(value: Any, path: str, code: str = "E_SCHEMA", positive: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise error(code, f"{path} must be an integer", path)
    if positive and value <= 0:
        raise error(code, f"{path} must be a positive integer", path)
    return value


def _require_vec3(value: Any, path: str) -> Vec3:
    values = _require_array(value, path)
    if len(values) != 3:
        raise error("E_SCHEMA", f"{path} must contain exactly three numbers", path)
    return tuple(_require_number(item, f"{path}[{index}]") for index, item in enumerate(values))  # type: ignore[return-value]


def _positive_vec3(value: Any, path: str, code: str) -> Vec3:
    result = _require_vec3(value, path)
    if any(component <= 0.0 for component in result):
        raise error(code, f"{path} components must all be finite positive numbers", path)
    return result


def _nonnegative_vec3(value: Any, path: str, code: str) -> Vec3:
    result = _require_vec3(value, path)
    if any(component < 0.0 or component > 1.0 for component in result):
        raise error(code, f"{path} components must be in [0, 1]", path)
    return result


def _scalar_or_vec3(value: Any, path: str, code: str, *, positive: bool = False) -> Vec3:
    if finite_number(value):
        component = float(value)
        result = (component, component, component)
    else:
        result = _require_vec3(value, path)
    if positive and any(component <= 0.0 for component in result):
        raise error(code, f"{path} must be finite and positive", path)
    if not positive and any(component < 0.0 or component > 1.0 for component in result):
        raise error(code, f"{path} components must be in [0, 1]", path)
    return result


def _load_data_file(scene_path: Path, value: Any, path: str) -> Any:
    name = _require_str(value, path)
    candidate = Path(name)
    if not candidate.is_absolute():
        candidate = scene_path.parent / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise error("E_IO", f"cannot resolve {candidate}: {exc}", path) from exc
    return read_json(resolved)


def _parse_media(scene_path: Path, root: dict[str, Any]) -> tuple[MediaBox, ...]:
    data_ref = root.get("data")
    if isinstance(data_ref, dict) and "media" in data_ref:
        raw = _load_data_file(scene_path, data_ref["media"], "data.media")
    else:
        raw = root.get("media")
    if isinstance(raw, dict):
        raw = raw.get("media")
    raw_items = _require_array(raw, "media")
    if not raw_items:
        raise error("E_MEDIA", "at least one medium box is required", "media")

    media: list[MediaBox] = []
    seen_ids: set[str] = set()
    for index, raw_item in enumerate(raw_items):
        item = _require_object(raw_item, f"media[{index}]")
        medium_id = _require_str(item.get("id"), f"media[{index}].id")
        if medium_id in seen_ids:
            raise error("E_MEDIA", f"duplicate medium id {medium_id!r}", f"media[{index}].id")
        seen_ids.add(medium_id)
        kind = item.get("kind", "medium")
        if not isinstance(kind, str) or not kind:
            raise error("E_SCHEMA", f"media[{index}].kind must be a non-empty string", f"media[{index}].kind")
        center = _require_vec3(item.get("center"), f"media[{index}].center")
        size = _positive_vec3(item.get("size"), f"media[{index}].size", "E_MEDIA_SIZE")
        extinction = _scalar_or_vec3(
            item.get("extinction"), f"media[{index}].extinction", "E_EXTINCTION", positive=True
        )
        albedo = _scalar_or_vec3(item.get("albedo"), f"media[{index}].albedo", "E_ALBEDO")
        ior = _require_number(item.get("ior"), f"media[{index}].ior", "E_IOR")
        if ior < 1.0:
            raise error("E_IOR", f"media[{index}].ior must be >= 1", f"media[{index}].ior")
        media.append(
            MediaBox(
                id=medium_id,
                kind=kind,
                center=center,
                size=size,
                extinction=extinction,
                albedo=albedo,
                ior=ior,
                declaration_index=index,
            )
        )
    return tuple(media)


def _parse_lights(scene_path: Path, root: dict[str, Any], bounds_min: Vec3, bounds_max: Vec3) -> tuple[PointLight, ...]:
    data_ref = root.get("data")
    if isinstance(data_ref, dict) and "lights" in data_ref:
        raw = _load_data_file(scene_path, data_ref["lights"], "data.lights")
    else:
        raw = root.get("lights")
    if isinstance(raw, dict):
        raw = raw.get("lights")
    raw_items = _require_array(raw, "lights")
    if not raw_items:
        raise error("E_LIGHT", "at least one point light is required", "lights")

    lights: list[PointLight] = []
    seen_ids: set[str] = set()
    for index, raw_item in enumerate(raw_items):
        item = _require_object(raw_item, f"lights[{index}]")
        light_id = _require_str(item.get("id"), f"lights[{index}].id")
        if light_id in seen_ids:
            raise error("E_LIGHT", f"duplicate light id {light_id!r}", f"lights[{index}].id")
        seen_ids.add(light_id)
        position = _require_vec3(item.get("position"), f"lights[{index}].position")
        if any(
            position[axis] < bounds_min[axis] or position[axis] > bounds_max[axis]
            for axis in range(3)
        ):
            raise error(
                "E_LIGHT_POS",
                f"lights[{index}] is outside the scene bounding box",
                f"lights[{index}].position",
                bounds_min=list(bounds_min),
                bounds_max=list(bounds_max),
            )
        intensity = _require_number(item.get("intensity"), f"lights[{index}].intensity", "E_LIGHT_INTENSITY")
        if intensity <= 0.0:
            raise error(
                "E_LIGHT_INTENSITY",
                f"lights[{index}].intensity must be positive",
                f"lights[{index}].intensity",
            )
        lights.append(PointLight(id=light_id, position=position, intensity=intensity))
    return tuple(lights)


def _parse_jitter(scene_path: Path, root: dict[str, Any], samples_per_pixel: int) -> JitterConfig:
    data_ref = root.get("data")
    if isinstance(data_ref, dict) and "jitter" in data_ref:
        raw = _load_data_file(scene_path, data_ref["jitter"], "data.jitter")
    else:
        raw = root.get("jitter")
    payload = _require_object(raw, "jitter")
    offsets_raw = _require_array(payload.get("sample_offsets"), "jitter.sample_offsets")
    if len(offsets_raw) != samples_per_pixel:
        raise error(
            "E_JITTER",
            "jitter.sample_offsets length must equal samples_per_pixel",
            "jitter.sample_offsets",
            expected=samples_per_pixel,
            actual=len(offsets_raw),
        )
    offsets: list[tuple[float, float]] = []
    for index, raw_offset in enumerate(offsets_raw):
        pair = _require_array(raw_offset, f"jitter.sample_offsets[{index}]")
        if len(pair) != 2:
            raise error("E_JITTER", "each jitter offset must have two components", f"jitter.sample_offsets[{index}]")
        x = _require_number(pair[0], f"jitter.sample_offsets[{index}][0]", "E_JITTER")
        y = _require_number(pair[1], f"jitter.sample_offsets[{index}][1]", "E_JITTER")
        if not (0.0 <= x < 1.0 and 0.0 <= y < 1.0):
            raise error("E_JITTER", "jitter offsets must be in [0, 1)", f"jitter.sample_offsets[{index}]")
        offsets.append((x, y))

    def parse_scales(key: str) -> tuple[float, ...]:
        values = _require_array(payload.get(key), f"jitter.{key}")
        if not values:
            raise error("E_JITTER", f"jitter.{key} must not be empty", f"jitter.{key}")
        parsed: list[float] = []
        for index, item in enumerate(values):
            number = _require_number(item, f"jitter.{key}[{index}]", "E_JITTER")
            if number <= 0.0:
                raise error("E_JITTER", f"jitter.{key}[{index}] must be positive", f"jitter.{key}[{index}]")
            parsed.append(number)
        return tuple(parsed)

    return JitterConfig(
        sample_offsets=tuple(offsets),
        step_scales=parse_scales("step_scales"),
        cutoff_scales=parse_scales("cutoff_scales"),
    )


def load_scene(scene_path: str | Path) -> Scene:
    path = Path(scene_path)
    try:
        path = path.resolve()
    except OSError as exc:
        raise error("E_IO", f"cannot resolve scene path: {exc}", str(scene_path)) from exc
    root = _require_object(read_json(path, "E_JSON"), "$")
    schema_version = root.get("schema_version", 1)
    if schema_version not in (1, "1", "1.0"):
        raise error("E_SCHEMA", f"unsupported schema_version {schema_version!r}", "schema_version")

    name = root.get("name", path.stem)
    if not isinstance(name, str) or not name:
        raise error("E_SCHEMA", "name must be a non-empty string", "name")
    seed = root.get("seed")
    if seed is not None:
        seed = _require_int(seed, "seed")

    bounds = _require_object(root.get("bounds"), "bounds")
    bounds_min = _require_vec3(bounds.get("min"), "bounds.min")
    bounds_max = _require_vec3(bounds.get("max"), "bounds.max")
    if any(bounds_min[axis] >= bounds_max[axis] for axis in range(3)):
        raise error("E_SCHEMA", "bounds.min must be strictly smaller than bounds.max on every axis", "bounds")

    camera_raw = _require_object(root.get("camera"), "camera")
    position = _require_vec3(camera_raw.get("position"), "camera.position")
    if "forward" in camera_raw:
        forward_raw = _require_vec3(camera_raw["forward"], "camera.forward")
        if all(component == 0.0 for component in forward_raw):
            raise error("E_SCHEMA", "camera.forward must be nonzero", "camera.forward")
        forward = _normalize(forward_raw)
    elif "target" in camera_raw:
        target = _require_vec3(camera_raw["target"], "camera.target")
        delta = _sub(target, position)
        if all(component == 0.0 for component in delta):
            raise error("E_SCHEMA", "camera.target must differ from camera.position", "camera.target")
        forward = _normalize(delta)
    else:
        raise error("E_SCHEMA", "camera requires forward or target", "camera")
    up_raw = _require_vec3(camera_raw.get("up", [0.0, 1.0, 0.0]), "camera.up")
    if all(component == 0.0 for component in up_raw):
        raise error("E_SCHEMA", "camera.up must be nonzero", "camera.up")
    up = _normalize(up_raw)
    if abs(sum(forward[i] * up[i] for i in range(3))) > 0.999999:
        raise error("E_SCHEMA", "camera.up must not be parallel to camera.forward", "camera.up")
    fov = _require_number(camera_raw.get("vertical_fov_deg"), "camera.vertical_fov_deg", "E_FOV")
    if not (0.0 < fov < 180.0):
        raise error("E_FOV", "camera.vertical_fov_deg must be in (0, 180)", "camera.vertical_fov_deg")
    aspect = _require_number(camera_raw.get("aspect_ratio"), "camera.aspect_ratio", "E_ASPECT")
    if aspect <= 0.0:
        raise error("E_ASPECT", "camera.aspect_ratio must be finite and positive", "camera.aspect_ratio")
    camera = Camera(
        position=position,
        forward=forward,
        up=up,
        vertical_fov_deg=fov,
        aspect_ratio=aspect,
    )

    render_raw = _require_object(root.get("render"), "render")
    width = _require_int(render_raw.get("width"), "render.width", "E_RESOLUTION", positive=True)
    height = _require_int(render_raw.get("height"), "render.height", "E_RESOLUTION", positive=True)
    if width > 16384 or height > 16384:
        raise error("E_RESOLUTION", "render dimensions are unreasonably large", "render")
    samples_per_pixel = _require_int(
        render_raw.get("samples_per_pixel"), "render.samples_per_pixel", "E_SAMPLES", positive=True
    )
    step_size = _require_number(render_raw.get("step_size"), "render.step_size", "E_STEP")
    if step_size <= 0.0:
        raise error("E_STEP", "render.step_size must be finite and positive", "render.step_size")
    max_steps = _require_int(render_raw.get("max_steps"), "render.max_steps", "E_MAX_STEPS", positive=True)
    cutoff = _require_number(
        render_raw.get("transmittance_cutoff"), "render.transmittance_cutoff", "E_CUTOFF"
    )
    if not (0.0 < cutoff < 1.0):
        raise error("E_CUTOFF", "render.transmittance_cutoff must be in (0, 1)", "render.transmittance_cutoff")
    fixed_scale = _require_int(render_raw.get("fixed_scale"), "render.fixed_scale", "E_FIXED_SCALE", positive=True)
    tolerance = _require_number(
        render_raw.get("transmittance_tolerance", 1e-5),
        "render.transmittance_tolerance",
        "E_TOLERANCE",
    )
    if tolerance < 0.0:
        raise error("E_TOLERANCE", "render.transmittance_tolerance must be nonnegative", "render.transmittance_tolerance")
    ray_epsilon = _require_number(render_raw.get("ray_epsilon", 1e-7), "render.ray_epsilon", "E_TOLERANCE")
    if ray_epsilon <= 0.0:
        raise error("E_TOLERANCE", "render.ray_epsilon must be positive", "render.ray_epsilon")
    exposure = _require_number(render_raw.get("exposure", 1.0), "render.exposure", "E_TOLERANCE")
    if exposure <= 0.0:
        raise error("E_TOLERANCE", "render.exposure must be positive", "render.exposure")
    tile_raw = _require_object(render_raw.get("tile"), "render.tile")
    tile_width = _require_int(tile_raw.get("width"), "render.tile.width", "E_TILE", positive=True)
    tile_height = _require_int(tile_raw.get("height"), "render.tile.height", "E_TILE", positive=True)
    max_workers = _require_int(render_raw.get("max_workers"), "render.max_workers", "E_CONCURRENCY", positive=True)
    memory_limit = _require_int(
        render_raw.get("inflight_memory_limit_bytes"),
        "render.inflight_memory_limit_bytes",
        "E_MEMORY_BUDGET",
        positive=True,
    )
    state_value = _require_str(render_raw.get("state_dir"), "render.state_dir")
    output_value = _require_str(render_raw.get("output_dir"), "render.output_dir")
    state_dir = Path(state_value)
    output_dir = Path(output_value)
    if not state_dir.is_absolute():
        state_dir = path.parent / state_dir
    if not output_dir.is_absolute():
        output_dir = path.parent / output_dir
    try:
        state_dir = state_dir.resolve()
        output_dir = output_dir.resolve()
    except OSError as exc:
        raise error("E_PATH", f"cannot resolve output/state path: {exc}", "render") from exc
    if state_dir == output_dir:
        raise error("E_PATH", "render.state_dir and render.output_dir must be different", "render")
    if not state_dir.name or not output_dir.name:
        raise error("E_PATH", "render.state_dir and render.output_dir must name directories", "render")

    render = RenderConfig(
        width=width,
        height=height,
        samples_per_pixel=samples_per_pixel,
        step_size=step_size,
        max_steps=max_steps,
        transmittance_cutoff=cutoff,
        fixed_scale=fixed_scale,
        transmittance_tolerance=tolerance,
        tile_width=tile_width,
        tile_height=tile_height,
        max_workers=max_workers,
        inflight_memory_limit_bytes=memory_limit,
        state_dir=state_dir,
        output_dir=output_dir,
        ray_epsilon=ray_epsilon,
        exposure=exposure,
    )

    jitter = _parse_jitter(path, root, samples_per_pixel)
    media = _parse_media(path, root)
    lights = _parse_lights(path, root, bounds_min, bounds_max)
    scene = Scene(
        name=name,
        seed=seed,
        bounds_min=bounds_min,
        bounds_max=bounds_max,
        media=media,
        lights=lights,
        camera=camera,
        render=render,
        jitter=jitter,
        source_path=path,
    )
    _validate_memory_budget(scene)
    return scene


def _validate_memory_budget(scene: Scene) -> None:
    tile_w = min(scene.render.tile_width, scene.render.width)
    tile_h = min(scene.render.tile_height, scene.render.height)
    sample_bytes = tile_buffer_bytes(tile_w, tile_h, scene.render.samples_per_pixel)
    if sample_bytes > scene.render.inflight_memory_limit_bytes:
        raise error(
            "E_MEMORY_BUDGET",
            "inflight_memory_limit_bytes is smaller than one tile's sampling buffer",
            "render.inflight_memory_limit_bytes",
            required_at_least=sample_bytes,
        )


def math_is_finite(value: float) -> bool:
    return math.isfinite(value)

