from __future__ import annotations

from typing import Any, Mapping, Sequence

from .errors import ReplayError, SchemaError
from .math3d import is_finite_number
from .models import CHANNELS, ReplayFrame, ReplayResult


def flat_frame(
    frame: ReplayFrame,
    *,
    sequence: int | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "t": frame.t,
        "clip": frame.clip,
        "state_generation": frame.generation,
        "bones": frame.bones_flat(),
    }
    if sequence is not None:
        value["sequence"] = sequence
    return value


def make_client_cache(
    result: ReplayResult,
    *,
    frame_times: Sequence[int] | None = None,
    applied_event_count: int | None = None,
) -> dict[str, Any]:
    wanted = None if frame_times is None else set(frame_times)
    frames = [
        flat_frame(frame, sequence=index)
        for index, frame in enumerate(result.frames)
        if wanted is None or frame.t in wanted
    ]
    event_count = len(result.events) if applied_event_count is None else applied_event_count
    return {
        "format": "anim-replay-client-cache-v1",
        "version_id": result.version_id,
        "frames": frames,
        "applied_event_count": event_count,
    }


def _client_frame_map(cache: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    if cache.get("format") != "anim-replay-client-cache-v1":
        raise SchemaError("unsupported client cache format", location="client.frames")
    raw_frames = cache.get("frames")
    if not isinstance(raw_frames, list):
        raise SchemaError("client frames must be an array", location="client.frames")
    result: dict[int, dict[str, Any]] = {}
    for index, frame in enumerate(raw_frames):
        location = f"client.frames[{index}]"
        if not isinstance(frame, dict):
            raise SchemaError("client frame must be an object", location=location)
        t = frame.get("t")
        if not isinstance(t, int) or isinstance(t, bool) or t < 0:
            raise SchemaError("client frame time must be a non-negative integer", location=location)
        if t in result:
            raise SchemaError("client frame times must be unique", location=location)
        bones = frame.get("bones")
        if not isinstance(bones, dict):
            raise SchemaError("client frame bones must be an object", location=location)
        for key, value in bones.items():
            if not isinstance(key, str) or "." not in key:
                raise SchemaError("client bone channel key is malformed", location=location)
            if not isinstance(value, list) or not all(is_finite_number(item) for item in value):
                raise SchemaError("client bone channel value is not finite", location=location)
        result[t] = frame
    return result


def _frame_for_compare(
    *,
    t: int,
    clip: str,
    generation: int,
    bones: Mapping[str, Any],
) -> dict[str, Any]:
    return {"t": t, "clip": clip, "state_generation": generation, "bones": bones}


def _first_pose_difference(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    *,
    tolerance: float,
) -> dict[str, Any] | None:
    if expected.get("clip") != actual.get("clip") or expected.get("state_generation") != actual.get(
        "state_generation"
    ):
        return {
            "logical_ms": expected.get("t", actual.get("t")),
            "clip": expected.get("clip", actual.get("clip")),
            "bone_channel": "*",
            "reason": "clip_or_state_generation_mismatch",
            "expected": {
                "clip": expected.get("clip"),
                "state_generation": expected.get("state_generation"),
            },
            "actual": {
                "clip": actual.get("clip"),
                "state_generation": actual.get("state_generation"),
            },
        }
    expected_bones = expected.get("bones", {})
    actual_bones = actual.get("bones", {})
    for key in sorted(set(expected_bones) | set(actual_bones)):
        if key not in actual_bones or key not in expected_bones:
            return {
                "logical_ms": expected.get("t", actual.get("t")),
                "clip": expected.get("clip", actual.get("clip")),
                "bone_channel": key,
                "reason": "missing_bone_channel",
            }
        left = expected_bones[key]
        right = actual_bones[key]
        if len(left) != len(right):
            return {
                "logical_ms": expected.get("t", actual.get("t")),
                "clip": expected.get("clip", actual.get("clip")),
                "bone_channel": key,
                "reason": "component_count_mismatch",
            }
        for component, (a, b) in enumerate(zip(left, right)):
            if abs(float(a) - float(b)) > tolerance:
                return {
                    "logical_ms": expected.get("t", actual.get("t")),
                    "clip": expected.get("clip"),
                    "bone_channel": key,
                    "component": component,
                    "reason": "pose_tolerance",
                    "expected": float(a),
                    "actual": float(b),
                    "abs_diff": abs(float(a) - float(b)),
                }
    return None


def reconnect_catchup(
    authoritative: ReplayResult,
    client_cache: Mapping[str, Any],
    *,
    tolerance: float = 1e-6,
    max_operations_in_report: int = 100,
) -> dict[str, Any]:
    client_version = client_cache.get("version_id")
    if client_version is not None and client_version != authoritative.version_id:
        raise ReplayError(
            "client cache was produced from a different animation version",
            details={
                "first_mismatch": {
                    "logical_ms": 0,
                    "clip": authoritative.spec.initial_clip,
                    "bone_channel": "*",
                    "reason": "version_mismatch",
                },
                "client_version": client_version,
                "authoritative_version": authoritative.version_id,
            },
        )
    client_frames = _client_frame_map(client_cache)
    authoritative_frames = {frame.t: flat_frame(frame) for frame in authoritative.frames}

    for t in sorted(client_frames):
        if t not in authoritative_frames:
            mismatch = {
                "logical_ms": t,
                "clip": client_frames[t].get("clip"),
                "bone_channel": "*",
                "reason": "frame_not_present_authoritatively",
            }
            raise ReplayError(
                "client cache contains an unexpected logical time",
                details={"first_mismatch": mismatch},
            )

    first_mismatch: dict[str, Any] | None = None
    for t in sorted(set(client_frames) & set(authoritative_frames)):
        difference = _first_pose_difference(
            authoritative_frames[t],
            client_frames[t],
            tolerance=tolerance,
        )
        if difference is not None:
            first_mismatch = difference
            break

    applied_event_count = client_cache.get("applied_event_count", 0)
    if (
        not isinstance(applied_event_count, int)
        or isinstance(applied_event_count, bool)
        or applied_event_count < 0
        or applied_event_count > len(authoritative.events)
    ):
        raise ReplayError(
            "client applied event count is out of range",
            details={
                "first_mismatch": {
                    "logical_ms": 0,
                    "clip": authoritative.spec.initial_clip,
                    "bone_channel": None,
                    "reason": "invalid_event_count",
                }
            },
        )
    for index in range(applied_event_count):
        expected = authoritative.events[index]
        client_events = client_cache.get("events")
        if client_events is None:
            continue
        if not isinstance(client_events, list) or index >= len(client_events):
            raise ReplayError(
                "client event history is shorter than applied_event_count",
                details={
                    "first_mismatch": {
                        "logical_ms": expected.t,
                        "clip": expected.source_clip,
                        "bone_channel": None,
                        "reason": "missing_client_event",
                    }
                },
            )
        if client_events[index] != expected.to_dict():
            raise ReplayError(
                "client event history diverges from authoritative event stream",
                details={
                    "first_mismatch": {
                        "logical_ms": expected.t,
                        "clip": expected.source_clip,
                        "bone_channel": None,
                        "reason": "event_mismatch",
                        "expected": expected.to_dict(),
                        "actual": client_events[index],
                    }
                },
            )

    if first_mismatch is not None:
        raise ReplayError(
            "client cache diverges from authoritative replay",
            details={"first_mismatch": first_mismatch},
        )

    missing = sorted(set(authoritative_frames) - set(client_frames))
    operations: list[dict[str, Any]] = [
        {
            "op": "fill_pose",
            "logical_ms": t,
            "frame": authoritative_frames[t],
        }
        for t in missing
    ]
    for index in range(applied_event_count, len(authoritative.events)):
        operations.append(
            {
                "op": "replay_event",
                "sequence": index,
                "event": authoritative.events[index].to_dict(),
            }
        )
    report_operations = operations[:max_operations_in_report]
    return {
        "ok": True,
        "version_id": authoritative.version_id,
        "tolerance": tolerance,
        "missing_pose_count": len(missing),
        "missing_pose_times": missing,
        "event_replay_count": len(authoritative.events) - applied_event_count,
        "operations": report_operations,
        "operations_truncated": len(operations) > len(report_operations),
        "operation_count": len(operations),
        "order_contract": "fill_pose_ascending_then_replay_event_ascending_sequence",
    }

