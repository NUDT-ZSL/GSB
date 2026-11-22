"""Deterministic local sample generation and validated input loading."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .config import (
    FRAME_HEIGHT,
    FRAME_WIDTH,
    SUPPORTED_MOTION_MODES,
    SUPPORTED_TEMPORAL_POLICIES,
    PipelineConfig,
)
from .deterministic import atomic_write_json, write_jsonl
from .errors import BusinessError


MANIFEST_FIELDS = (
    "clip_id",
    "frame_count",
    "frame_interval_ms",
    "object_count",
    "motion_mode",
    "seed",
    "temporal_policy",
)


def _derive_seed(seed: int, clip_id: str, object_count: int) -> int:
    material = f"motion-pipeline-v1|{seed}|{clip_id}|{object_count}".encode("utf-8")
    return int(hashlib.sha256(material).hexdigest()[:16], 16)


def _clip_specs(seed: int, clip_count: int) -> list[dict[str, Any]]:
    if clip_count < 1:
        raise BusinessError(
            "E_CONFIG_INVALID", "clip count must be positive", field="clip_count"
        )
    specs: list[dict[str, Any]] = []
    modes = ("constant", "accelerated", "periodic")
    policies = ("every_frame", "stride_2", "stride_3")
    intervals = (20, 25, 40)
    for index in range(clip_count):
        material = f"sample-spec|{seed}|{index}".encode("utf-8")
        digest = hashlib.sha256(material).digest()
        clip_id = f"clip-{index + 1:04d}"
        frame_count = 22 + (digest[0] % 11)
        object_count = 2 + (digest[1] % 3)
        specs.append(
            {
                "clip_id": clip_id,
                "frame_count": frame_count,
                "frame_interval_ms": intervals[digest[2] % len(intervals)],
                "object_count": object_count,
                "motion_mode": modes[index % len(modes)],
                "seed": (seed + int.from_bytes(digest[3:7], "big")) % (2**31 - 1),
                "temporal_policy": policies[index % len(policies)],
            }
        )
    specs.sort(key=lambda item: item["clip_id"])
    return specs


def _object_parameters(
    spec: dict[str, Any], object_id: int
) -> dict[str, float]:
    seed = _derive_seed(spec["seed"], spec["clip_id"], spec["object_count"])
    rng = np.random.Generator(np.random.PCG64(seed + object_id * 104729))
    count = spec["object_count"]
    lane = (FRAME_HEIGHT - 8.0) * (object_id + 0.5) / count + 4.0
    base_x = 9.0 + float(rng.uniform(-3.0, 3.0))
    mode = spec["motion_mode"]
    if mode == "periodic":
        x = 5.0 + float(rng.uniform(0.0, FRAME_WIDTH - 10.0))
        y = lane
        vx = 0.0
        vy = 0.0
        ax = 0.0
        ay = 0.0
        amplitude_x = float(rng.uniform(3.0, 6.0))
        amplitude_y = float(rng.uniform(0.5, 1.5))
        period_s = float(rng.uniform(0.55, 1.35))
        phase = float(rng.uniform(0.0, 2.0 * math.pi))
        return {
            "base_x": x,
            "base_y": y,
            "vx": vx,
            "vy": vy,
            "ax": ax,
            "ay": ay,
            "amplitude_x": amplitude_x,
            "amplitude_y": amplitude_y,
            "period_s": period_s,
            "phase": phase,
        }
    return {
        "base_x": base_x,
        "base_y": lane,
        "vx": float(rng.uniform(-5.0, 5.0)),
        "vy": float(rng.uniform(-1.0, 1.0)),
        "ax": float(rng.uniform(-1.2, 1.2)) if mode == "accelerated" else 0.0,
        "ay": float(rng.uniform(-0.25, 0.25)) if mode == "accelerated" else 0.0,
        "amplitude_x": 0.0,
        "amplitude_y": 0.0,
        "period_s": 1.0,
        "phase": 0.0,
    }


def _clamp_center(value: float, size: int) -> float:
    return min(size - 3.0, max(2.0, value))


def _state_at(
    params: dict[str, float], mode: str, time_s: float
) -> tuple[float, float, float, float, float, float]:
    if mode == "periodic":
        omega = 2.0 * math.pi / params["period_s"]
        x = params["base_x"] + params["amplitude_x"] * math.sin(
            omega * time_s + params["phase"]
        )
        y = params["base_y"] + params["amplitude_y"] * math.sin(
            2.0 * omega * time_s + params["phase"] * 0.5
        )
        vx = params["amplitude_x"] * omega * math.cos(
            omega * time_s + params["phase"]
        )
        vy = (
            2.0
            * params["amplitude_y"]
            * omega
            * math.cos(2.0 * omega * time_s + params["phase"] * 0.5)
        )
        ax = -params["amplitude_x"] * omega * omega * math.sin(
            omega * time_s + params["phase"]
        )
        ay = (
            -4.0
            * params["amplitude_y"]
            * omega
            * omega
            * math.sin(2.0 * omega * time_s + params["phase"] * 0.5)
        )
        return (
            _clamp_center(x, FRAME_WIDTH),
            _clamp_center(y, FRAME_HEIGHT),
            vx,
            vy,
            ax,
            ay,
        )
    x = params["base_x"] + params["vx"] * time_s + 0.5 * params["ax"] * time_s**2
    y = params["base_y"] + params["vy"] * time_s + 0.5 * params["ay"] * time_s**2
    vx = params["vx"] + params["ax"] * time_s
    vy = params["vy"] + params["ay"] * time_s
    return (
        _clamp_center(x, FRAME_WIDTH),
        _clamp_center(y, FRAME_HEIGHT),
        vx,
        vy,
        params["ax"],
        params["ay"],
    )


def _blob(center_x: float, center_y: float) -> list[tuple[int, int]]:
    cx = int(round(center_x))
    cy = int(round(center_y))
    return [
        (row, col)
        for row in range(cy - 1, cy + 2)
        for col in range(cx - 1, cx + 2)
        if 0 <= row < FRAME_HEIGHT and 0 <= col < FRAME_WIDTH
    ]


def _rounded(value: float) -> float:
    return round(float(value), 6)


def _generate_clip_frames(spec: dict[str, Any]) -> dict[str, Any]:
    mode = spec["motion_mode"]
    interval_s = spec["frame_interval_ms"] / 1000.0
    params = [_object_parameters(spec, object_id) for object_id in range(spec["object_count"])]
    frames: list[dict[str, Any]] = []
    for frame_index in range(spec["frame_count"]):
        time_s = frame_index * interval_s
        objects: list[dict[str, Any]] = []
        mask: list[list[int]] = []
        pixels: list[list[int]] = []
        for object_id, object_params in enumerate(params):
            cx, cy, vx, vy, ax, ay = _state_at(object_params, mode, time_s)
            objects.append(
                {
                    "object_id": object_id,
                    "cx": _rounded(cx),
                    "cy": _rounded(cy),
                    "w": 4.0,
                    "h": 4.0,
                    "vx": _rounded(vx),
                    "vy": _rounded(vy),
                    "ax": _rounded(ax),
                    "ay": _rounded(ay),
                }
            )
            intensity = 80 + object_id * 30
            for row, col in _blob(cx, cy):
                mask.append([row, col, object_id])
                pixels.append([row, col, intensity])
        mask.sort(key=lambda item: (item[0], item[1], item[2]))
        pixels.sort(key=lambda item: (item[0], item[1], item[2]))
        frames.append(
            {
                "frame_index": frame_index,
                "timestamp_ms": frame_index * spec["frame_interval_ms"],
                "shape": [FRAME_HEIGHT, FRAME_WIDTH],
                "mask": mask,
                "pixels": pixels,
                "objects": objects,
            }
        )
    return {
        "clip_id": spec["clip_id"],
        "frame_interval_ms": spec["frame_interval_ms"],
        "shape": [FRAME_HEIGHT, FRAME_WIDTH],
        "frames": frames,
    }


def generate_dataset(
    output_dir: str | Path,
    *,
    seed: int = 20261123,
    clip_count: int = 6,
) -> dict[str, Any]:
    """Create a manifest and raw frame file without system entropy/time."""

    output = Path(output_dir)
    if not isinstance(seed, int) or seed < 0:
        raise BusinessError(
            "E_CONFIG_INVALID", "seed must be a non-negative integer", field="seed"
        )
    specs = _clip_specs(seed, clip_count)
    clips = [_generate_clip_frames(spec) for spec in specs]
    output.mkdir(parents=True, exist_ok=True)
    write_jsonl(output / "manifest.jsonl", specs)
    write_jsonl(output / "frames.jsonl", clips)
    metadata = {
        "schema_version": 1,
        "generator": "motion-pipeline-deterministic-v1",
        "seed": seed,
        "clip_count": clip_count,
        "frame_height": FRAME_HEIGHT,
        "frame_width": FRAME_WIDTH,
        "manifest_sha256": hashlib.sha256(
            b"".join(
                json.dumps(
                    spec, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
                + b"\n"
                for spec in specs
            )
        ).hexdigest(),
        "frames_sha256": hashlib.sha256(
            b"".join(
                json.dumps(
                    clip, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
                + b"\n"
                for clip in clips
            )
        ).hexdigest(),
    }
    atomic_write_json(output / "dataset_meta.json", metadata)
    return {
        "manifest": str(output / "manifest.jsonl"),
        "frames": str(output / "frames.jsonl"),
        "meta": str(output / "dataset_meta.json"),
        "clip_count": clip_count,
        "seed": seed,
        "metadata": metadata,
    }


def _load_jsonl(path: Path, error_code: str) -> list[Any]:
    if not path.exists():
        raise BusinessError(error_code, "input file does not exist", field=str(path))
    records: list[Any] = []
    with path.open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                records.append(json.loads(raw))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise BusinessError(
                    "E_MANIFEST_INVALID",
                    "input JSONL line is not valid JSON",
                    field=f"line:{line_number}",
                    detail={"path": str(path), "error": str(exc)},
                ) from exc
    return records


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_manifest(
    records: Iterable[Any], config: PipelineConfig
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    degradations: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            raise BusinessError(
                "E_MANIFEST_INVALID", "manifest record must be an object", field="record"
            )
        missing = [name for name in MANIFEST_FIELDS if name not in record]
        if missing:
            raise BusinessError(
                "E_MANIFEST_INVALID",
                "manifest record is missing required fields",
                field=missing[0],
                detail={"missing": missing},
            )
        clip_id = record["clip_id"]
        if not isinstance(clip_id, str) or not clip_id:
            raise BusinessError(
                "E_MANIFEST_INVALID",
                "clip_id must be a non-empty string",
                field="clip_id",
            )
        if clip_id in seen:
            raise BusinessError(
                "E_DUPLICATE_CLIP_ID",
                "clip_id must be unique",
                field="clip_id",
                clip_id=clip_id,
            )
        seen.add(clip_id)
        if not _integer(record["frame_count"]) or record["frame_count"] < 3:
            raise BusinessError(
                "E_FRAME_COUNT_MISMATCH",
                "frame_count must be an integer >= 3",
                field="frame_count",
                clip_id=clip_id,
                detail={"value": record["frame_count"]},
            )
        if record["frame_count"] > config.max_frames_per_clip:
            raise BusinessError(
                "E_RESOURCE_LIMIT",
                "clip frame count exceeds the configured limit",
                field="frame_count",
                clip_id=clip_id,
                limit=config.max_frames_per_clip,
                detail={"requested": record["frame_count"]},
            )
        if not _integer(record["frame_interval_ms"]) or record["frame_interval_ms"] <= 0:
            raise BusinessError(
                "E_FRAME_INTERVAL_INVALID",
                "frame_interval_ms must be a positive integer",
                field="frame_interval_ms",
                clip_id=clip_id,
            )
        if (
            not _integer(record["object_count"])
            or record["object_count"] < 1
            or record["object_count"] > 8
        ):
            raise BusinessError(
                "E_MANIFEST_INVALID",
                "object_count must be an integer between 1 and 8",
            field="object_count",
            clip_id=clip_id,
            )
        if record["motion_mode"] not in SUPPORTED_MOTION_MODES:
            raise BusinessError(
                "E_MOTION_MODE_INVALID",
                "motion_mode is not in the controlled set",
                field="motion_mode",
                clip_id=clip_id,
                detail={"allowed": sorted(SUPPORTED_MOTION_MODES)},
            )
        if not _integer(record["seed"]) or record["seed"] < 0:
            raise BusinessError(
                "E_MANIFEST_INVALID",
                "seed must be a non-negative integer",
                field="seed",
                clip_id=clip_id,
            )
        if record["temporal_policy"] not in SUPPORTED_TEMPORAL_POLICIES:
            raise BusinessError(
                "E_TEMPORAL_POLICY_INVALID",
                "temporal_policy is not in the controlled set",
                field="temporal_policy",
                clip_id=clip_id,
                detail={"allowed": sorted(SUPPORTED_TEMPORAL_POLICIES)},
            )
        specs.append({name: record[name] for name in MANIFEST_FIELDS})
    specs.sort(key=lambda item: item["clip_id"])
    if not specs:
        raise BusinessError(
            "E_MANIFEST_INVALID", "manifest contains no clips", field="manifest"
        )
    config.validate_temporal_parameters(specs[0]["clip_id"])
    degradations.extend(config.parallel_degradations(len(specs)))
    resident_limit = config.max_resident_history_windows
    for spec in specs:
        if spec["object_count"] <= resident_limit:
            continue
        if config.resource_policy == "error":
            raise BusinessError(
                "E_RESOURCE_LIMIT",
                "resident history windows exceed the configured limit",
                field="object_count",
                clip_id=spec["clip_id"],
                limit=resident_limit,
                detail={"requested": spec["object_count"]},
            )
        degradations.append(
            {
                "category": "history_window_batched",
                "clip_id": spec["clip_id"],
                "requested_history_windows": spec["object_count"],
                "effective_batch_size": resident_limit,
                "limit": resident_limit,
            }
        )
    return specs, degradations


def _validate_frames(
    raw_clip: Any, spec: dict[str, Any], config: PipelineConfig
) -> dict[str, Any]:
    clip_id = spec["clip_id"]
    if not isinstance(raw_clip, dict):
        raise BusinessError(
            "E_FRAME_DATA_INVALID",
            "raw clip record must be an object",
            field="frames",
            clip_id=clip_id,
        )
    if raw_clip.get("clip_id") != clip_id:
        raise BusinessError(
            "E_FRAME_DATA_INVALID",
            "frame data clip id does not match the manifest",
            field="clip_id",
            clip_id=clip_id,
        )
    frames = raw_clip.get("frames")
    if not isinstance(frames, list):
        raise BusinessError(
            "E_FRAME_DATA_INVALID",
            "frames must be a list",
            field="frames",
            clip_id=clip_id,
        )
    if len(frames) != spec["frame_count"]:
        raise BusinessError(
            "E_FRAME_COUNT_MISMATCH",
            "frame_count does not equal the number of frame records",
            field="frame_count",
            clip_id=clip_id,
            detail={"declared": spec["frame_count"], "actual": len(frames)},
        )
    expected_shape = [FRAME_HEIGHT, FRAME_WIDTH]
    for expected_index, frame in enumerate(frames):
        if not isinstance(frame, dict):
            raise BusinessError(
                "E_FRAME_DATA_INVALID",
                "frame record must be an object",
                field="frames",
                clip_id=clip_id,
            )
        if frame.get("frame_index") != expected_index:
            raise BusinessError(
                "E_FRAME_DATA_INVALID",
                "frame indices must be contiguous and start at zero",
                field="frame_index",
                clip_id=clip_id,
                detail={"expected": expected_index, "actual": frame.get("frame_index")},
            )
        if frame.get("shape") != expected_shape:
            raise BusinessError(
                "E_MASK_SHAPE_MISMATCH",
                "mask canvas dimensions do not match the fixed frame shape",
                field="mask.shape",
                clip_id=clip_id,
                detail={"expected": expected_shape, "actual": frame.get("shape")},
            )
        timestamp = frame.get("timestamp_ms")
        expected_timestamp = expected_index * spec["frame_interval_ms"]
        if not _integer(timestamp) or timestamp != expected_timestamp:
            raise BusinessError(
                "E_FRAME_DATA_INVALID",
                "frame timestamp does not match frame_interval_ms",
                field="timestamp_ms",
                clip_id=clip_id,
            )
        mask = frame.get("mask")
        pixels = frame.get("pixels")
        objects = frame.get("objects")
        if not isinstance(mask, list) or not isinstance(pixels, list):
            raise BusinessError(
                "E_FRAME_DATA_INVALID",
                "mask and pixels must be lists",
                field="mask",
                clip_id=clip_id,
            )
        if not isinstance(objects, list) or len(objects) != spec["object_count"]:
            raise BusinessError(
                "E_OBJECT_STATE_INVALID",
                "objects must contain the declared object count",
                field="objects",
                clip_id=clip_id,
            )
        object_ids = [item.get("object_id") for item in objects if isinstance(item, dict)]
        if sorted(object_ids) != list(range(spec["object_count"])):
            raise BusinessError(
                "E_OBJECT_STATE_INVALID",
                "object state ids must be contiguous",
                field="objects.object_id",
                clip_id=clip_id,
            )
        coordinates: set[tuple[int, int]] = set()
        for mask_entry in mask:
            if (
                not isinstance(mask_entry, list)
                or len(mask_entry) != 3
                or not all(_integer(item) for item in mask_entry)
            ):
                raise BusinessError(
                    "E_MASK_SHAPE_MISMATCH",
                    "sparse mask entries must be [row, col, object_id] integers",
                    field="mask",
                    clip_id=clip_id,
                )
            row, col, object_id = mask_entry
            if (
                row < 0
                or row >= FRAME_HEIGHT
                or col < 0
                or col >= FRAME_WIDTH
                or object_id < 0
                or object_id >= spec["object_count"]
            ):
                raise BusinessError(
                    "E_MASK_SHAPE_MISMATCH",
                    "sparse mask coordinate or object id is outside the frame",
                    field="mask",
                    clip_id=clip_id,
                )
            if (row, col) in coordinates:
                raise BusinessError(
                    "E_MASK_SHAPE_MISMATCH",
                    "sparse mask contains a duplicate coordinate",
                    field="mask",
                    clip_id=clip_id,
                )
            coordinates.add((row, col))
        if len(coordinates) > FRAME_HEIGHT * FRAME_WIDTH:
            raise BusinessError(
                "E_MASK_SHAPE_MISMATCH",
                "sparse mask exceeds the frame canvas",
                field="mask",
                clip_id=clip_id,
            )
        pixel_coordinates: list[tuple[int, int]] = []
        for pixel_entry in pixels:
            if (
                not isinstance(pixel_entry, list)
                or len(pixel_entry) != 3
                or not all(_integer(item) for item in pixel_entry)
            ):
                raise BusinessError(
                    "E_FRAME_DATA_INVALID",
                    "sparse pixel entries must be [row, col, intensity] integers",
                    field="pixels",
                    clip_id=clip_id,
                )
            row, col, intensity = pixel_entry
            if (
                row < 0
                or row >= FRAME_HEIGHT
                or col < 0
                or col >= FRAME_WIDTH
                or not 0 <= intensity <= 255
            ):
                raise BusinessError(
                    "E_MASK_SHAPE_MISMATCH",
                    "sparse pixel is outside the frame or intensity range",
                    field="pixels",
                    clip_id=clip_id,
                )
            pixel_coordinates.append((row, col))
        if sorted(pixel_coordinates) != sorted(coordinates):
            raise BusinessError(
                "E_FRAME_DATA_INVALID",
                "pixels and mask must describe the same sparse support",
                field="pixels",
                clip_id=clip_id,
            )
    return raw_clip


def load_dataset(
    manifest_path: str | Path,
    frames_path: str | Path,
    config: PipelineConfig | None = None,
) -> dict[str, Any]:
    """Load and validate the complete input chain before pipeline work."""

    config = config or PipelineConfig()
    config.validate()
    specs, degradations = validate_manifest(
        _load_jsonl(Path(manifest_path), "E_MANIFEST_INVALID"), config
    )
    raw_records = _load_jsonl(Path(frames_path), "E_FRAME_DATA_INVALID")
    raw_by_clip: dict[str, dict[str, Any]] = {}
    for raw_clip in raw_records:
        if isinstance(raw_clip, dict):
            clip_id = raw_clip.get("clip_id")
        else:
            clip_id = None
        if not isinstance(clip_id, str):
            raise BusinessError(
                "E_FRAME_DATA_INVALID",
                "raw clip record has no valid clip_id",
                field="clip_id",
            )
        if clip_id in raw_by_clip:
            raise BusinessError(
                "E_FRAME_DATA_INVALID",
                "raw frame data contains a duplicate clip id",
                field="clip_id",
                clip_id=clip_id,
            )
        raw_by_clip[clip_id] = raw_clip
    expected_ids = {spec["clip_id"] for spec in specs}
    actual_ids = set(raw_by_clip)
    if actual_ids != expected_ids:
        missing = sorted(expected_ids - actual_ids)
        extras = sorted(actual_ids - expected_ids)
        offending = (missing or extras)[0]
        raise BusinessError(
            "E_FRAME_DATA_INVALID",
            "raw frame data clip set does not match the manifest",
            field="clip_id",
            clip_id=offending,
            detail={"missing": missing, "extra": extras},
        )
    validated = {
        spec["clip_id"]: _validate_frames(raw_by_clip[spec["clip_id"]], spec, config)
        for spec in specs
    }
    return {
        "specs": specs,
        "clip_ids": [spec["clip_id"] for spec in specs],
        "spec_by_clip": {spec["clip_id"]: spec for spec in specs},
        "frames_by_clip": validated,
        "degradations": degradations,
        "manifest_sha256": hashlib.sha256(
            b"".join(
                json.dumps(
                    spec, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
                + b"\n"
                for spec in specs
            )
        ).hexdigest(),
    }
