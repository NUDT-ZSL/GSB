from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .models import (
    CameraFrame,
    NarrativeLayerSpec,
    matrix_from_rotation_scale,
    matrix_to_euler_xyz_deg,
    matrix_to_quaternion,
    quaternion_to_matrix,
)
from .projection import project_element
from .schemas import NormalizationResult, load_camera_frames, load_normalized_scene
from .storage import read_json, sha256_file, write_json_atomic

DEFAULT_SEED = 20261128
DEFAULT_FRAME_COUNT = 12
DEFAULT_WIDTH = 160
DEFAULT_HEIGHT = 96


class StableRandom:
    """Small deterministic SplitMix64 generator independent of NumPy's RNG stream."""

    _MASK = (1 << 64) - 1

    def __init__(self, seed: int) -> None:
        self.state = int(seed) & self._MASK

    def next_u64(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & self._MASK
        value = self.state
        value = (value ^ (value >> 30)) * 0xBF58476D1CE4E5B9 & self._MASK
        value = (value ^ (value >> 27)) * 0x94D049BB133111EB & self._MASK
        return value ^ (value >> 31)

    def uniform(self, low: float, high: float) -> float:
        unit = self.next_u64() / float(1 << 64)
        return low + (high - low) * unit


LAYERS: tuple[NarrativeLayerSpec, ...] = (
    NarrativeLayerSpec(0, "backdrop_glow", ("backdrop", "bg")),
    NarrativeLayerSpec(1, "stained_glass", ("glass",)),
    NarrativeLayerSpec(2, "smoke", ("fog",)),
    NarrativeLayerSpec(3, "particles", ("particle", "sprites")),
    NarrativeLayerSpec(4, "light_glow", ("glow",)),
)

LAYER_CURVES: dict[str, tuple[tuple[float, float], ...]] = {
    "backdrop_glow": ((0.0, 0.75), (0.40, 1.00), (0.72, 0.82), (1.0, 0.30)),
    "stained_glass": ((0.0, 0.70), (0.34, 1.04), (0.62, 0.92), (1.0, 0.55)),
    "smoke": ((0.0, 0.28), (0.30, 0.92), (0.62, 1.00), (1.0, 0.18)),
    "particles": ((0.0, 0.80), (0.42, 1.00), (0.75, 0.75), (1.0, 0.20)),
    "light_glow": ((0.0, 0.22), (0.36, 0.86), (0.60, 1.00), (1.0, 0.42)),
}


@dataclass(frozen=True)
class Template:
    element_id: str
    layer: str
    center: tuple[float, float, float]
    scale: tuple[float, float, float]
    euler: tuple[float, float, float]
    opacity: float
    color_rgb8: tuple[int, int, int]


TEMPLATES: tuple[Template, ...] = (
    Template("bg_warm_haze", "backdrop_glow", (-0.92, 0.42, -0.18), (0.95, 0.67, 0.18), (10.0, 20.0, 15.0), 0.23, (255, 184, 92)),
    Template("bg_cool_haze", "backdrop_glow", (0.86, -0.34, -0.14), (0.86, 0.58, 0.16), (-20.0, 15.0, -10.0), 0.21, (88, 180, 255)),
    Template("glass_left", "stained_glass", (-0.70, 0.10, 0.04), (0.72, 0.44, 0.055), (62.0, 8.0, -22.0), 0.47, (64, 220, 225)),
    Template("glass_right", "stained_glass", (0.58, -0.18, 0.10), (0.62, 0.46, 0.060), (-58.0, 12.0, 28.0), 0.43, (245, 92, 175)),
    Template("glass_cross", "stained_glass", (-0.22, 0.05, 0.02), (0.92, 0.56, 0.070), (73.0, -12.0, -35.0), 0.39, (255, 176, 55)),
    Template("smoke_cross", "smoke", (-0.10, -0.02, -0.03), (0.78, 0.52, 0.18), (-67.0, 18.0, 41.0), 0.31, (112, 126, 152)),
    Template("smoke_upper", "smoke", (0.10, 0.45, 0.23), (0.80, 0.48, 0.22), (15.0, 55.0, 15.0), 0.25, (178, 182, 196)),
    Template("smoke_lower", "smoke", (-0.45, -0.42, 0.16), (0.65, 0.55, 0.28), (-25.0, 30.0, -40.0), 0.23, (130, 148, 170)),
    Template("particle_amber", "particles", (0.35, 0.32, 0.00), (0.16, 0.12, 0.075), (18.0, -24.0, 35.0), 0.68, (255, 216, 120)),
    Template("particle_cyan", "particles", (-0.32, 0.35, 0.03), (0.14, 0.10, 0.060), (-31.0, 12.0, -16.0), 0.72, (144, 246, 255)),
    Template("particle_rose", "particles", (0.05, -0.48, 0.06), (0.13, 0.12, 0.070), (44.0, 36.0, 8.0), 0.64, (255, 148, 202)),
    Template("glow_center", "light_glow", (0.00, 0.00, -0.05), (0.57, 0.39, 0.14), (-40.0, 10.0, 15.0), 0.35, (255, 238, 176)),
    Template("glow_upper_left", "light_glow", (-0.58, 0.32, 0.27), (0.35, 0.28, 0.12), (24.0, -18.0, 47.0), 0.32, (190, 164, 255)),
    Template("glow_lower_right", "light_glow", (0.62, -0.43, 0.20), (0.31, 0.24, 0.10), (-52.0, 21.0, -30.0), 0.30, (255, 130, 110)),
    Template("offscreen_light_probe", "light_glow", (8.0, 6.0, 0.0), (0.20, 0.16, 0.08), (12.0, -35.0, 18.0), 0.12, (255, 244, 210)),
)


def _jittered_scene(seed: int) -> list[dict[str, Any]]:
    rng = StableRandom(seed ^ 0xA0761D6478BD642F)
    result: list[dict[str, Any]] = []
    for stable_index, template in enumerate(TEMPLATES):
        center = [
            template.center[0] + rng.uniform(-0.025, 0.025),
            template.center[1] + rng.uniform(-0.025, 0.025),
            template.center[2] + rng.uniform(-0.018, 0.018),
        ]
        scale = [
            max(0.035, template.scale[0] * rng.uniform(0.97, 1.03)),
            max(0.030, template.scale[1] * rng.uniform(0.97, 1.03)),
            max(0.040, template.scale[2] * rng.uniform(0.96, 1.04)),
        ]
        euler = [
            template.euler[0] + rng.uniform(-2.5, 2.5),
            template.euler[1] + rng.uniform(-2.5, 2.5),
            template.euler[2] + rng.uniform(-2.5, 2.5),
        ]
        opacity = float(np.clip(template.opacity + rng.uniform(-0.012, 0.012), 0.03, 0.92))
        result.append(
            {
                "id": template.element_id,
                "layer": template.layer,
                "center": center,
                "scale": scale,
                "euler": euler,
                "opacity": opacity,
                "color": [channel / 255.0 for channel in template.color_rgb8],
                "curve": list(LAYER_CURVES[template.layer]),
                "stable_rank": stable_index,
            }
        )
    return result


def _camera_frames(seed: int, frame_count: int, width: int, height: int) -> list[CameraFrame]:
    rng = StableRandom(seed ^ 0xE7037ED1A0B428DB)
    radius_phase = rng.uniform(-0.20, 0.20)
    frames: list[CameraFrame] = []
    for index in range(frame_count):
        phase = 0.0 if frame_count <= 1 else index / float(frame_count - 1)
        angle = math.radians(-54.0 + 108.0 * phase)
        radius = 3.95 + 0.18 * math.sin(2.0 * angle + radius_phase)
        height_value = 0.34 + 0.22 * math.cos(angle + 0.35 * radius_phase)
        eye = (
            radius * math.sin(angle),
            height_value,
            radius * math.cos(angle),
        )
        target = (0.0, 0.015 * math.sin(angle * 1.7), 0.02)
        frames.append(
            CameraFrame(
                frame_index=index,
                eye=eye,
                target=target,
                up=(0.0, 1.0, 0.0),
                width=width,
                height=height,
                fov_y_deg=50.0,
                near=0.08,
                far=20.0,
            )
        )
    return frames


def _layer_payloads() -> list[dict[str, Any]]:
    return [layer.to_dict() for layer in LAYERS]


def _v1_document(scene_id: str, seed: int, elements: list[dict[str, Any]]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for element in elements:
        rotation = quaternion_to_matrix(
            matrix_to_quaternion(_rotation_from_euler(element["euler"]))
        )
        euler = matrix_to_euler_xyz_deg(rotation)
        records.append(
            {
                "id": element["id"],
                "layer": element["layer"],
                "center": [float(v) for v in element["center"]],
                "rotation_deg": [float(v) for v in euler],
                "scale": [float(v) for v in element["scale"]],
                "opacity": float(element["opacity"]),
                "rgb": [float(v) for v in element["color"]],
                "depth_curve": {
                    "type": "piecewise_linear",
                    "control_points": [[float(x), float(y)] for x, y in element["curve"]],
                },
                "stable_rank": int(element["stable_rank"]),
            }
        )
    broken = dict(records[0])
    broken["id"] = "audit_v1_missing_center"
    broken.pop("center", None)
    records.append(broken)
    return {
        "schema_version": "narrative.gaussian.v1",
        "scene": {"id": scene_id, "seed": seed},
        "narrative_layers": _layer_payloads(),
        "elements": records,
        "audit_note": "The final record intentionally omits center to exercise field diagnostics.",
    }


def _v2_document(scene_id: str, seed: int, elements: list[dict[str, Any]]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for element in elements:
        rotation = _rotation_from_euler(element["euler"])
        quaternion = matrix_to_quaternion(rotation)
        if element["stable_rank"] % 2 == 1:
            quaternion = -quaternion
        rgb8 = [int(round(float(channel) * 255.0)) for channel in element["color"]]
        records.append(
            {
                "uid": element["id"],
                "story_layer": element["layer"],
                "position": {
                    "x": float(element["center"][0]),
                    "y": float(element["center"][1]),
                    "z": float(element["center"][2]),
                },
                "rotation_quaternion_wxyz": [float(v) for v in quaternion],
                "sigmas": [float(v) for v in element["scale"]],
                "alpha": float(element["opacity"]),
                "tint": "#" + "".join(f"{channel:02x}" for channel in rgb8),
                "attenuation": {
                    "kind": "narrative_depth_gain",
                    "keys": [
                        {"depth_norm": float(x), "gain": float(y)}
                        for x, y in element["curve"]
                    ],
                },
                "stable_rank": int(element["stable_rank"]),
            }
        )
    broken = dict(records[1])
    broken["uid"] = "audit_v2_bad_quaternion"
    broken["rotation_quaternion_wxyz"] = broken["rotation_quaternion_wxyz"][:3]
    records.append(broken)
    return {
        "version": 2,
        "scene_id": scene_id,
        "seed": seed,
        "narrative_layers": [
            {
                "id": layer.index,
                "key": layer.name,
                "aliases": list(layer.aliases),
            }
            for layer in LAYERS
        ],
        "primitives": records,
        "audit_note": "The final primitive intentionally has a three-component quaternion.",
    }


def _v3_document(scene_id: str, seed: int, elements: list[dict[str, Any]]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    layer_index = {layer.name: layer.index for layer in LAYERS}
    for element in elements:
        rotation = _rotation_from_euler(element["euler"])
        covariance = matrix_from_rotation_scale(
            rotation, np.asarray(element["scale"], dtype=np.float64)
        )
        records.append(
            {
                "key": element["id"],
                "layer_id": layer_index[element["layer"]],
                "mu": [float(v) for v in element["center"]],
                "covariance": [
                    float(covariance[0, 0]),
                    float(covariance[1, 1]),
                    float(covariance[2, 2]),
                    float(covariance[0, 1]),
                    float(covariance[0, 2]),
                    float(covariance[1, 2]),
                ],
                "covariance_order": "xx,yy,zz,xy,xz,yz",
                "base_opacity": float(element["opacity"]),
                "rgba": [float(v) for v in element["color"]] + [1.0],
                "depth_alpha": {
                    "type": "piecewise_linear",
                    "points": [[float(x), float(y)] for x, y in element["curve"]],
                },
                "stable_rank": int(element["stable_rank"]),
            }
        )
    broken = dict(records[2])
    broken["key"] = "audit_v3_unmapped_layer"
    broken["layer_id"] = 77
    records.append(broken)
    return {
        "$schema": "narrative-scene/3",
        "scene": {
            "id": scene_id,
            "seed": seed,
            "layers": [
                {"id": layer.index, "name": layer.name, "aliases": list(layer.aliases)}
                for layer in LAYERS
            ],
        },
        "items": records,
        "audit_note": "The final item intentionally uses an undeclared narrative layer.",
    }


def _rotation_from_euler(euler: list[float] | tuple[float, float, float]) -> np.ndarray:
    from .models import euler_xyz_deg_to_matrix

    return euler_xyz_deg_to_matrix(np.asarray(euler, dtype=np.float64))


def _write_reference(
    scene_dir: Path,
    normalized: NormalizationResult,
    camera_frames: list[CameraFrame],
) -> dict[str, Any]:
    frames: list[dict[str, Any]] = []
    for camera in camera_frames:
        projected: list[dict[str, Any]] = []
        culled: list[dict[str, Any]] = []
        for element in normalized.scene.elements:
            record = project_element(element, camera)
            if record["status"] == "visible":
                projected.append(record)
            else:
                culled.append(record)
        frames.append(
            {
                "frame": camera.frame_index,
                "camera": camera.to_dict(),
                "elements": projected,
                "culled": culled,
            }
        )
    result = {
        "schema_version": "reference-ellipses/1",
        "scene_id": normalized.scene.scene_id,
        "seed": normalized.scene.seed,
        "source_schema": normalized.source_schema,
        "ellipse_convention": {
            "pixel_centers": "integer pixel (x,y) samples at (x+0.5,y+0.5)",
            "covariance": "J R C R^T J^T, 2x2 screen-space Gaussian covariance",
            "semi_axis": "sqrt(eigenvalue)",
            "orientation": "CCW degrees from +x in y-down pixel space; major-axis direction canonicalized to x>=0",
            "support": "3-sigma screen bounding box",
        },
        "frames": frames,
    }
    write_json_atomic(scene_dir / "reference_ellipses.json", result)
    return result


@dataclass
class GeneratedProject:
    root: Path
    scene_dir: Path
    config: dict[str, Any]
    scene_files: dict[str, Path]
    camera_file: Path
    reference_file: Path
    manifest_file: Path
    normalized_v1: NormalizationResult

    @property
    def fingerprint(self) -> str:
        return str(read_json(self.manifest_file)["scene_fingerprint"])


def generate_project(
    output_dir: str | Path,
    *,
    seed: int = DEFAULT_SEED,
    frame_count: int = DEFAULT_FRAME_COUNT,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    force: bool = False,
) -> GeneratedProject:
    root = Path(output_dir)
    scene_dir = root / "scene"
    manifest_file = scene_dir / "manifest.json"
    if manifest_file.exists() and not force:
        manifest = read_json(manifest_file)
        existing = manifest.get("config", {})
        requested = {
            "seed": int(seed),
            "frame_count": int(frame_count),
            "width": int(width),
            "height": int(height),
        }
        if existing != requested:
            raise RuntimeError(
                "generated scene exists with different configuration; pass --force to regenerate"
            )
        camera_frames = load_camera_frames(scene_dir / "camera.json")
        normalized = load_normalized_scene(scene_dir / "scene_v1.json", camera_frames)
        return GeneratedProject(
            root=root,
            scene_dir=scene_dir,
            config=requested,
            scene_files={version: scene_dir / f"scene_{version}.json" for version in ("v1", "v2", "v3")},
            camera_file=scene_dir / "camera.json",
            reference_file=scene_dir / "reference_ellipses.json",
            manifest_file=manifest_file,
            normalized_v1=normalized,
        )
    if frame_count < 1:
        raise ValueError("frame_count must be positive")
    if width < 8 or height < 8:
        raise ValueError("width and height must be at least 8")
    scene_id = f"narrative-seed-{int(seed)}"
    elements = _jittered_scene(seed)
    camera_frames = _camera_frames(seed, frame_count, width, height)
    documents = {
        "v1": _v1_document(scene_id, seed, elements),
        "v2": _v2_document(scene_id, seed, elements),
        "v3": _v3_document(scene_id, seed, elements),
    }
    scene_files: dict[str, Path] = {}
    for version, document in documents.items():
        path = scene_dir / f"scene_{version}.json"
        write_json_atomic(path, document)
        scene_files[version] = path
    camera_file = scene_dir / "camera.json"
    camera_document = {
        "schema_version": "camera-orbit/1",
        "scene_id": scene_id,
        "width": int(width),
        "height": int(height),
        "fov_y_deg": 50.0,
        "near": 0.08,
        "far": 20.0,
        "frames": [frame.to_dict() for frame in camera_frames],
    }
    write_json_atomic(camera_file, camera_document)
    normalized = load_normalized_scene(scene_files["v1"], camera_frames)
    reference_file = scene_dir / "reference_ellipses.json"
    _write_reference(scene_dir, normalized, camera_frames)
    manifest = {
        "schema_version": "scene-manifest/1",
        "scene_id": scene_id,
        "config": {
            "seed": int(seed),
            "frame_count": int(frame_count),
            "width": int(width),
            "height": int(height),
        },
        "layers": [layer.to_dict() for layer in LAYERS],
        "files": {
            **{version: path.name for version, path in scene_files.items()},
            "camera": camera_file.name,
            "reference_ellipses": reference_file.name,
        },
        "sha256": {
            **{version: sha256_file(path) for version, path in scene_files.items()},
            "camera": sha256_file(camera_file),
            "reference_ellipses": sha256_file(reference_file),
        },
        "reference_source": "v1 normalized scene",
        "deterministic_generator": "SplitMix64 seed stream; no wall-clock or external assets",
    }
    write_json_atomic(manifest_file, manifest)
    fingerprint_seed = "|".join([scene_id, str(manifest["sha256"])])
    from .storage import sha256_bytes

    manifest["scene_fingerprint"] = sha256_bytes(fingerprint_seed.encode("utf-8"))
    manifest["sha256"]["manifest_payload"] = sha256_bytes(
        json.dumps(manifest, sort_keys=True).encode("utf-8")
    )
    write_json_atomic(manifest_file, manifest)
    return GeneratedProject(
        root=root,
        scene_dir=scene_dir,
        config={
            "seed": int(seed),
            "frame_count": int(frame_count),
            "width": int(width),
            "height": int(height),
        },
        scene_files=scene_files,
        camera_file=camera_file,
        reference_file=reference_file,
        manifest_file=manifest_file,
        normalized_v1=normalized,
    )
