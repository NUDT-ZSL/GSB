"""Explicit deterministic temporal propagation operators.

All arithmetic is integer arithmetic.  Frame boundaries consume and return
serializable ``uint8`` frames, so a frame loaded from a checkpoint is exactly
the same condition used by an uninterrupted in-memory run.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from .hashing import aggregate_hashes, array_content_hash
from .models import ClipTask
from .noise import frame_noise


OPERATOR_VERSION = "timeline-propagation-v1"


@dataclass(frozen=True)
class PropagationResult:
    frame: np.ndarray
    intermediate_state_count: int
    trace_digest: str


def _spatial_pattern(
    shape: tuple[int, ...], frame_index: int, step: int, revision_epoch: int
) -> np.ndarray:
    height, width = shape[0], shape[1]
    yy, xx = np.indices((height, width), dtype=np.int16)
    phase = frame_index * 17 + step * 31 + revision_epoch * 7
    pattern = ((xx * 3 + yy * 5 + phase) % 13) - 6
    if len(shape) == 3:
        return pattern[:, :, None]
    return pattern


def _substep(
    state: np.ndarray,
    *,
    motion_mode: str,
    frame_index: int,
    step: int,
    noise: np.ndarray,
    revision_epoch: int,
) -> np.ndarray:
    if motion_mode == "translate":
        shift_y = ((frame_index + step) % 3) - 1
        shift_x = ((frame_index * 2 + step * 3) % 5) - 2
        moved_x = np.roll(state, shift_x, axis=1)
        moved_y = np.roll(state, shift_y, axis=0)
        blended = (state * 6 + moved_x * 3 + moved_y * 2) // 11
    elif motion_mode == "ripple":
        direction = 1 if (frame_index + step) % 2 == 0 else -1
        right = np.roll(state, direction, axis=1)
        left = np.roll(state, -direction, axis=1)
        down = np.roll(state, direction, axis=0)
        up = np.roll(state, -direction, axis=0)
        blended = (state * 4 + left * 2 + right * 2 + up + down) // 10
    else:  # diffuse, validated by ClipTask
        right = np.roll(state, 1, axis=1)
        left = np.roll(state, -1, axis=1)
        down = np.roll(state, 1, axis=0)
        up = np.roll(state, -1, axis=0)
        blended = (state * 2 + left + right + up + down) // 6

    noise_delta = (noise.astype(np.int16) - 128) // 32
    revision_delta = (int(revision_epoch) % 5) - 2
    pattern = _spatial_pattern(
        state.shape if len(state.shape) == 2 else tuple(state.shape),
        frame_index,
        step,
        revision_epoch,
    )
    next_state = blended + pattern.astype(np.int16) + noise_delta + revision_delta
    return np.clip(next_state, 0, 255).astype(np.int16)


def propagate_frame(
    previous_frame: np.ndarray,
    *,
    frame_index: int,
    propagation_steps: int,
    motion_mode: str,
    noise: np.ndarray,
    revision_epoch: int,
) -> PropagationResult:
    if propagation_steps <= 0:
        raise ValueError("propagation_steps must be positive")
    state = np.ascontiguousarray(previous_frame).astype(np.int16)
    trace: list[str] = []
    for step in range(propagation_steps):
        state = _substep(
            state,
            motion_mode=motion_mode,
            frame_index=frame_index,
            step=step,
            noise=noise,
            revision_epoch=revision_epoch,
        )
        trace.append(array_content_hash(state.astype(np.uint8)))
    output = np.clip(state, 0, 255).astype(np.uint8)
    return PropagationResult(
        frame=output,
        intermediate_state_count=propagation_steps,
        trace_digest=aggregate_hashes(trace),
    )


ParameterResolver = Callable[[int], tuple[int, int, int]]
"""Returns ``(noise_seed, propagation_steps, revision_epoch)`` for a frame."""


def task_parameter_resolver(task: ClipTask) -> ParameterResolver:
    def resolve(_frame_index: int) -> tuple[int, int, int]:
        return task.noise_seed, task.propagation_steps, task.revision

    return resolve


def generate_continuous(
    task: ClipTask,
    *,
    parameter_resolver: ParameterResolver | None = None,
    noise_counter: Callable[[int], None] | None = None,
) -> np.ndarray:
    """Generate every frame with no block boundaries or cache access."""
    resolve = parameter_resolver or task_parameter_resolver(task)
    frames: list[np.ndarray] = []
    previous: np.ndarray | None = None
    for frame_index in range(task.total_frames):
        seed, steps, revision_epoch = resolve(frame_index)
        noise = frame_noise(task.clip_id, seed, frame_index, task.frame_shape)
        if noise_counter is not None:
            noise_counter(frame_index)
        if frame_index == 0:
            frame = noise
        else:
            if previous is None:
                raise RuntimeError("previous frame missing during continuous run")
            result = propagate_frame(
                previous,
                frame_index=frame_index,
                propagation_steps=steps,
                motion_mode=task.motion_mode,
                noise=noise,
                revision_epoch=revision_epoch,
            )
            frame = result.frame
        frames.append(frame)
        previous = frame
    return np.stack(frames, axis=0)
