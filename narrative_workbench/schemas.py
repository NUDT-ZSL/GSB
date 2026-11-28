from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .models import (
    CameraFrame,
    DepthCurve,
    GaussianElement,
    NarrativeLayerSpec,
    SceneBundle,
    canonical_plane_normal,
    covariance_from_6,
    decompose_covariance,
    euler_xyz_deg_to_matrix,
    matrix_from_rotation_scale,
    quaternion_to_matrix,
)


@dataclass
class NormalizationIssue:
    schema_version: str
    element_id: str | None
    source_index: int | None
    code: str
    path: str
    message: str
    severity: str = "error"
    action: str = "excluded"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "element_id": self.element_id,
            "source_index": self.source_index,
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "severity": self.severity,
            "action": self.action,
        }


@dataclass
class NormalizationResult:
    scene: SceneBundle
    issues: list[NormalizationIssue]
    source_schema: str
    source_path: str
    raw_layer_metadata: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "normalized-narrative-gaussians/1",
            "source_schema": self.source_schema,
            "source_path": self.source_path,
            "scene": {
                "id": self.scene.scene_id,
                "seed": int(self.scene.seed),
                "width": self.scene.width,
                "height": self.scene.height,
                "layers": [layer.to_dict() for layer in self.scene.layers],
                "frame_count": len(self.scene.camera_frames),
            },
            "elements": [element.to_dict() for element in self.scene.elements],
            "issues": [issue.to_dict() for issue in self.issues],
            "raw_layer_metadata": self.raw_layer_metadata,
        }


def _issue(
    issues: list[NormalizationIssue],
    schema: str,
    element_id: str | None,
    index: int | None,
    code: str,
    path: str,
    message: str,
    action: str = "excluded",
) -> None:
    issues.append(
        NormalizationIssue(
            schema_version=schema,
            element_id=element_id,
            source_index=index,
            code=code,
            path=path,
            message=message,
            action=action,
        )
    )


def _as_float(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, np.number)):
        raise ValueError(f"{path} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{path} must be finite")
    return result


def _parse_vector(value: Any, length: int, path: str) -> np.ndarray:
    if isinstance(value, dict):
        names = ("x", "y", "z", "w")[:length]
        if not all(name in value for name in names):
            raise ValueError(f"{path} must contain {', '.join(names)}")
        values = [value[name] for name in names]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        raise ValueError(f"{path} must be an array or object")
    if len(values) != length:
        raise ValueError(f"{path} must have {length} components")
    return np.array([_as_float(item, path) for item in values], dtype=np.float64)


def _parse_color(value: Any, path: str) -> np.ndarray:
    if isinstance(value, str):
        match = re.fullmatch(r"#([0-9a-fA-F]{6})([0-9a-fA-F]{2})?", value.strip())
        if not match:
            raise ValueError(f"{path} must be #RRGGBB or #RRGGBBAA")
        rgb_hex = match.group(1)
        return np.array(
            [int(rgb_hex[index : index + 2], 16) / 255.0 for index in (0, 2, 4)],
            dtype=np.float64,
        )
    if isinstance(value, dict):
        length = 4 if "a" in value else 3
        color = _parse_vector(value, length, path)
    elif isinstance(value, (list, tuple)):
        if len(value) not in (3, 4):
            raise ValueError(f"{path} must have 3 or 4 components")
        color = np.array([_as_float(item, path) for item in value], dtype=np.float64)
    else:
        raise ValueError(f"{path} must be a hex string, array, or object")
    if np.any(color < 0.0) or np.any(color > 1.0):
        if np.all(color >= 0.0) and np.all(color <= 255.0):
            color = color / 255.0
        else:
            raise ValueError(f"{path} components must be in [0, 1] or [0, 255]")
    return color[:3].copy()


