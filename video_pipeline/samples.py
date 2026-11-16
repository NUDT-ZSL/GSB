"""Deterministic synthetic AVI corpus generator used by demos and tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .atomic_io import atomic_write_json


@dataclass(frozen=True)
class SyntheticObject:
    object_id: str
    class_name: str
    color_bgr: tuple[int, int, int]
    width: int
    height: int
    start_x: int
    start_y: int
    velocity_x: int
    velocity_y: int
    start_frame: int = 0


@dataclass(frozen=True)
class SyntheticRoute:
    filename: str
    width: int
    height: int
    fps: float
    frame_count: int
    seed: int
    objects: tuple[SyntheticObject, ...]


VEHICLE_COLOR = (20, 20, 220)
PEDESTRIAN_COLOR = (220, 64, 32)


DEFAULT_ROUTES: tuple[SyntheticRoute, ...] = (
    SyntheticRoute(
        "cam_short.avi",
        320,
        240,
        12.0,
        8,
        101,
        (
            SyntheticObject("short-car", "vehicle", VEHICLE_COLOR, 48, 30, 20, 150, 8, 0, 1),
        ),
    ),
    SyntheticRoute(
        "cam_front.avi",
        384,
        288,
        15.0,
        18,
        202,
        (
            SyntheticObject("front-car", "vehicle", VEHICLE_COLOR, 50, 32, 24, 186, 9, 0),
            SyntheticObject("front-person", "pedestrian", PEDESTRIAN_COLOR, 18, 38, 324, 30, -3, 8, 2),
        ),
    ),
    SyntheticRoute(
        "cam_rear.avi",
        320,
        240,
        10.0,
        16,
        303,
        (
            SyntheticObject("rear-person", "pedestrian", PEDESTRIAN_COLOR, 18, 38, 80, 24, 6, 7, 1),
        ),
    ),
    SyntheticRoute(
        "cam_side.avi",
        416,
        312,
        12.0,
        20,
        404,
        (
            SyntheticObject("side-car", "vehicle", VEHICLE_COLOR, 54, 34, 34, 206, 10, -2, 2),
            SyntheticObject("side-person", "pedestrian", PEDESTRIAN_COLOR, 18, 38, 340, 42, -2, 8, 3),
        ),
    ),
)


def generate_sample_corpus(
    output_dir: str | Path,
    ground_truth_path: str | Path | None = None,
    *,
    routes: tuple[SyntheticRoute, ...] = DEFAULT_ROUTES,
    fourcc: str = "MJPG",
) -> dict[str, Any]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    ground_truth: dict[str, Any] = {
        "schema_version": 1,
        "generator": "video_pipeline.samples",
        "routes": {},
    }
    for route in routes:
        path = root / route.filename
        writer = cv2.VideoWriter(
            str(path),
            cv2.VideoWriter_fourcc(*fourcc),
            route.fps,
            (route.width, route.height),
        )
        if not writer.isOpened():
            raise RuntimeError(f"OpenCV cannot create AVI with fourcc {fourcc}: {path}")
        rng = np.random.default_rng(route.seed)
        route_truth: list[dict[str, Any]] = []
        try:
            for frame_index in range(route.frame_count):
                frame = _background(route, rng, frame_index)
                visible_objects = [obj for obj in route.objects if frame_index >= obj.start_frame]
                for obj in visible_objects:
                    bbox = _object_bbox(obj, frame_index, route.width, route.height)
                    cv2.rectangle(
                        frame,
                        (bbox[0], bbox[1]),
                        (bbox[2] - 1, bbox[3] - 1),
                        obj.color_bgr,
                        thickness=-1,
                    )
                    # A small darker window gives the synthetic vehicle/person an internal edge.
                    inset_x = max(3, obj.width // 5)
                    inset_y = max(3, obj.height // 4)
                    if obj.width > 2 * inset_x and obj.height > 2 * inset_y:
                        cv2.rectangle(
                            frame,
                            (bbox[0] + inset_x, bbox[1] + inset_y),
                            (bbox[2] - inset_x - 1, bbox[3] - inset_y - 1),
                            tuple(int(component * 0.72) for component in obj.color_bgr),
                            thickness=-1,
                        )
                    route_truth.append(
                        {
                            "object_id": obj.object_id,
                            "class_name": obj.class_name,
                            "frame_index": frame_index,
                            "bbox": list(bbox),
                        }
                    )
                writer.write(frame)
        finally:
            writer.release()
        ground_truth["routes"][route.filename] = {
            "width": route.width,
            "height": route.height,
            "fps": route.fps,
            "frame_count": route.frame_count,
            "frames": route_truth,
        }

    if ground_truth_path is not None:
        atomic_write_json(ground_truth_path, ground_truth)
    return ground_truth


def create_invalid_samples(output_dir: str | Path) -> list[Path]:
    """Create header-invalid and truncated-AVI samples for failure injection."""

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    bad_header = root / "bad_header.avi"
    bad_header.write_bytes(b"not an avi container" * 8)

    source = root / "source_for_truncation.avi"
    writer = cv2.VideoWriter(
        str(source), cv2.VideoWriter_fourcc(*"MJPG"), 10.0, (160, 120)
    )
    if not writer.isOpened():
        raise RuntimeError("cannot create truncation source video")
    for frame_index in range(12):
        frame = np.full((120, 160, 3), 32, dtype=np.uint8)
        cv2.rectangle(frame, (10 + frame_index * 8, 40), (70 + frame_index * 8, 75), VEHICLE_COLOR, -1)
        writer.write(frame)
    writer.release()
    data = source.read_bytes()
    truncated = root / "truncated.avi"
    truncated.write_bytes(data[: max(64, int(len(data) * 0.45))])
    try:
        source.unlink()
    except FileNotFoundError:
        pass
    return [bad_header, truncated]


def _background(route: SyntheticRoute, rng: np.random.Generator, frame_index: int) -> np.ndarray:
    base = np.empty((route.height, route.width, 3), dtype=np.int16)
    base[:, :, 0] = 34
    base[:, :, 1] = 38
    base[:, :, 2] = 42
    noise = rng.integers(-5, 6, size=(route.height, route.width, 1), dtype=np.int16)
    frame = np.clip(base + noise, 0, 255).astype(np.uint8)
    # Deterministic grayscale distractors contribute edges but not target colors.
    offset = (frame_index * 3) % 17
    cv2.circle(frame, (40 + offset, route.height - 36), 11, (170, 170, 170), 1)
    cv2.rectangle(frame, (route.width - 72, 18), (route.width - 18, 24), (115, 115, 115), 1)
    return frame


def _object_bbox(
    obj: SyntheticObject,
    frame_index: int,
    frame_width: int,
    frame_height: int,
) -> tuple[int, int, int, int]:
    relative = frame_index - obj.start_frame
    x = obj.start_x + obj.velocity_x * relative
    y = obj.start_y + obj.velocity_y * relative
    max_x = max(0, frame_width - obj.width)
    max_y = max(0, frame_height - obj.height)
    if max_x:
        x %= max_x + 1
    else:
        x = 0
    if max_y:
        y %= max_y + 1
    else:
        y = 0
    return (int(x), int(y), int(x + obj.width), int(y + obj.height))
