from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .math3d import (
    IDENTITY_QUAT,
    ONE_VEC,
    Quat,
    Transform,
    Vec3,
    compose,
    lerp,
    lerp_vec3,
    quat,
    quat_normalize,
    quat_slerp,
    vec3,
)
from .models import (
    CHANNELS,
    Clip,
    Condition,
    MachineState,
    Parameter,
    ReplayFrame,
    Spec,
    Track,
    TrackSample,
    Transition,
)


def sample_parameter(parameter: Parameter, t: int) -> Any:
    if not parameter.track:
        return parameter.default
    times = [item[0] for item in parameter.track]
    if t < times[0]:
        return parameter.default
    index = bisect.bisect_right(times, t) - 1
    if index >= len(times) - 1:
        return parameter.track[-1][1]
    left_t, left_value = parameter.track[index]
    right_t, right_value = parameter.track[index + 1]
    if isinstance(left_value, float) and isinstance(right_value, float):
        alpha = (t - left_t) / (right_t - left_t)
        return lerp(left_value, right_value, alpha)
    return left_value


def compare_parameter(value: Any, op: str, threshold: Any) -> bool:
    if op == "<":
        return float(value) < float(threshold)
    if op == "<=":
        return float(value) <= float(threshold)
    if op == ">":
        return float(value) > float(threshold)
    if op == ">=":
        return float(value) >= float(threshold)
    if op == "==":
        return value == threshold
    if op == "!=":
        return value != threshold
    return False


def conditions_match(
    transition: Transition,
    parameter_values: Mapping[str, Any],
) -> bool:
    for condition in transition.when:
        if not compare_parameter(
            parameter_values[condition.param],
            condition.op,
            condition.value,
        ):
            return False
    return True


def sample_track(track: Track, local_ms: int) -> tuple[tuple[float, ...], int, int]:
    keys = track.keyframes
    times = [key.t for key in keys]
    if local_ms <= times[0]:
        return keys[0].value, times[0], times[0]
    if local_ms >= times[-1]:
        return keys[-1].value, times[-1], times[-1]
    index = bisect.bisect_right(times, local_ms) - 1
    left = keys[index]
    right = keys[index + 1]
    if local_ms == left.t:
        return left.value, left.t, left.t
    alpha = (local_ms - left.t) / (right.t - left.t)
    if track.channel == "rotation":
        value = quat_slerp(quat(left.value), quat(right.value), alpha)
    else:
        value = lerp_vec3(vec3(left.value), vec3(right.value), alpha)
    return tuple(value), left.t, right.t


@dataclass(frozen=True)
class ClipPose:
    world: Mapping[str, Transform]
    track_samples: tuple[TrackSample, ...]


@dataclass(frozen=True)
class ClipPoseSet:
    clip_id: str
    duration_ms: int
    step_ms: int
    poses: Mapping[int, ClipPose]
    final_pose: ClipPose
    estimated_bytes: int


def _local_pose_at(
    spec: Spec,
    clip: Clip,
    local_ms: int,
) -> tuple[dict[str, Transform], tuple[TrackSample, ...]]:
    local: dict[str, Transform] = {}
    samples: list[TrackSample] = []
    for bone in spec.skeleton:
        transform = bone.rest
        bone_tracks = clip.tracks.get(bone.name, {})
        translation = transform[0]
        rotation = transform[1]
        scale = transform[2]
        changed = False
        for channel in CHANNELS:
            track = bone_tracks.get(channel)
            if track is None or not track.keyframes:
                continue
            value, lo_ms, hi_ms = sample_track(track, local_ms)
            samples.append(
                TrackSample(
                    bone=bone.name,
                    channel=channel,
                    lo_ms=lo_ms,
                    hi_ms=hi_ms,
                )
            )
            changed = True
            if channel == "translation":
                translation = vec3(value)
            elif channel == "rotation":
                rotation = quat_normalize(quat(value))
            else:
                scale = vec3(value)
        local[bone.name] = (
            translation if changed else transform[0],
            rotation if changed else transform[1],
            scale if changed else transform[2],
        )
    return local, tuple(samples)


def evaluate_world_at(spec: Spec, clip: Clip, local_ms: int) -> ClipPose:
    local, samples = _local_pose_at(spec, clip, local_ms)
    by_name = {bone.name: bone for bone in spec.skeleton}
    world: dict[str, Transform] = {}
    for name in spec.bone_order:
        bone = by_name[name]
        if bone.parent is None:
            world[name] = local[name]
        else:
            world[name] = compose(world[bone.parent], local[name])
    return ClipPose(world=world, track_samples=samples)


def evaluate_clip_offsets(
    spec: Spec,
    clip_id: str,
    offsets: Iterable[int],
    *,
    include_final: bool = True,
) -> ClipPoseSet:
    clip = spec.clips[clip_id]
    poses: dict[int, ClipPose] = {}
    for offset in offsets:
        poses[int(offset)] = evaluate_world_at(spec, clip, int(offset))
    final_pose = (
        evaluate_world_at(spec, clip, clip.duration_ms)
        if include_final
        else ClipPose(world={}, track_samples=())
    )
    return ClipPoseSet(
        clip_id=clip_id,
        duration_ms=clip.duration_ms,
        step_ms=spec.timeline.frame_step_ms,
        poses=poses,
        final_pose=final_pose,
        estimated_bytes=0,
    )


def clip_timeline_position(
    clip: Clip,
    state: MachineState,
    t: int,
) -> tuple[int, bool]:
    elapsed = t - state.enter_ms
    if elapsed < 0:
        # A state boundary can only be at or before the current logical time.
        return 0, False
    if state.target_loop_count > 0:
        total = state.target_loop_count * clip.duration_ms
        if elapsed >= total:
            return clip.duration_ms, True
    return elapsed % clip.duration_ms, False


def pose_for_state(
    spec: Spec,
    cache: Mapping[str, ClipPoseSet],
    state: MachineState,
    t: int,
) -> tuple[ClipPose, int, bool]:
    clip = spec.clips[state.active_clip]
    local_ms, finished = clip_timeline_position(clip, state, t)
    clip_cache = cache[state.active_clip]
    if finished:
        pose = clip_cache.final_pose
    else:
        pose = clip_cache.poses.get(local_ms)
        if pose is None:
            pose = evaluate_world_at(spec, clip, local_ms)
    return pose, local_ms, finished


def frame_from_pose(
    t: int,
    state: MachineState,
    local_ms: int,
    pose: ClipPose,
) -> ReplayFrame:
    return ReplayFrame(
        t=t,
        clip=state.active_clip,
        local_ms=local_ms,
        generation=state.generation,
        world=pose.world,
        track_samples=pose.track_samples,
    )


def clip_local_offsets(clip: Clip, step_ms: int) -> tuple[int, ...]:
    return tuple(range(0, clip.duration_ms, step_ms))