def _parse_curve(value: Any, path: str) -> DepthCurve:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    raw_points = (
        value.get("control_points")
        if "control_points" in value
        else value.get("points", value.get("keys"))
    )
    if raw_points is None:
        raise ValueError(f"{path} is missing control points")
    if not isinstance(raw_points, list):
        raise ValueError(f"{path} control points must be an array")
    points: list[tuple[float, float]] = []
    for index, point in enumerate(raw_points):
        if isinstance(point, dict):
            x_value = point.get("depth_norm", point.get("t", point.get("normalized_depth")))
            y_value = point.get("gain", point.get("opacity", point.get("multiplier")))
        elif isinstance(point, (list, tuple)) and len(point) == 2:
            x_value, y_value = point
        else:
            raise ValueError(f"{path}[{index}] is not a depth/gain pair")
        x = _as_float(x_value, f"{path}[{index}].depth")
        y = _as_float(y_value, f"{path}[{index}].gain")
        points.append((float(np.clip(x, 0.0, 1.0)), float(np.clip(y, 0.0, 1.0))))
    try:
        return DepthCurve(tuple(points))
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from error


def _parse_layers_v1(raw: dict[str, Any]) -> list[NarrativeLayerSpec]:
    scene = raw.get("scene", {})
    source = raw.get("narrative_layers", scene.get("narrative_layers", scene.get("layers")))
    result: list[NarrativeLayerSpec] = []
    if isinstance(source, list):
        for index, entry in enumerate(source):
            if isinstance(entry, str):
                result.append(NarrativeLayerSpec(index=index, name=entry))
            elif isinstance(entry, dict):
                layer_index = int(entry.get("index", entry.get("id", index)))
                name = str(entry.get("name", entry.get("key", f"layer_{layer_index}")))
                aliases = tuple(str(v) for v in entry.get("aliases", []) if v != name)
                result.append(NarrativeLayerSpec(layer_index, name, aliases))
    if not result:
        result = [
            NarrativeLayerSpec(0, "backdrop_glow", ("backdrop", "bg")),
            NarrativeLayerSpec(1, "stained_glass", ("glass",)),
            NarrativeLayerSpec(2, "smoke", ("fog",)),
            NarrativeLayerSpec(3, "particles", ("particle", "sprites")),
            NarrativeLayerSpec(4, "light_glow", ("glow",)),
        ]
    return result


def _parse_layers_v2(raw: dict[str, Any]) -> list[NarrativeLayerSpec]:
    source = raw.get("narrative_layers", raw.get("layers", []))
    result: list[NarrativeLayerSpec] = []
    if isinstance(source, list):
        for index, entry in enumerate(source):
            if isinstance(entry, str):
                result.append(NarrativeLayerSpec(index=index, name=entry))
            elif isinstance(entry, dict):
                layer_index = int(entry.get("id", entry.get("index", index)))
                name = str(entry.get("key", entry.get("name", f"layer_{layer_index}")))
                raw_aliases = entry.get("aliases", entry.get("alias", []))
                if isinstance(raw_aliases, str):
                    raw_aliases = [raw_aliases]
                aliases = tuple(str(v) for v in raw_aliases if v != name)
                result.append(NarrativeLayerSpec(layer_index, name, aliases))
    return result or _parse_layers_v1(raw)


def _parse_layers_v3(raw: dict[str, Any]) -> list[NarrativeLayerSpec]:
    scene = raw.get("scene", {})
    source = scene.get("layers", raw.get("layers", []))
    result: list[NarrativeLayerSpec] = []
    if isinstance(source, list):
        for index, entry in enumerate(source):
            if isinstance(entry, str):
                result.append(NarrativeLayerSpec(index=index, name=entry))
            elif isinstance(entry, dict):
                layer_index = int(entry.get("id", entry.get("index", index)))
                name = str(entry.get("name", entry.get("key", f"layer_{layer_index}")))
                aliases = tuple(str(v) for v in entry.get("aliases", []) if v != name)
                result.append(NarrativeLayerSpec(layer_index, name, aliases))
    return result or _parse_layers_v1(raw)


