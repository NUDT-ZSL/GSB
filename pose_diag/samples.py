"""Deterministic meeting-scene generation and manifest validation."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .config import PipelineConfig
from .errors import BusinessError
from .io_utils import atomic_write_bytes, atomic_write_text, canonical_json_text
from .models import SceneRecord, SceneSample

KEYPOINT_TEMPLATE: tuple[tuple[str, float, float], ...] = (
    ("neck", 0.0, 0.0),
    ("left_shoulder", -18.0, 0.0),
    ("right_shoulder", 18.0, 0.0),
    ("left_elbow", -28.0, 15.0),
    ("right_elbow", 28.0, 15.0),
    ("left_wrist", -32.0, 30.0),
    ("right_wrist", 32.0, 30.0),
    ("left_hip", -11.0, 22.0),
    ("right_hip", 11.0, 22.0),
    ("nose", 0.0, -20.0),
    ("left_eye", -7.0, -27.0),
    ("right_eye", 7.0, -27.0),
)

SKELETON_EDGES: tuple[tuple[int, int], ...] = (
    (0, 1),
    (0, 2),
    (1, 3),
    (3, 5),
    (2, 4),
    (4, 6),
    (1, 7),
    (2, 8),
    (0, 9),
    (9, 10),
    (9, 11),
)

PERSON_COLORS = (
    (238, 109, 102),
    (92, 183, 239),
    (119, 214, 162),
    (231, 183, 88),
)


@dataclass(frozen=True)
class GeneratedPaths:
    out_dir: Path
    manifest_path: Path
    scene_paths: tuple[Path, ...]


def _scene_slots(person_count: int, width: int, height: int) -> list[tuple[float, float]]:
    if person_count < 1:
        return []
    margin = width * 0.12
    usable = width - 2 * margin
    if person_count == 1:
        return [(width / 2.0, height * 0.53)]
    return [
        (margin + usable * index / (person_count - 1), height * 0.53)
        for index in range(person_count)
    ]


def _draw_disc(canvas: np.ndarray, x: int, y: int, radius: int, color: tuple[int, int, int]) -> None:
    height, width = canvas.shape[:2]
    x0, x1 = max(0, x - radius), min(width, x + radius + 1)
    y0, y1 = max(0, y - radius), min(height, y + radius + 1)
    if x0 >= x1 or y0 >= y1:
        return
    yy, xx = np.ogrid[y0:y1, x0:x1]
    mask = (xx - x) ** 2 + (yy - y) ** 2 <= radius * radius
    canvas[y0:y1, x0:x1][mask] = color


def _draw_line(
    canvas: np.ndarray,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    color: tuple[int, int, int],
) -> None:
    steps = max(abs(x1 - x0), abs(y1 - y0), 1)
    for step in range(steps + 1):
        t = step / steps
        x = int(round(x0 + (x1 - x0) * t))
        y = int(round(y0 + (y1 - y0) * t))
        if 0 <= y < canvas.shape[0] and 0 <= x < canvas.shape[1]:
            canvas[y, x] = color


def _paint_canvas_and_people(
    seed: int,
    canvas_shape: tuple[int, int, int],
    person_count: int,
    occlusion_ratio: float,
) -> tuple[np.ndarray, list[dict[str, Any]]]:
    height, width, channels = canvas_shape
    if channels != 3:
        raise BusinessError(
            "E_CANVAS_INVALID",
            "canvas_shape must describe an RGB image",
            field="canvas_shape",
            details={"canvas_shape": list(canvas_shape)},
        )
    rng = np.random.Generator(np.random.PCG64(seed))
    canvas = np.zeros(canvas_shape, dtype=np.uint8)
    canvas[:, :, :] = np.array([29, 34, 43], dtype=np.uint8)

    table_top = int(height * 0.68)
    table_color = np.array([64, 72, 84], dtype=np.uint8)
    canvas[table_top:, :, :] = table_color
    canvas[table_top : table_top + 3, :, :] = np.array([94, 104, 118], dtype=np.uint8)
    for index in range(5):
        gx = int(width * (index + 0.5) / 5)
        _draw_line(canvas, gx, table_top, int(width / 2), height, (82, 91, 104))

    slots = _scene_slots(person_count, width, height)
    people: list[dict[str, Any]] = []
    num_keypoints = len(KEYPOINT_TEMPLATE)
    occluded_count = int(round(person_count * num_keypoints * occlusion_ratio))
    occluded_flat = {
        int(value)
        for value in rng.choice(
            person_count * num_keypoints, size=occluded_count, replace=False
        )
    }

    for person_index, (center_x, center_y) in enumerate(slots):
        color = PERSON_COLORS[person_index % len(PERSON_COLORS)]
        scale = float(rng.uniform(0.94, 1.06))
        keypoints: list[dict[str, Any]] = []
        for channel, (name, dx, dy) in enumerate(KEYPOINT_TEMPLATE):
            jitter_x = float(rng.uniform(-2.5, 2.5))
            jitter_y = float(rng.uniform(-2.5, 2.5))
            x = float(np.clip(center_x + dx * scale + jitter_x, 2, width - 3))
            y = float(np.clip(center_y + dy * scale + jitter_y, 2, height - 3))
            visible = bool(rng.random() >= 0.025)
            occluded = (person_index * num_keypoints + channel) in occluded_flat
            keypoints.append(
                {
                    "channel": channel,
                    "name": name,
                    "x": round(x, 3),
                    "y": round(y, 3),
                    "visible": visible,
                    "occluded": occluded,
                }
            )

        person = {
            "person_id": f"p{person_index:02d}",
            "center": [round(center_x, 3), round(center_y, 3)],
            "keypoints": keypoints,
        }
        people.append(person)

        shoulder = keypoints[0]
        _draw_disc(
            canvas,
            int(round(shoulder["x"])),
            int(round(shoulder["y"])),
            9,
            color,
        )
        for left, right in SKELETON_EDGES:
            kp_left = keypoints[left]
            kp_right = keypoints[right]
            _draw_line(
                canvas,
                int(round(kp_left["x"])),
                int(round(kp_left["y"])),
                int(round(kp_right["x"])),
                int(round(kp_right["y"])),
                color,
            )
        for keypoint in keypoints:
            radius = 2 if keypoint["visible"] else 1
            _draw_disc(
                canvas,
                int(round(keypoint["x"])),
                int(round(keypoint["y"])),
                radius,
                (245, 245, 245) if not keypoint["occluded"] else (145, 145, 145),
            )
    return canvas, people


def _reconstruct_heatmaps(
    heatmap_seed: int,
    canvas_shape: tuple[int, int, int],
    people: list[dict[str, Any]],
    occlusion_ratio: float,
) -> tuple[np.ndarray, np.ndarray]:
    height, width, _ = canvas_shape
    channels = len(KEYPOINT_TEMPLATE)
    truth = np.zeros((channels, height, width), dtype=np.float32)
    sigma = 2.2
    radius = int(math.ceil(3.5 * sigma))
    yy = np.arange(-radius, radius + 1, dtype=np.float32)
    grid_y, grid_x = np.meshgrid(yy, yy, indexing="xy")
    gaussian = np.exp(-(grid_x * grid_x + grid_y * grid_y) / (2.0 * sigma * sigma))

    factors: dict[tuple[int, int], float] = {}
    for person_index, person in enumerate(people):
        for keypoint in person["keypoints"]:
            channel = int(keypoint["channel"])
            cx = int(round(float(keypoint["x"])))
            cy = int(round(float(keypoint["y"])))
            x0, x1 = max(0, cx - radius), min(width, cx + radius + 1)
            y0, y1 = max(0, cy - radius), min(height, cy + radius + 1)
            gx0 = x0 - (cx - radius)
            gx1 = gx0 + (x1 - x0)
            gy0 = y0 - (cy - radius)
            gy1 = gy0 + (y1 - y0)
            truth[channel, y0:y1, x0:x1] += gaussian[gy0:gy1, gx0:gx1]
            factor = 1.0
            if not bool(keypoint["visible"]):
                factor *= 0.45
            if bool(keypoint["occluded"]):
                factor *= 0.32
            factors[(person_index, channel)] = factor

    rng = np.random.Generator(np.random.PCG64(heatmap_seed))
    noise = rng.uniform(0.0, 0.005, size=truth.shape).astype(np.float32)
    observation = np.zeros_like(truth)
    for person_index, person in enumerate(people):
        for keypoint in person["keypoints"]:
            channel = int(keypoint["channel"])
            cx = int(round(float(keypoint["x"])))
            cy = int(round(float(keypoint["y"])))
            x0, x1 = max(0, cx - radius), min(width, cx + radius + 1)
            y0, y1 = max(0, cy - radius), min(height, cy + radius + 1)
            gx0 = x0 - (cx - radius)
            gx1 = gx0 + (x1 - x0)
            gy0 = y0 - (cy - radius)
            gy1 = gy0 + (y1 - y0)
            observation[channel, y0:y1, x0:x1] += (
                gaussian[gy0:gy1, gx0:gx1] * factors[(person_index, channel)]
            )
    observation += noise
    return np.clip(truth, 0.0, 1.0).astype(np.float32), observation.astype(np.float32)


def reconstruct_heatmaps_for_scene(
    record: SceneRecord,
    people: list[dict[str, Any]],
) -> tuple[np.ndarray, np.ndarray]:
    if len(people) != record.person_count:
        raise BusinessError(
            "E_PERSON_COUNT_MISMATCH",
            "person_count does not match reconstructed skeleton count",
            field="person_count",
            scene_id=record.scene_id,
            details={
                "person_count": record.person_count,
                "skeletons": len(people),
            },
        )
    for person in people:
        channels = [int(item["channel"]) for item in person.get("keypoints", [])]
        if len(channels) != record.num_keypoints or sorted(channels) != list(
            range(record.num_keypoints)
        ):
            raise BusinessError(
                "E_CHANNEL_MISMATCH",
                "skeleton channel set does not match num_keypoints",
                field="num_keypoints",
                scene_id=record.scene_id,
                details={
                    "num_keypoints": record.num_keypoints,
                    "channels": channels,
                },
            )
    return _reconstruct_heatmaps(
        record.heatmap_seed,
        record.canvas_shape,
        people,
        record.occlusion_ratio,
    )


def _metadata_for_scene(
    record: SceneRecord, people: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "num_keypoints": record.num_keypoints,
        "channels": [
            {"channel": index, "name": name, "weight": 1.0}
            for index, (name, _, _) in enumerate(KEYPOINT_TEMPLATE)
        ],
        "edges": [list(edge) for edge in SKELETON_EDGES],
        "link_distance": 30.0,
        "min_keypoints": 7,
        "max_instance_diameter": 90.0,
        "persons": people,
    }


def generate_samples(
    out_dir: str | Path,
    config: PipelineConfig,
    scene_count: int = 3,
) -> GeneratedPaths:
    if scene_count < 1:
        raise BusinessError(
            "E_CONFIG_INVALID", "scene_count must be positive", field="scene_count"
        )
    root = Path(out_dir)
    samples_dir = root
    scenes_dir = samples_dir / "scenes"
    samples_dir.mkdir(parents=True, exist_ok=True)
    scenes_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = samples_dir / "manifest.jsonl"
    lines: list[str] = []
    scene_paths: list[Path] = []

    for index in range(scene_count):
        scene_id = f"scene-{index + 1:04d}"
        scene_seed = 3407 + index * 97
        heatmap_seed = 90001 + index * 131
        person_count = (2, 3, 4)[index % 3]
        canvas_shape = (480, 640, 3)
        occlusion_ratio = round(0.08 + 0.03 * (index % 4), 4)
        record = SceneRecord(
            scene_id=scene_id,
            scene_seed=scene_seed,
            canvas_shape=canvas_shape,
            person_count=person_count,
            num_keypoints=len(KEYPOINT_TEMPLATE),
            heatmap_seed=heatmap_seed,
            occlusion_ratio=occlusion_ratio,
            policy="meeting-default-v1",
            sample_dir=f"scenes/{scene_id}",
        )
        canvas, people = _paint_canvas_and_people(
            scene_seed,
            canvas_shape,
            person_count,
            occlusion_ratio,
        )
        truth, observation = _reconstruct_heatmaps(
            heatmap_seed, canvas_shape, people, occlusion_ratio
        )
        metadata = _metadata_for_scene(record, people)
        scene_payload = {
            **record.to_dict(),
            "metadata": metadata,
            "scoring": dict(config.scoring),
        }
        scene_dir = scenes_dir / scene_id
        scene_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(
            scene_dir / "canvas.npy",
            _npy_bytes(canvas),
        )
        atomic_write_bytes(
            scene_dir / "truth_heatmaps.npy",
            _npy_bytes(truth),
        )
        atomic_write_bytes(
            scene_dir / "observation_heatmaps.npy",
            _npy_bytes(observation),
        )
        atomic_write_text(
            scene_dir / "scene.json",
            canonical_json_text(scene_payload) + "\n",
        )
        lines.append(canonical_json_text(record.to_dict()))
        scene_paths.append(scene_dir)

    atomic_write_text(manifest_path, "\n".join(lines) + "\n")
    return GeneratedPaths(
        out_dir=samples_dir,
        manifest_path=manifest_path,
        scene_paths=tuple(scene_paths),
    )


def _npy_bytes(array: np.ndarray) -> bytes:
    import io

    buffer = io.BytesIO()
    np.save(buffer, array, allow_pickle=False)
    return buffer.getvalue()


def load_manifest(path: str | Path) -> list[SceneRecord]:
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise BusinessError(
            "E_MANIFEST_MISSING",
            "manifest.jsonl does not exist",
            field="manifest",
            details={"path": str(manifest_path)},
        )
    records: list[SceneRecord] = []
    seen: set[str] = set()
    with manifest_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                record = SceneRecord.from_dict(raw)
            except (ValueError, TypeError) as exc:
                raise BusinessError(
                    "E_MANIFEST_INVALID",
                    "manifest line is not a valid scene record",
                    field="manifest",
                    details={"line": line_number, "reason": str(exc)},
                ) from exc
            if not record.scene_id:
                raise BusinessError(
                    "E_MANIFEST_INVALID",
                    "scene_id must be a non-empty string",
                    field="scene_id",
                    details={"line": line_number},
                )
            if record.scene_id in seen:
                raise BusinessError(
                    "E_DUPLICATE_SCENE_ID",
                    "duplicate scene_id in manifest",
                    field="scene_id",
                    scene_id=record.scene_id,
                    details={"line": line_number},
                )
            seen.add(record.scene_id)
            records.append(record)
    if not records:
        raise BusinessError("E_MANIFEST_EMPTY", "manifest contains no scenes", field="manifest")
    return records


def validate_scene_record(record: SceneRecord, config: PipelineConfig) -> None:
    shape = record.canvas_shape
    if (
        not isinstance(shape, (tuple, list))
        or len(shape) != 3
        or shape[2] != 3
        or int(shape[0]) <= 0
        or int(shape[1]) <= 0
    ):
        raise BusinessError(
            "E_CANVAS_INVALID",
            "canvas_shape must be [height, width, 3] with positive dimensions",
            field="canvas_shape",
            scene_id=record.scene_id,
            details={"canvas_shape": list(shape)},
        )
    if not 0.0 <= record.occlusion_ratio <= 1.0:
        raise BusinessError(
            "E_OCCLUSION_RATIO",
            "occlusion_ratio must be within [0, 1]",
            field="occlusion_ratio",
            scene_id=record.scene_id,
            details={"value": record.occlusion_ratio},
        )
    if record.person_count < 1 or record.num_keypoints < 1:
        raise BusinessError(
            "E_PERSON_COUNT_MISMATCH",
            "person_count and num_keypoints must be positive",
            field="person_count",
            scene_id=record.scene_id,
            details={
                "person_count": record.person_count,
                "num_keypoints": record.num_keypoints,
            },
        )
    pixels = int(shape[0]) * int(shape[1])
    if pixels > config.max_scene_pixels:
        raise BusinessError(
            "E_RESOURCE_LIMIT",
            "scene pixel count exceeds the configured limit",
            field="canvas_shape",
            scene_id=record.scene_id,
            details={
                "pixels": pixels,
                "max_scene_pixels": config.max_scene_pixels,
            },
        )
    peak_memory_bytes = (
        record.num_keypoints * pixels * np.dtype(np.float32).itemsize * 2
        + record.num_keypoints * 2048 * 256
    )
    if peak_memory_bytes > config.max_peak_memory_mb * 1024 * 1024:
        raise BusinessError(
            "E_RESOURCE_LIMIT",
            "estimated peak residency exceeds the configured memory limit",
            field="max_peak_memory_mb",
            scene_id=record.scene_id,
            details={
                "estimated_bytes": peak_memory_bytes,
                "max_peak_memory_mb": config.max_peak_memory_mb,
            },
        )


def load_scene_sample(
    manifest_path: str | Path,
    record: SceneRecord,
    config: PipelineConfig,
) -> SceneSample:
    validate_scene_record(record, config)
    base = Path(manifest_path).parent
    sample_dir = base / record.sample_dir
    scene_path = sample_dir / "scene.json"
    try:
        scene_payload = json.loads(scene_path.read_text(encoding="utf-8"))
        canvas = np.load(sample_dir / "canvas.npy", allow_pickle=False)
        truth = np.load(sample_dir / "truth_heatmaps.npy", allow_pickle=False)
        observation = np.load(
            sample_dir / "observation_heatmaps.npy", allow_pickle=False
        )
    except (OSError, ValueError) as exc:
        raise BusinessError(
            "E_SAMPLE_CORRUPT",
            "deterministic scene sample could not be loaded",
            field="sample_dir",
            scene_id=record.scene_id,
            details={"path": str(sample_dir), "reason": str(exc)},
        ) from exc
    metadata = scene_payload.get("metadata", {})
    scoring = scene_payload.get("scoring", {})
    persons = metadata.get("persons", [])
    if len(persons) != record.person_count:
        raise BusinessError(
            "E_PERSON_COUNT_MISMATCH",
            "person_count does not match keypoint metadata person entries",
            field="person_count",
            scene_id=record.scene_id,
            details={
                "person_count": record.person_count,
                "metadata_persons": len(persons),
            },
        )
    if observation.ndim != 3 or observation.shape[0] != record.num_keypoints:
        raise BusinessError(
            "E_CHANNEL_MISMATCH",
            "heatmap channel count does not match num_keypoints",
            field="num_keypoints",
            scene_id=record.scene_id,
            details={
                "heatmap_channels": int(observation.shape[0]) if observation.ndim else 0,
                "num_keypoints": record.num_keypoints,
            },
        )
    if tuple(observation.shape[1:]) != tuple(record.canvas_shape[:2]):
        raise BusinessError(
            "E_CANVAS_INVALID",
            "heatmap spatial shape does not match canvas_shape",
            field="canvas_shape",
            scene_id=record.scene_id,
            details={
                "heatmap_shape": list(observation.shape),
                "canvas_shape": list(record.canvas_shape),
            },
        )
    config.validate_scoring(scoring)
    return SceneSample(
        record=record,
        canvas=canvas,
        truth_heatmaps=truth,
        observation_heatmaps=observation,
        metadata=metadata,
        scoring=scoring,
    )


def build_region_specs(
    config: PipelineConfig, canvas_shape: tuple[int, int, int]
) -> list[Any]:
    from .models import RegionSpec

    regions: list[RegionSpec] = []
    for row in range(config.region_rows):
        for col in range(config.region_cols):
            x0, y0, x1, y1 = config.region_bbox(row, col, canvas_shape)
            regions.append(RegionSpec(row, col, x0, y0, x1, y1))
    return regions


def iter_manifest_records(path: str | Path) -> Iterable[SceneRecord]:
    yield from load_manifest(path)

