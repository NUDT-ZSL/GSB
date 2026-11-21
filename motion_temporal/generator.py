"""Deterministic local video samples with no external data dependencies."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path
from typing import Iterable

import numpy as np

from .canonical import atomic_write_bytes, canonical_bytes, checksum, r6, sha256_bytes
from .codes import E_INPUT_INVALID
from .errors import BusinessError
from .models import Box, ClipSpec, FrameSample, GeneratedClip, TargetObservation


def _seed_bits(seed: int, object_id: int, salt: str, modulus: int) -> int:
    if modulus <= 0:
        return 0
    token = f"{seed}:{object_id}:{salt}".encode("utf-8")
    return int(sha256_bytes(token)[:12], 16) % modulus


def _box_from_center(cx: float, cy: float, width: float, height: float) -> Box:
    return (
        r6(cx - width / 2.0),
        r6(cy - height / 2.0),
        r6(cx + width / 2.0),
        r6(cy + height / 2.0),
    )


def _target_geometry(spec: ClipSpec, object_id: int, timestamp_ms: int) -> tuple[Box, tuple[float, float], tuple[float, float]]:
    height = float(spec.canvas_height)
    width = float(spec.canvas_width)
    object_width = float(10 + 2 * (object_id % 3) + _seed_bits(spec.seed, object_id, "w", 3))
    object_height = float(8 + 2 * ((object_id + 1) % 3) + _seed_bits(spec.seed, object_id, "h", 2))
    lane_y = height * (object_id + 1) / (spec.object_count + 1)
    seconds = timestamp_ms / 1000.0

    if spec.motion_mode == "uniform":
        start_x = 14.0 + 19.0 * object_id + _seed_bits(spec.seed, object_id, "x", 8)
        vx = 9.0 + _seed_bits(spec.seed, object_id, "vx", 9)
        vy = (_seed_bits(spec.seed, object_id, "vy", 7) - 3) * 0.45
        cx = start_x + vx * seconds
        cy = lane_y + vy * seconds
        velocity = (vx, vy)
        acceleration = (0.0, 0.0)
    elif spec.motion_mode == "accelerated":
        start_x = 12.0 + 15.0 * object_id + _seed_bits(spec.seed, object_id, "ax0", 7)
        vx0 = 5.0 + _seed_bits(spec.seed, object_id, "av", 5)
        ax = 3.0 + _seed_bits(spec.seed, object_id, "aa", 4)
        ay = (_seed_bits(spec.seed, object_id, "ay", 5) - 2) * 0.7
        cx = start_x + vx0 * seconds + 0.5 * ax * seconds * seconds
        cy = lane_y + 0.5 * ay * seconds * seconds
        velocity = (vx0 + ax * seconds, ay * seconds)
        acceleration = (ax, ay)
    elif spec.motion_mode == "periodic":
        period = 1.1 + 0.15 * _seed_bits(spec.seed, object_id, "p", 5)
        phase = 2.0 * math.pi * _seed_bits(spec.seed, object_id, "phase", 360) / 360.0
        amplitude_x = 16.0 + 2.5 * _seed_bits(spec.seed, object_id, "ampx", 5)
        amplitude_y = 2.0 + 0.7 * _seed_bits(spec.seed, object_id, "ampy", 3)
        omega = 2.0 * math.pi / period
        cx = width * 0.5 + amplitude_x * math.sin(omega * seconds + phase)
        cy = lane_y + amplitude_y * math.sin(omega * 0.63 * seconds + phase * 0.5)
        velocity = (
            amplitude_x * omega * math.cos(omega * seconds + phase),
            amplitude_y * omega * 0.63 * math.cos(omega * 0.63 * seconds + phase * 0.5),
        )
        acceleration = (
            -amplitude_x * omega * omega * math.sin(omega * seconds + phase),
            -amplitude_y * (omega * 0.63) ** 2 * math.sin(omega * 0.63 * seconds + phase * 0.5),
        )
    else:
        raise BusinessError(
            E_INPUT_INVALID,
            f"unsupported motion_mode {spec.motion_mode!r}",
            spec.clip_id,
            0,
        )

    margin_x = object_width / 2.0 + 1.0
    margin_y = object_height / 2.0 + 1.0
    cx = min(max(cx, margin_x), width - margin_x)
    cy = min(max(cy, margin_y), height - margin_y)
    return _box_from_center(cx, cy, object_width, object_height), velocity, acceleration


def _sparse_mask(
    spec: ClipSpec,
    object_id: int,
    box: Box,
    frame_index: int,
) -> tuple[tuple[int, int], ...]:
    x0 = max(0, int(math.floor(box[0])))
    y0 = max(0, int(math.floor(box[1])))
    x1 = min(spec.canvas_width - 1, int(math.ceil(box[2])))
    y1 = min(spec.canvas_height - 1, int(math.ceil(box[3])))
    stride_seed = int(sha256_bytes(f"{spec.seed}:{object_id}:{frame_index}:mask".encode("utf-8"))[:8], 16)
    points: list[tuple[int, int]] = []
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            if (x * 3 + y * 5 + object_id * 7 + stride_seed) % 7 == 0:
                points.append((y, x))
    if not points:
        points.append((min(max(y0, 0), spec.canvas_height - 1), min(max(x0, 0), spec.canvas_width - 1)))
    return tuple(points)


def _render_image(spec: ClipSpec, targets: Iterable[TargetObservation]) -> np.ndarray:
    image = np.zeros((spec.canvas_height, spec.canvas_width), dtype=np.uint8)
    for target in targets:
        if target.occluded:
            continue
        base = 20 + (target.object_id * 13) % 90
        x0 = max(0, int(math.floor(target.box[0])))
        y0 = max(0, int(math.floor(target.box[1])))
        x1 = min(spec.canvas_width - 1, int(math.ceil(target.box[2])))
        y1 = min(spec.canvas_height - 1, int(math.ceil(target.box[3])))
        image[y0 : y1 + 1, x0 : x1 + 1] = np.maximum(image[y0 : y1 + 1, x0 : x1 + 1], base)
        for y, x in target.mask_coords:
            if 0 <= y < spec.canvas_height and 0 <= x < spec.canvas_width:
                image[y, x] = min(255, base + 100)
    return image


def generate_clip(spec: ClipSpec, missing_frames: Iterable[int] = ()) -> GeneratedClip:
    """Generate one clip only from the spec and its fixed seed."""

    if spec.frame_count < 1 or spec.frame_interval_ms < 1 or spec.object_count < 1:
        raise BusinessError(E_INPUT_INVALID, "frame_count, frame_interval_ms and object_count must be positive", spec.clip_id, 0)
    missing = {int(index) for index in missing_frames}
    frames: list[FrameSample] = []
    for frame_index in range(spec.frame_count):
        timestamp_ms = frame_index * spec.frame_interval_ms
        if frame_index in missing:
            frames.append(
                FrameSample(
                    clip_id=spec.clip_id,
                    frame_index=frame_index,
                    timestamp_ms=timestamp_ms,
                    observed=False,
                    targets=(),
                    image=np.zeros((spec.canvas_height, spec.canvas_width), dtype=np.uint8),
                )
            )
            continue
        targets: list[TargetObservation] = []
        for object_id in range(spec.object_count):
            box, _, _ = _target_geometry(spec, object_id, timestamp_ms)
            occluded = (frame_index + object_id * 5) % 23 == 11
            coords = () if occluded else _sparse_mask(spec, object_id, box, frame_index)
            targets.append(
                TargetObservation(
                    object_id=object_id,
                    box=box,
                    occluded=occluded,
                    mask_shape=(spec.canvas_height, spec.canvas_width),
                    mask_coords=coords,
                )
            )
        frames.append(
            FrameSample(
                clip_id=spec.clip_id,
                frame_index=frame_index,
                timestamp_ms=timestamp_ms,
                observed=True,
                targets=tuple(targets),
                image=_render_image(spec, targets),
            )
        )
    return GeneratedClip(spec=spec, frames=frames)


def default_specs(frame_count: int = 18, frame_interval_ms: int = 100, seed: int = 20261121) -> list[ClipSpec]:
    return [
        ClipSpec(
            clip_id="clip_uniform",
            frame_count=frame_count,
            frame_interval_ms=frame_interval_ms,
            object_count=3,
            motion_mode="uniform",
            seed=seed,
            temporal_policy="adjacent",
            model_revision="rev-1",
        ),
        ClipSpec(
            clip_id="clip_accelerated",
            frame_count=frame_count,
            frame_interval_ms=frame_interval_ms,
            object_count=3,
            motion_mode="accelerated",
            seed=seed + 1,
            temporal_policy="stride2",
            model_revision="rev-1",
        ),
        ClipSpec(
            clip_id="clip_periodic",
            frame_count=frame_count,
            frame_interval_ms=frame_interval_ms,
            object_count=2,
            motion_mode="periodic",
            seed=seed + 2,
            temporal_policy="pyramid",
            model_revision="rev-1",
        ),
    ]


def truth_bytes(clip: GeneratedClip) -> bytes:
    return b"\n".join(canonical_bytes(frame.truth_dict()) for frame in clip.frames) + b"\n"


def generate_manifest(
    output_dir: str | Path,
    specs: Iterable[ClipSpec] | None = None,
    missing_frames: Iterable[int] = (),
) -> dict[str, object]:
    """Write manifest.jsonl plus per-clip truth/mask JSONL and return a summary."""

    root = Path(output_dir)
    truth_dir = root / "truth"
    truth_dir.mkdir(parents=True, exist_ok=True)
    selected_specs = list(specs if specs is not None else default_specs())
    manifest_records: list[dict[str, object]] = []
    clip_summaries: list[dict[str, object]] = []
    for spec in sorted(selected_specs, key=lambda item: item.clip_id):
        clip = generate_clip(spec, missing_frames=missing_frames)
        data = truth_bytes(clip)
        truth_path = truth_dir / f"{spec.clip_id}.jsonl"
        atomic_write_bytes(truth_path, data)
        relative_truth = truth_path.relative_to(root).as_posix()
        manifest = spec.to_manifest_dict(relative_truth, sha256_bytes(data))
        manifest_records.append(manifest)
        clip_summaries.append(
            {
                "clip_id": spec.clip_id,
                "frame_count": spec.frame_count,
                "truth_path": relative_truth,
                "truth_checksum": sha256_bytes(data),
                "frame_truth_checksum": checksum([frame.truth_dict() for frame in clip.frames]),
            }
        )
    manifest_data = b"\n".join(canonical_bytes(record) for record in manifest_records) + b"\n"
    manifest_path = root / "manifest.jsonl"
    atomic_write_bytes(manifest_path, manifest_data)
    summary = {
        "manifest_path": str(manifest_path),
        "manifest_checksum": sha256_bytes(manifest_data),
        "clips": clip_summaries,
    }
    atomic_write_bytes(root / "sample_summary.json", canonical_bytes(summary) + b"\n")
    return summary


def load_manifest(manifest_path: str | Path) -> tuple[list[ClipSpec], list[GeneratedClip]]:
    """Load local manifest and truth data, rebuilding pixels from sparse masks."""

    path = Path(manifest_path)
    if not path.exists():
        raise BusinessError(E_INPUT_INVALID, f"manifest not found: {path}")
    specs: list[ClipSpec] = []
    clips: list[GeneratedClip] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            spec = ClipSpec.from_dict(record)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BusinessError(E_INPUT_INVALID, f"invalid manifest line {line_number}: {exc}") from exc
        truth_path = path.parent / str(record.get("truth_path", f"truth/{spec.clip_id}.jsonl"))
        if not truth_path.exists():
            raise BusinessError(E_INPUT_INVALID, f"truth file not found: {truth_path}", spec.clip_id)
        raw_truth = truth_path.read_bytes()
        expected_checksum = record.get("truth_checksum")
        if expected_checksum is not None and sha256_bytes(raw_truth) != expected_checksum:
            raise BusinessError(
                E_INPUT_INVALID,
                "truth checksum mismatch",
                spec.clip_id,
                details={"expected": expected_checksum, "actual": sha256_bytes(raw_truth)},
            )
        frames: list[FrameSample] = []
        for frame_line in raw_truth.decode("utf-8").splitlines():
            if not frame_line.strip():
                continue
            truth = json.loads(frame_line)
            image = np.zeros((spec.canvas_height, spec.canvas_width), dtype=np.uint8)
            frame = FrameSample.from_truth_dict(truth, image=image)
            image = _render_image(spec, frame.targets)
            frames.append(replace(frame, image=image))
        specs.append(spec)
        clips.append(GeneratedClip(spec=spec, frames=frames))
    if not specs:
        raise BusinessError(E_INPUT_INVALID, "manifest contains no clips")
    return specs, clips