def _resolve_layer(
    value: Any,
    layers: Iterable[NarrativeLayerSpec],
    path: str,
) -> NarrativeLayerSpec:
    layer_list = list(layers)
    by_index = {layer.index: layer for layer in layer_list}
    by_name: dict[str, NarrativeLayerSpec] = {}
    for layer in layer_list:
        by_name[layer.name] = layer
        for alias in layer.aliases:
            by_name[alias] = layer
    if isinstance(value, bool):
        raise ValueError(f"{path} is not a layer id or name")
    if isinstance(value, (int, np.integer)):
        if int(value) not in by_index:
            raise ValueError(f"{path}={value!r} does not map to a declared layer")
        return by_index[int(value)]
    if isinstance(value, str):
        if value in by_name:
            return by_name[value]
        if value.isdigit() and int(value) in by_index:
            return by_index[int(value)]
        raise ValueError(f"{path}={value!r} is not a declared layer name")
    if isinstance(value, dict):
        for key in ("name", "key", "id", "index"):
            if key in value:
                return _resolve_layer(value[key], layer_list, f"{path}.{key}")
        raise ValueError(f"{path} object does not contain a layer name or id")
    raise ValueError(f"{path} must be an integer id or textual layer name")


def _base_element(
    *,
    schema: str,
    element_id: str,
    layer: NarrativeLayerSpec,
    center: np.ndarray,
    covariance: np.ndarray,
    opacity: float,
    color: np.ndarray,
    curve: DepthCurve,
    stable_index: int,
    source_index: int,
) -> GaussianElement:
    axes, scales, quaternion = decompose_covariance(covariance)
    return GaussianElement(
        element_id=element_id,
        layer_index=layer.index,
        layer_name=layer.name,
        center=center,
        covariance=covariance,
        opacity=float(np.clip(opacity, 0.0, 1.0)),
        color=color,
        depth_curve=curve,
        stable_index=int(stable_index),
        source_schema=schema,
        source_index=int(source_index),
        rotation=axes,
        scale=scales,
        quaternion=quaternion,
        plane_normal=canonical_plane_normal(covariance),
    )


