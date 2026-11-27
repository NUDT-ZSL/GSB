"""Project schema parsing and deterministic keyframe interpolation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .math3d import vec3


@dataclass(frozen=True)
class Joint:
    name: str
    parent: str | None
    rest_translation: np.ndarray
    rest_rotation_euler_deg: np.ndarray


@dataclass(frozen=True)
class Track:
    interpolation: str
    frames: np.ndarray
    values: np.ndarray

    def evaluate(self, frame: float) -> np.ndarray:
        frames = self.frames
        values = self.values
        if frames.size == 1:
            return values[0].copy()
        if frame <= frames[0]:
            return values[0].copy()
        if frame >= frames[-1]:
            return values[-1].copy()

        upper = int(np.searchsorted(frames, frame, side="right"))
        lower = max(upper - 1, 0)
        upper = min(upper, frames.size - 1)
        f0, f1 = float(frames[lower]), float(frames[upper])
        u = 0.0 if f1 == f0 else (float(frame) - f0) / (f1 - f0)
        p1, p2 = values[lower], values[upper]

        if self.interpolation == "step":
            return p1.copy()
        if self.interpolation == "linear":
            return p1 + (p2 - p1) * u
        if self.interpolation == "cubic":
            # Catmull-Rom tangents with clamped endpoints. This is a cubic
            # interpolation curve in the keyed scalar domain.
            previous = values[max(lower - 1, 0)]
            following = values[min(upper + 1, values.shape[0] - 1)]
            t_prev = frames[max(lower - 1, 0)]
            t_next = frames[min(upper + 1, values.shape[0] - 1)]
            left_span = max(f1 - float(t_prev), 1e-12)
            right_span = max(float(t_next) - f0, 1e-12)
            m1 = (p2 - previous) / left_span
            m2 = (following - p1) / right_span
            dt = f1 - f0
            u2, u3 = u * u, u * u * u
            h00 = 2.0 * u3 - 3.0 * u2 + 1.0
            h10 = u3 - 2.0 * u2 + u
            h01 = -2.0 * u3 + 3.0 * u2
            h11 = u3 - u2
            return h00 * p1 + h10 * dt * m1 + h01 * p2 + h11 * dt * m2
        raise ValueError(f"unsupported interpolation curve: {self.interpolation!r}")


@dataclass(frozen=True)
class JointTracks:
    translation: Track | None = None
    rotation_euler_deg: Track | None = None

    def translation_at(self, frame: float) -> np.ndarray:
        if self.translation is None:
            return np.zeros(3, dtype=np.float64)
        return self.translation.evaluate(frame)

    def rotation_at(self, frame: float) -> np.ndarray:
        if self.rotation_euler_deg is None:
            return np.zeros(3, dtype=np.float64)
        return self.rotation_euler_deg.evaluate(frame)


@dataclass(frozen=True)
class PrimitiveSpec:
    id: str
    kind: str
    bone: str
    center: np.ndarray | None = None
    a: np.ndarray | None = None
    b: np.ndarray | None = None
    radii: np.ndarray | None = None
    radius: float | None = None
    color: np.ndarray | None = None


@dataclass(frozen=True)
class MeshData:
    vertices: np.ndarray
    triangles: np.ndarray
    weight_matrix: np.ndarray
    vertex_parts: tuple[str, ...]
    triangle_parts: tuple[str, ...]


@dataclass(frozen=True)
class CameraSpec:
    mode: str
    position: np.ndarray
    target: np.ndarray
    up: np.ndarray
    near: float
    far: float
    half_width: float
    half_height: float
    fov_deg: float


@dataclass(frozen=True)
class RenderSettings:
    width: int
    height: int
    max_steps: int
    epsilon: float
    step_safety: float
    min_step: float
    max_step: float
    gradient_epsilon: float
    smooth_union_radius: float
    ambient: float
    light_direction: np.ndarray
    background_top: np.ndarray
    background_bottom: np.ndarray


@dataclass(frozen=True)
class KeyMoment:
    frame: int
    label: str


@dataclass
class Project:
    raw: dict[str, Any]
    source_path: Path | None
    schema_version: str
    project_id: str
    project_version: str
    fps: float
    frame_start: int
    frame_end: int
    joints: tuple[Joint, ...]
    joint_map: dict[str, Joint]
    joint_order: tuple[str, ...]
    tracks: dict[str, JointTracks]
    primitives: tuple[PrimitiveSpec, ...]
    primitive_map: dict[str, PrimitiveSpec]
    mesh: MeshData
    camera: CameraSpec
    render: RenderSettings
    collision_config: dict[str, Any] = field(default_factory=dict)
    deformation_config: dict[str, Any] = field(default_factory=dict)
    verification_bones: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def frames(self) -> range:
        return range(self.frame_start, self.frame_end + 1)


def _number(raw: Any, name: str, default: float | None = None) -> float:
    if raw is None and default is not None:
        return float(default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{name} must be a number")
    value = float(raw)
    if not np.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _rgb(raw, fallback) -> np.ndarray:
    if raw is None:
        return np.asarray(fallback, dtype=np.float64).copy()
    values = np.asarray(raw, dtype=np.float64)
    if values.shape != (3,) or not np.all(np.isfinite(values)):
        raise ValueError("color must contain three finite numbers")
    return np.clip(values, 0.0, 1.0)


def _parse_track(raw: Any, name: str, dimensions: int) -> Track:
    if isinstance(raw, list):
        interpolation = "linear"
        keys = raw
    elif isinstance(raw, dict):
        interpolation = str(raw.get("interpolation", "linear")).lower()
        keys = raw.get("keys")
    else:
        raise ValueError(f"track {name} must be an object or a key list")
    if interpolation not in {"linear", "step", "cubic"}:
        raise ValueError(f"track {name} uses unsupported interpolation {interpolation!r}")
    if not isinstance(keys, list) or not keys:
        raise ValueError(f"track {name} requires a non-empty keys list")

    frames: list[float] = []
    values: list[np.ndarray] = []
    for index, key in enumerate(keys):
        if isinstance(key, dict):
            frame_raw = key.get("frame")
            value_raw = key.get("value")
        elif isinstance(key, (list, tuple)) and len(key) == 2:
            frame_raw, value_raw = key
        else:
            raise ValueError(f"track {name} key {index} must be [frame, value]")
        frame = _number(frame_raw, f"track {name} key frame")
        array = np.asarray(value_raw, dtype=np.float64)
        if dimensions == 1:
            array = array.reshape(-1)
            if array.size != 1:
                raise ValueError(f"track {name} values must be scalar")
        else:
            array = array.reshape(-1)
            if array.size != dimensions:
                raise ValueError(f"track {name} values must have {dimensions} components")
        if not np.all(np.isfinite(array)):
            raise ValueError(f"track {name} values must be finite")
        frames.append(frame)
        values.append(array)
    frame_array = np.asarray(frames, dtype=np.float64)
    if np.any(np.diff(frame_array) <= 0.0):
        raise ValueError(f"track {name} key frames must be strictly increasing")
    return Track(interpolation, frame_array, np.vstack(values))


def _parse_joint(raw: dict[str, Any], index: int) -> Joint:
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"skeleton joint {index} requires a non-empty name")
    parent = raw.get("parent")
    if parent is not None and (not isinstance(parent, str) or not parent):
        raise ValueError(f"joint {name} has an invalid parent")
    return Joint(
        name=name,
        parent=parent,
        rest_translation=vec3(raw.get("rest_translation", [0.0, 0.0, 0.0]), f"{name}.rest_translation"),
        rest_rotation_euler_deg=vec3(
            raw.get("rest_rotation_euler_deg", [0.0, 0.0, 0.0]),
            f"{name}.rest_rotation_euler_deg",
        ),
    )


def _topological_joint_order(joints: tuple[Joint, ...], joint_map: dict[str, Joint]) -> tuple[str, ...]:
    roots = [joint.name for joint in joints if joint.parent is None]
    if len(roots) != 1:
        raise ValueError("skeleton must contain exactly one root joint")
    order: list[str] = []
    pending = {joint.name for joint in joints}
    children: dict[str, list[str]] = {}
    for joint in joints:
        if joint.parent is not None:
            if joint.parent not in joint_map:
                raise ValueError(f"joint {joint.name} references missing parent {joint.parent}")
            children.setdefault(joint.parent, []).append(joint.name)
    queue = [roots[0]]
    while queue:
        name = queue.pop(0)
        if name not in pending:
            continue
        pending.remove(name)
        order.append(name)
        queue.extend(sorted(children.get(name, [])))
    if pending:
        raise ValueError(f"cycle detected in skeleton joints: {', '.join(sorted(pending))}")
    return tuple(order)


def _parse_primitive(raw: dict[str, Any], joint_map: dict[str, Joint], index: int) -> PrimitiveSpec:
    primitive_id = raw.get("id")
    if not isinstance(primitive_id, str) or not primitive_id:
        raise ValueError(f"SDF primitive {index} requires a non-empty id")
    kind = str(raw.get("type", "")).lower()
    bone = raw.get("bone")
    if bone not in joint_map:
        raise ValueError(f"primitive {primitive_id} references unknown bone {bone!r}")
    color = None if raw.get("color") is None else _rgb(raw["color"], (0.8, 0.7, 0.6))
    if kind == "sphere":
        radius = _number(raw.get("radius"), f"{primitive_id}.radius")
        if radius <= 0:
            raise ValueError(f"primitive {primitive_id} radius must be positive")
        return PrimitiveSpec(
            id=primitive_id,
            kind=kind,
            bone=bone,
            center=vec3(raw.get("center", [0, 0, 0]), f"{primitive_id}.center"),
            radius=radius,
            color=color,
        )
    if kind == "capsule":
        radius = _number(raw.get("radius"), f"{primitive_id}.radius")
        if radius <= 0:
            raise ValueError(f"primitive {primitive_id} radius must be positive")
        a = vec3(raw.get("a"), f"{primitive_id}.a")
        b = vec3(raw.get("b"), f"{primitive_id}.b")
        if np.linalg.norm(b - a) <= 1e-12:
            raise ValueError(f"capsule {primitive_id} endpoints must differ")
        return PrimitiveSpec(id=primitive_id, kind=kind, bone=bone, a=a, b=b, radius=radius, color=color)
    if kind == "ellipsoid":
        radii = vec3(raw.get("radii"), f"{primitive_id}.radii")
        if np.any(radii <= 0):
            raise ValueError(f"primitive {primitive_id} radii must be positive")
        return PrimitiveSpec(
            id=primitive_id,
            kind=kind,
            bone=bone,
            center=vec3(raw.get("center", [0, 0, 0]), f"{primitive_id}.center"),
            radii=radii,
            color=color,
        )
    raise ValueError(f"primitive {primitive_id} has unsupported type {kind!r}")


def _parse_mesh(raw: dict[str, Any], joint_names: tuple[str, ...], joint_index: dict[str, int]) -> MeshData:
    vertices = np.asarray(raw.get("vertices"), dtype=np.float64)
    triangles = np.asarray(raw.get("triangles"), dtype=np.int64)
    if vertices.ndim != 2 or vertices.shape[1] != 3 or vertices.shape[0] == 0:
        raise ValueError("mesh.vertices must be a non-empty N x 3 array")
    if not np.all(np.isfinite(vertices)):
        raise ValueError("mesh.vertices must be finite")
    if triangles.ndim != 2 or triangles.shape[1] != 3 or triangles.shape[0] == 0:
        raise ValueError("mesh.triangles must be a non-empty M x 3 array")
    if np.any(triangles < 0) or np.any(triangles >= vertices.shape[0]):
        raise ValueError("mesh.triangles references an out-of-range vertex")

    raw_weights = raw.get("vertex_weights")
    if not isinstance(raw_weights, list) or len(raw_weights) != vertices.shape[0]:
        raise ValueError("mesh.vertex_weights must have one entry per vertex")
    weights = np.zeros((vertices.shape[0], len(joint_names)), dtype=np.float64)
    for vertex_index, entry in enumerate(raw_weights):
        if isinstance(entry, dict):
            items = entry.items()
        elif isinstance(entry, list):
            items = []
            for pair in entry:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    raise ValueError(f"vertex {vertex_index} has an invalid weight pair")
                items.append((pair[0], pair[1]))
        else:
            raise ValueError(f"vertex {vertex_index} weights must be an object or pair list")
        for name, weight_raw in items:
            if name not in joint_index:
                raise ValueError(f"vertex {vertex_index} references unknown joint {name!r}")
            weight = _number(weight_raw, f"vertex {vertex_index} weight")
            if weight < 0:
                raise ValueError(f"vertex {vertex_index} has a negative weight")
            weights[vertex_index, joint_index[name]] += weight
    sums = weights.sum(axis=1)
    if np.any(sums <= 0):
        raise ValueError("every mesh vertex must have positive total skin weight")
    weights /= sums[:, None]

    vertex_parts = tuple(str(value) for value in raw.get("vertex_parts", []))
    triangle_parts = tuple(str(value) for value in raw.get("triangle_parts", []))
    if len(vertex_parts) != vertices.shape[0]:
        raise ValueError("mesh.vertex_parts must have one entry per vertex")
    if len(triangle_parts) != triangles.shape[0]:
        raise ValueError("mesh.triangle_parts must have one entry per triangle")
    for part in set(vertex_parts) | set(triangle_parts):
        if part not in joint_index:
            raise ValueError(f"mesh part references unknown joint {part!r}")
    return MeshData(vertices, triangles, weights, vertex_parts, triangle_parts)


def _parse_camera(raw: dict[str, Any]) -> CameraSpec:
    mode = str(raw.get("mode", "orthographic")).lower()
    if mode not in {"orthographic", "perspective"}:
        raise ValueError("camera.mode must be 'orthographic' or 'perspective'")
    near = _number(raw.get("near", 0.01), "camera.near")
    far = _number(raw.get("far", 20.0), "camera.far")
    if near <= 0 or far <= near:
        raise ValueError("camera requires 0 < near < far")
    half_width = _number(raw.get("half_width", 1.5), "camera.half_width")
    half_height = _number(raw.get("half_height", 1.5), "camera.half_height")
    if half_width <= 0 or half_height <= 0:
        raise ValueError("camera half extents must be positive")
    position = vec3(raw.get("position", [0, 0, 5]), "camera.position")
    target = vec3(raw.get("target", [0, 0, 0]), "camera.target")
    if np.linalg.norm(target - position) <= 1e-12:
        raise ValueError("camera position and target must differ")
    fov = _number(raw.get("fov_deg", 45.0), "camera.fov_deg")
    if not 0.0 < fov < 180.0:
        raise ValueError("camera.fov_deg must be in (0, 180)")
    return CameraSpec(
        mode=mode,
        position=position,
        target=target,
        up=vec3(raw.get("up", [0, 1, 0]), "camera.up"),
        near=near,
        far=far,
        half_width=half_width,
        half_height=half_height,
        fov_deg=fov,
    )


def _parse_render(raw: dict[str, Any]) -> RenderSettings:
    width = int(raw.get("width", 128))
    height = int(raw.get("height", 128))
    max_steps = int(raw.get("max_steps", 96))
    if width <= 0 or height <= 0:
        raise ValueError("render width and height must be positive")
    if max_steps <= 0:
        raise ValueError("render.max_steps must be positive")
    epsilon = _number(raw.get("epsilon", 4e-4), "render.epsilon")
    safety = _number(raw.get("step_safety", 0.8), "render.step_safety")
    min_step = _number(raw.get("min_step", 1e-6), "render.min_step")
    max_step = _number(raw.get("max_step", 0.08), "render.max_step")
    if not (0 < safety <= 1.0) or epsilon <= 0 or min_step <= 0 or max_step <= 0:
        raise ValueError("render tracing parameters are out of range")
    return RenderSettings(
        width=width,
        height=height,
        max_steps=max_steps,
        epsilon=epsilon,
        step_safety=safety,
        min_step=min_step,
        max_step=max_step,
        gradient_epsilon=_number(raw.get("gradient_epsilon", 8e-4), "render.gradient_epsilon"),
        smooth_union_radius=_number(
            raw.get("smooth_union_radius", 0.0), "render.smooth_union_radius"
        ),
        ambient=_number(raw.get("ambient", 0.22), "render.ambient"),
        light_direction=vec3(raw.get("light_direction", [-0.45, 0.65, 1.0]), "render.light_direction"),
        background_top=_rgb(raw.get("background_top"), (0.055, 0.075, 0.11)),
        background_bottom=_rgb(raw.get("background_bottom"), (0.012, 0.016, 0.025)),
    )


def load_project(path: str | Path) -> Project:
    source = Path(path)
    with source.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("project root must be a JSON object")
    if raw.get("schema_version") != "1.0":
        raise ValueError("unsupported project schema_version; expected '1.0'")

    skeleton_raw = raw.get("skeleton")
    if not isinstance(skeleton_raw, dict) or not isinstance(skeleton_raw.get("joints"), list):
        raise ValueError("project.skeleton.joints must be a list")
    joints = tuple(_parse_joint(item, index) for index, item in enumerate(skeleton_raw["joints"]))
    if not joints:
        raise ValueError("skeleton must contain at least one joint")
    joint_map = {joint.name: joint for joint in joints}
    if len(joint_map) != len(joints):
        raise ValueError("skeleton joint names must be unique")
    order = _topological_joint_order(joints, joint_map)
    declared_root = skeleton_raw.get("root")
    if declared_root is not None and declared_root != order[0]:
        raise ValueError("skeleton.root does not match the unique parentless joint")

    animation = raw.get("animation", {})
    tracks_raw = animation.get("tracks", {}) if isinstance(animation, dict) else {}
    if not isinstance(tracks_raw, dict):
        raise ValueError("animation.tracks must be an object")
    tracks: dict[str, JointTracks] = {}
    for name, value in tracks_raw.items():
        if name not in joint_map:
            raise ValueError(f"animation references unknown joint {name!r}")
        if not isinstance(value, dict):
            raise ValueError(f"animation track for {name} must be an object")
        rotation_raw = value.get("rotation_euler_deg", value.get("rotation_deg"))
        tracks[name] = JointTracks(
            translation=None
            if value.get("translation") is None
            else _parse_track(value["translation"], f"{name}.translation", 3),
            rotation_euler_deg=None
            if rotation_raw is None
            else _parse_track(rotation_raw, f"{name}.rotation_euler_deg", 3),
        )

    frame_range = raw.get("frame_range")
    if not isinstance(frame_range, (list, tuple)) or len(frame_range) != 2:
        raise ValueError("project.frame_range must be [start, end]")
    frame_start, frame_end = int(frame_range[0]), int(frame_range[1])
    if frame_end < frame_start:
        raise ValueError("project.frame_range end must not precede start")

    primitives_raw = raw.get("sdf_primitives")
    if not isinstance(primitives_raw, list) or not primitives_raw:
        raise ValueError("project.sdf_primitives must be a non-empty list")
    primitives = tuple(
        _parse_primitive(item, joint_map, index) for index, item in enumerate(primitives_raw)
    )
    primitive_map = {primitive.id: primitive for primitive in primitives}
    if len(primitive_map) != len(primitives):
        raise ValueError("SDF primitive ids must be unique")

    mesh_raw = raw.get("mesh")
    if not isinstance(mesh_raw, dict):
        raise ValueError("project.mesh must be an object")
    joint_index = {name: index for index, name in enumerate(order)}
    mesh = _parse_mesh(mesh_raw, order, joint_index)

    camera_raw = raw.get("camera")
    if not isinstance(camera_raw, dict):
        raise ValueError("project.camera must be an object")
    camera = _parse_camera(camera_raw)
    render = _parse_render(raw.get("render", {}))

    verification = raw.get("deformation_verification", {})
    verification_bones = tuple(verification.get("bones", []))
    for bone in verification_bones:
        if bone not in joint_map:
            raise ValueError(f"deformation verification references unknown bone {bone!r}")

    fps = _number(raw.get("fps", 24.0), "fps")
    if fps <= 0:
        raise ValueError("fps must be positive")
    return Project(
        raw=raw,
        source_path=source,
        schema_version=str(raw["schema_version"]),
        project_id=str(raw.get("project_id", source.stem)),
        project_version=str(raw.get("project_version", "v1")),
        fps=fps,
        frame_start=frame_start,
        frame_end=frame_end,
        joints=joints,
        joint_map=joint_map,
        joint_order=order,
        tracks=tracks,
        primitives=primitives,
        primitive_map=primitive_map,
        mesh=mesh,
        camera=camera,
        render=render,
        collision_config=dict(raw.get("collision", {})),
        deformation_config=dict(verification),
        verification_bones=verification_bones,
        metadata=dict(raw.get("metadata", {})),
    )


def load_key_moments(path: str | Path) -> tuple[KeyMoment, ...]:
    source = Path(path)
    with source.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    items = raw.get("key_moments") if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not items:
        raise ValueError("key moments file must contain a non-empty key_moments list")
    result: list[KeyMoment] = []
    seen: set[int] = set()
    for index, item in enumerate(items):
        if isinstance(item, int):
            frame = item
            label = f"frame_{frame}"
        elif isinstance(item, dict):
            frame = int(item.get("frame"))
            label = str(item.get("label", f"frame_{frame}"))
        else:
            raise ValueError(f"key moment {index} must be an integer or object")
        if frame in seen:
            raise ValueError(f"duplicate key moment frame {frame}")
        seen.add(frame)
        result.append(KeyMoment(frame=frame, label=label))
    return tuple(sorted(result, key=lambda moment: moment.frame))

