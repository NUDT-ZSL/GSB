"""Clip task parsing, shape constraints, keyframe indices and interpolation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .errors import E_CLIP_SHAPE, KFError
from .hashing import hash_json


REQUIRED_FIELDS = (
    "clip_id",
    "total_frames",
    "keyframe_interval",
    "frame_shape",
    "noise_seed",
    "prompt",
    "diffusion_steps",
    "block_grid",
    "revision",
)


@dataclass
class ClipTask:
    clip_id: str
    total_frames: int
    keyframe_interval: int
    frame_shape: Tuple[int, int, int]
    noise_seed: int
    prompt: str
    diffusion_steps: int
    block_grid: int
    revision: int = 1
    phase_salts: Dict[int, str] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def height(self) -> int:
        return int(self.frame_shape[0])

    @property
    def width(self) -> int:
        return int(self.frame_shape[1])

    @property
    def channels(self) -> int:
        return int(self.frame_shape[2])

    @property
    def pixels_per_frame(self) -> int:
        return self.height * self.width * self.channels

    def keyframe_indices(self) -> List[int]:
        """Frame indices carrying keyframes: 0, I, 2I, ..., total_frames-1."""
        return list(range(0, self.total_frames, self.keyframe_interval))

    @property
    def keyframe_count(self) -> int:
        return len(self.keyframe_indices())

    def spec_hash(self) -> str:
        return hash_json(
            {
                "clip_id": self.clip_id,
                "total_frames": self.total_frames,
                "keyframe_interval": self.keyframe_interval,
                "frame_shape": list(self.frame_shape),
                "noise_seed": self.noise_seed,
                "prompt": self.prompt,
                "diffusion_steps": self.diffusion_steps,
                "block_grid": self.block_grid,
                "revision": self.revision,
                "phase_salts": {str(k): v for k, v in sorted(self.phase_salts.items())},
            }
        )

    def to_jsonl_dict(self) -> Dict[str, Any]:
        data = {
            "clip_id": self.clip_id,
            "total_frames": self.total_frames,
            "keyframe_interval": self.keyframe_interval,
            "frame_shape": list(self.frame_shape),
            "noise_seed": self.noise_seed,
            "prompt": self.prompt,
            "diffusion_steps": self.diffusion_steps,
            "block_grid": self.block_grid,
            "revision": self.revision,
        }
        if self.phase_salts:
            data["phase_salts"] = {str(k): v for k, v in sorted(self.phase_salts.items())}
        return data


def parse_task(row: Dict[str, Any]) -> ClipTask:
    """Parse and explicitly validate the keyframe/frame relationship."""
    violations: List[str] = []
    clip_id = str(row.get("clip_id", "<unknown>"))

    for name in REQUIRED_FIELDS:
        if name not in row:
            violations.append(f"missing field {name!r}")

    if violations:
        raise KFError(E_CLIP_SHAPE, f"clip {clip_id!r} has invalid shape specification",
                      {"clip_id": clip_id, "violations": violations})

    total_frames = row["total_frames"]
    interval = row["keyframe_interval"]
    shape = row["frame_shape"]
    steps = row["diffusion_steps"]
    grid = row["block_grid"]
    revision = row.get("revision", 1)

    if not isinstance(total_frames, int) or isinstance(total_frames, bool) or total_frames < 2:
        violations.append("total_frames must be an integer >= 2")
    if not isinstance(interval, int) or isinstance(interval, bool) or interval < 1:
        violations.append("keyframe_interval must be an integer >= 1")
    if not isinstance(shape, Sequence) or isinstance(shape, (str, bytes)) or len(shape) != 3:
        violations.append("frame_shape must be [height, width, channels]")
        shape = None
    else:
        if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in shape):
            violations.append("frame_shape entries must be positive integers")
        elif int(shape[2]) not in (1, 3, 4):
            violations.append("frame_shape channels must be 1, 3 or 4")
    if not isinstance(steps, int) or not (1 <= steps <= 64):
        violations.append("diffusion_steps must be an integer in [1, 64]")
    if not isinstance(grid, int) or isinstance(grid, bool) or grid < 1:
        violations.append("block_grid must be an integer >= 1")
    if not isinstance(revision, int) or revision < 1:
        violations.append("revision must be an integer >= 1")
    if not isinstance(row.get("noise_seed"), int):
        violations.append("noise_seed must be an integer")
    if not isinstance(row.get("prompt"), str) or not row["prompt"].strip():
        violations.append("prompt must be a non-empty string")

    if not violations and isinstance(total_frames, int) and isinstance(interval, int):
        if (total_frames - 1) % interval != 0:
            violations.append(
                "keyframe relationship violated: (total_frames - 1) must be divisible by "
                f"keyframe_interval (total_frames={total_frames}, interval={interval}, "
                f"remainder={(total_frames - 1) % interval})"
            )
        if interval >= total_frames:
            violations.append("keyframe_interval must be < total_frames so interior frames exist")

    if violations:
        raise KFError(
            E_CLIP_SHAPE,
            f"clip {clip_id!r} violates keyframe/frame shape constraints",
            {"clip_id": clip_id, "violations": violations},
        )

    phase_salts_raw = row.get("phase_salts") or {}
    phase_salts = {int(k): str(v) for k, v in phase_salts_raw.items()}

    return ClipTask(
        clip_id=clip_id,
        total_frames=int(total_frames),
        keyframe_interval=int(interval),
        frame_shape=(int(shape[0]), int(shape[1]), int(shape[2])),
        noise_seed=int(row["noise_seed"]),
        prompt=str(row["prompt"]),
        diffusion_steps=int(steps),
        block_grid=int(grid),
        revision=int(revision),
        phase_salts=phase_salts,
    )


def blocks_of_keyframes(keyframe_indices: Sequence[int], block_grid: int) -> List[List[int]]:
    """Split keyframe *ordinal positions* into consecutive blocks of size grid.

    Returns lists of ordinal positions (0..K-1).  The final block may be
    shorter than ``block_grid``.
    """
    if block_grid < 1:
        raise KFError(E_CLIP_SHAPE, "block_grid must be >= 1", {"block_grid": block_grid})
    positions = list(range(len(keyframe_indices)))
    return [positions[i : i + block_grid] for i in range(0, len(positions), block_grid)]


def interpolate_segment(
    left_pixels: np.ndarray,
    right_pixels: np.ndarray,
    left_frame: int,
    right_frame: int,
) -> np.ndarray:
    """Linearly interpolate every frame of ``[left_frame, right_frame]``.

    Endpoints are included.  Quantisation uses deterministic round-to-nearest
    on float32 arithmetic and is the single interpolation implementation used
    by generation and by replay self-checks.
    """
    if right_frame <= left_frame:
        raise ValueError("right_frame must be greater than left_frame")
    span = right_frame - left_frame
    left = left_pixels.astype(np.float32)
    right = right_pixels.astype(np.float32)
    frames = np.empty((span + 1,) + left_pixels.shape, dtype=np.uint8)
    frames[0] = left_pixels.astype(np.uint8)
    frames[-1] = right_pixels.astype(np.uint8)
    for offset in range(1, span):
        weight = np.float32(offset / span)
        blended = left * (np.float32(1.0) - weight) + right * weight
        frames[offset] = np.clip(np.rint(blended), 0, 255).astype(np.uint8)
    return frames


def pixel_summary(pixels: np.ndarray) -> Dict[str, Any]:
    """Content summary for a uint8 keyframe/frame array."""
    arr = np.asarray(pixels, dtype=np.uint8)
    return {
        "shape": list(arr.shape),
        "dtype": "uint8",
        "min": int(arr.min()),
        "max": int(arr.max()),
        "mean": round(float(arr.mean()), 4),
        "std": round(float(arr.std()), 4),
    }


def frame_content_summary(pixels: np.ndarray) -> Dict[str, Any]:
    from .hashing import array_digest, hash_bytes

    arr = np.ascontiguousarray(pixels, dtype=np.uint8)
    summary = pixel_summary(arr)
    summary["content_hash"] = hash_bytes(arr.tobytes())
    summary["array_hash"] = array_digest(arr)
    return summary