def _parse_v1(
    raw: dict[str, Any],
    camera_frames: list[CameraFrame],
) -> NormalizationResult:
    schema = "v1"
    issues: list[NormalizationIssue] = []
    layers = _parse_layers_v1(raw)
    scene_data = raw.get("scene", {})
    elements: list[GaussianElement] = []
    records = raw.get("elements")
    if not isinstance(records, list):
        _issue(issues, schema, None, None, "missing_field", "elements", "v1 elements array is missing", "scene-invalid")
        records = []
    for index, record in enumerate(records):
        element_id = record.get("id") if isinstance(record, dict) else None
        if not isinstance(record, dict):
            _issue(issues, schema, element_id, index, "invalid_record", "", "element is not an object")
            continue
        try:
            if "id" not in record:
                raise ValueError("id is missing")
            if "center" not in record:
                raise ValueError("center is missing")
            if "rotation_deg" not in record:
                raise ValueError("rotation_deg is missing")
            if "scale" not in record:
                raise ValueError("scale is missing")
            if "opacity" not in record:
                raise ValueError("opacity is missing")
            if "rgb" not in record and "color" not in record:
                raise ValueError("rgb/color is missing")
            if "depth_curve" not in record:
                raise ValueError("depth_curve is missing")
            layer_value = record.get("layer", record.get("layer_index"))
            if layer_value is None:
                raise ValueError("layer is missing")
            layer = _resolve_layer(layer_value, layers, "layer")
            center = _parse_vector(record["center"], 3, "center")
            euler = _parse_vector(record["rotation_deg"], 3, "rotation_deg")
            scale = _parse_vector(record["scale"], 3, "scale")
            rotation = euler_xyz_deg_to_matrix(euler)
            covariance = matrix_from_rotation_scale(rotation, scale)
            opacity = _as_float(record["opacity"], "opacity")
            color = _parse_color(record.get("rgb", record.get("color")), "rgb")
            curve = _parse_curve(record["depth_curve"], "depth_curve")
            elements.append(
                _base_element(
                    schema=schema,
                    element_id=str(record["id"]),
                    layer=layer,
                    center=center,
                    covariance=covariance,
                    opacity=opacity,
                    color=color,
                    curve=curve,
                    stable_index=int(record.get("stable_rank", index)),
                    source_index=index,
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            message = str(error)
            _issue(
                issues,
                schema,
                str(element_id) if element_id is not None else None,
                index,
                "invalid_element",
                message.split(maxsplit=1)[0] if message.split() else "",
                message,
            )
    return NormalizationResult(
        scene=SceneBundle(
            scene_id=str(scene_data.get("id", raw.get("scene_id", "unnamed"))),
            seed=int(scene_data.get("seed", raw.get("seed", 0))),
            layers=layers,
            camera_frames=camera_frames,
            elements=elements,
        ),
        issues=issues,
        source_schema=schema,
        source_path="",
        raw_layer_metadata=[layer.to_dict() for layer in layers],
    )


def _parse_v2(
    raw: dict[str, Any],
    camera_frames: list[CameraFrame],
) -> NormalizationResult:
    schema = "v2"
    issues: list[NormalizationIssue] = []
    layers = _parse_layers_v2(raw)
    elements: list[GaussianElement] = []
    records = raw.get("primitives")
    if not isinstance(records, list):
        _issue(issues, schema, None, None, "missing_field", "primitives", "v2 primitives array is missing", "scene-invalid")
        records = []
    for index, record in enumerate(records):
        element_id = record.get("uid") if isinstance(record, dict) else None
        if not isinstance(record, dict):
            _issue(issues, schema, element_id, index, "invalid_record", "", "primitive is not an object")
            continue
        try:
            if "uid" not in record:
                raise ValueError("uid is missing")
            if "story_layer" not in record:
                raise ValueError("story_layer is missing")
            if "position" not in record:
                raise ValueError("position is missing")
            if "rotation_quaternion_wxyz" not in record:
                raise ValueError("rotation_quaternion_wxyz is missing")
            if "sigmas" not in record:
                raise ValueError("sigmas is missing")
            if "alpha" not in record:
                raise ValueError("alpha is missing")
            if "tint" not in record:
                raise ValueError("tint is missing")
            if "attenuation" not in record:
                raise ValueError("attenuation is missing")
            layer = _resolve_layer(record["story_layer"], layers, "story_layer")
            center = _parse_vector(record["position"], 3, "position")
            quaternion = _parse_vector(record["rotation_quaternion_wxyz"], 4, "rotation_quaternion_wxyz")
            scale = _parse_vector(record["sigmas"], 3, "sigmas")
            rotation = quaternion_to_matrix(quaternion)
            covariance = matrix_from_rotation_scale(rotation, scale)
            opacity = _as_float(record["alpha"], "alpha")
            color = _parse_color(record["tint"], "tint")
            curve = _parse_curve(record["attenuation"], "attenuation")
            elements.append(
                _base_element(
                    schema=schema,
                    element_id=str(record["uid"]),
                    layer=layer,
                    center=center,
                    covariance=covariance,
                    opacity=opacity,
                    color=color,
                    curve=curve,
                    stable_index=int(record.get("stable_rank", index)),
                    source_index=index,
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            message = str(error)
            _issue(
                issues,
                schema,
                str(element_id) if element_id is not None else None,
                index,
                "invalid_element",
                message.split(maxsplit=1)[0] if message.split() else "",
                message,
            )
    return NormalizationResult(
        scene=SceneBundle(
            scene_id=str(raw.get("scene_id", "unnamed")),
            seed=int(raw.get("seed", 0)),
            layers=layers,
            camera_frames=camera_frames,
            elements=elements,
        ),
        issues=issues,
        source_schema=schema,
        source_path="",
        raw_layer_metadata=[layer.to_dict() for layer in layers],
    )


def _parse_v3(
    raw: dict[str, Any],
    camera_frames: list[CameraFrame],
) -> NormalizationResult:
    schema = "v3"
    issues: list[NormalizationIssue] = []
    layers = _parse_layers_v3(raw)
    scene_data = raw.get("scene", {})
    elements: list[GaussianElement] = []
    records = raw.get("items")
    if not isinstance(records, list):
        _issue(issues, schema, None, None, "missing_field", "items", "v3 items array is missing", "scene-invalid")
        records = []
    for index, record in enumerate(records):
        element_id = record.get("key") if isinstance(record, dict) else None
        if not isinstance(record, dict):
            _issue(issues, schema, element_id, index, "invalid_record", "", "item is not an object")
            continue
        try:
            if "key" not in record:
                raise ValueError("key is missing")
            if "layer_id" not in record:
                raise ValueError("layer_id is missing")
            if "mu" not in record:
                raise ValueError("mu is missing")
            if "covariance" not in record:
                raise ValueError("covariance is missing")
            if "base_opacity" not in record:
                raise ValueError("base_opacity is missing")
            if "rgba" not in record:
                raise ValueError("rgba is missing")
            if "depth_alpha" not in record:
                raise ValueError("depth_alpha is missing")
            layer = _resolve_layer(record["layer_id"], layers, "layer_id")
            center = _parse_vector(record["mu"], 3, "mu")
            covariance_raw = record["covariance"]
            covariance_order = str(record.get("covariance_order", "xx,yy,zz,xy,xz,yz"))
            covariance = covariance_from_6(covariance_raw, covariance_order)
            opacity = _as_float(record["base_opacity"], "base_opacity")
            color = _parse_color(record["rgba"], "rgba")
            curve = _parse_curve(record["depth_alpha"], "depth_alpha")
            elements.append(
                _base_element(
                    schema=schema,
                    element_id=str(record["key"]),
                    layer=layer,
                    center=center,
                    covariance=covariance,
                    opacity=opacity,
                    color=color,
                    curve=curve,
                    stable_index=int(record.get("stable_rank", index)),
                    source_index=index,
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            message = str(error)
            _issue(
                issues,
                schema,
                str(element_id) if element_id is not None else None,
                index,
                "invalid_element",
                message.split(maxsplit=1)[0] if message.split() else "",
                message,
            )
    return NormalizationResult(
        scene=SceneBundle(
            scene_id=str(scene_data.get("id", raw.get("scene_id", "unnamed"))),
            seed=int(scene_data.get("seed", raw.get("seed", 0))),
            layers=layers,
            camera_frames=camera_frames,
            elements=elements,
        ),
        issues=issues,
        source_schema=schema,
        source_path="",
        raw_layer_metadata=[layer.to_dict() for layer in layers],
    )


def detect_schema(raw: dict[str, Any]) -> str:
    marker = str(raw.get("$schema", raw.get("schema_version", ""))).lower()
    if "narrative-scene/3" in marker or "v3" in marker or "items" in raw:
        return "v3"
    if raw.get("version") == 2 or "primitives" in raw:
        return "v2"
    if "elements" in raw or "v1" in marker:
        return "v1"
    raise ValueError("cannot detect schema version")


def load_normalized_scene(
    path: str | Path,
    camera_frames: list[CameraFrame],
) -> NormalizationResult:
    source_path = Path(path)
    raw = json.loads(source_path.read_text(encoding="utf-8"))
    schema = detect_schema(raw)
    if schema == "v1":
        result = _parse_v1(raw, camera_frames)
    elif schema == "v2":
        result = _parse_v2(raw, camera_frames)
    else:
        result = _parse_v3(raw, camera_frames)
    result.source_path = str(source_path)
    return result


def load_camera_frames(path: str | Path) -> list[CameraFrame]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    width = int(raw.get("width", 160))
    height = int(raw.get("height", 96))
    fov_y_deg = float(raw.get("fov_y_deg", 50.0))
    near = float(raw.get("near", 0.08))
    far = float(raw.get("far", 20.0))
    result: list[CameraFrame] = []
    for index, item in enumerate(raw.get("frames", [])):
        result.append(
            CameraFrame(
                frame_index=int(item.get("frame", index)),
                eye=tuple(float(v) for v in item["eye"]),
                target=tuple(float(v) for v in item["target"]),
                up=tuple(float(v) for v in item.get("up", [0.0, 1.0, 0.0])),
                width=int(item.get("width", width)),
                height=int(item.get("height", height)),
                fov_y_deg=float(item.get("fov_y_deg", fov_y_deg)),
                near=float(item.get("near", near)),
                far=float(item.get("far", far)),
            )
        )
    return result
