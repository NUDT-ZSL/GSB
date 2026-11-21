"""Deterministic frame-pair sampling and relative timeline expansion."""

from __future__ import annotations

from typing import Any, Callable, Iterable

from .canonical import r6, sha256_bytes, canonical_bytes
from .codes import E_INPUT_INVALID, E_RESOURCE_LIMIT, E_TIME_ORDER, E_TIME_SPAN
from .errors import BusinessError
from .models import ClipSpec, FrameSample

POLICY_STRIDES: dict[str, tuple[int, ...]] = {
    "adjacent": (1,),
    "stride2": (1, 2),
    "pyramid": (1, 2, 3),
    "adaptive": (1, 2, 4),
}


def expand_timeline(
    spec: ClipSpec,
    frames: Iterable[FrameSample],
    *,
    frame_callback: Callable[[int], None] | None = None,
) -> list[dict[str, Any]]:
    """Validate temporal order/span and return one record per expected frame."""

    frame_list = list(frames)
    if len(frame_list) != spec.frame_count:
        location = min(len(frame_list), spec.frame_count)
        raise BusinessError(
            E_TIME_SPAN,
            f"frame_count={spec.frame_count} but received {len(frame_list)} frames",
            spec.clip_id,
            location,
            {"expected_frame_count": spec.frame_count, "actual_frame_count": len(frame_list)},
        )
    by_index: dict[int, FrameSample] = {}
    for frame in frame_list:
        if frame.frame_index in by_index:
            raise BusinessError(
                E_TIME_ORDER,
                f"duplicate frame_index {frame.frame_index}",
                spec.clip_id,
                frame.frame_index,
            )
        by_index[frame.frame_index] = frame
    expected_indices = list(range(spec.frame_count))
    for expected in expected_indices:
        if expected not in by_index:
            raise BusinessError(E_TIME_SPAN, f"missing frame_index {expected}", spec.clip_id, expected)

    ordered = [by_index[index] for index in expected_indices]
    timeline: list[dict[str, Any]] = []
    previous_timestamp: int | None = None
    for index, frame in enumerate(ordered):
        if frame_callback is not None:
            frame_callback(index)
        if previous_timestamp is not None and frame.timestamp_ms < previous_timestamp:
            raise BusinessError(
                E_TIME_ORDER,
                f"timestamp inversion: {frame.timestamp_ms} < {previous_timestamp}",
                spec.clip_id,
                index,
                {"previous_timestamp_ms": previous_timestamp, "timestamp_ms": frame.timestamp_ms},
            )
        if index > 0:
            actual_delta = frame.timestamp_ms - ordered[index - 1].timestamp_ms
            if actual_delta != spec.frame_interval_ms:
                raise BusinessError(
                    E_TIME_SPAN,
                    f"frame interval {actual_delta}ms != configured {spec.frame_interval_ms}ms",
                    spec.clip_id,
                    index,
                    {
                        "expected_interval_ms": spec.frame_interval_ms,
                        "actual_interval_ms": actual_delta,
                        "previous_frame_index": index - 1,
                    },
                )
        timeline.append(
            {
                "clip_id": spec.clip_id,
                "frame_index": index,
                "timestamp_ms": int(frame.timestamp_ms),
                "relative_ms": int(frame.timestamp_ms - ordered[0].timestamp_ms),
                "relative_steps": index,
                "frame_interval_ms": spec.frame_interval_ms,
                "observed": bool(frame.observed),
                "target_count": len(frame.targets),
                "timestamp_checksum": sha256_bytes(f"{spec.clip_id}:{index}:{frame.timestamp_ms}".encode("utf-8")),
            }
        )
        previous_timestamp = frame.timestamp_ms
    expected_span = (spec.frame_count - 1) * spec.frame_interval_ms
    actual_span = ordered[-1].timestamp_ms - ordered[0].timestamp_ms if ordered else 0
    if spec.frame_count > 1 and actual_span != expected_span:
        raise BusinessError(
            E_RESOURCE_LIMIT,
            f"timestamp span {actual_span}ms != expected {expected_span}ms",
            spec.clip_id,
            spec.frame_count - 1,
            {"expected_span_ms": expected_span, "actual_span_ms": actual_span},
        )
    return timeline


def sample_frame_pairs(
    spec: ClipSpec,
    timeline: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expand policy-specific frame pairs with stable tie-breaking."""

    if spec.temporal_policy not in POLICY_STRIDES:
        raise BusinessError(
            E_INPUT_INVALID,
            f"unsupported temporal_policy {spec.temporal_policy!r}",
            spec.clip_id,
            0,
            {"supported": sorted(POLICY_STRIDES)},
        )
    records = list(timeline)
    if not records:
        return []
    by_index = {int(record["frame_index"]): record for record in records}
    candidates: list[dict[str, Any]] = []
    for destination in sorted(by_index):
        for stride in POLICY_STRIDES[spec.temporal_policy]:
            source = destination - stride
            if source < 0 or source not in by_index:
                continue
            source_record = by_index[source]
            destination_record = by_index[destination]
            delta_ms = int(destination_record["timestamp_ms"]) - int(source_record["timestamp_ms"])
            target_delta = stride * spec.frame_interval_ms
            candidates.append(
                {
                    "clip_id": spec.clip_id,
                    "pair_id": f"{spec.clip_id}:{source}:{destination}",
                    "policy": spec.temporal_policy,
                    "stride": stride,
                    "src_frame_index": source,
                    "dst_frame_index": destination,
                    "src_timestamp_ms": int(source_record["timestamp_ms"]),
                    "dst_timestamp_ms": int(destination_record["timestamp_ms"]),
                    "delta_ms": delta_ms,
                    "delta_seconds": r6(delta_ms / 1000.0),
                    "relative_time_steps": r6(delta_ms / spec.frame_interval_ms),
                    "interval_score": abs(delta_ms - target_delta),
                    "tie_break_frame_index": source,
                }
            )
    candidates.sort(
        key=lambda item: (
            item["dst_frame_index"],
            item["interval_score"],
            item["delta_ms"],
            item["src_frame_index"],
        )
    )
    return candidates


def sampling_payload(
    spec: ClipSpec,
    frames: Iterable[FrameSample],
    *,
    max_sequence_frames: int | None = None,
    frame_callback: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    frame_list = list(frames)
    if max_sequence_frames is not None and len(frame_list) > max_sequence_frames:
        raise BusinessError(
            E_TIME_SPAN,
            f"sequence has {len(frame_list)} frames, resource cap is {max_sequence_frames}",
            spec.clip_id,
            max_sequence_frames,
        )
    timeline = expand_timeline(spec, frame_list, frame_callback=frame_callback)
    pairs = sample_frame_pairs(spec, timeline)
    payload = {
        "clip_id": spec.clip_id,
        "temporal_policy": spec.temporal_policy,
        "frame_interval_ms": spec.frame_interval_ms,
        "frame_indices": [record["frame_index"] for record in timeline],
        "timeline": timeline,
        "frame_pairs": pairs,
    }
    payload["timeline_checksum"] = sha256_bytes(canonical_bytes(timeline))
    payload["frame_pairs_checksum"] = sha256_bytes(canonical_bytes(pairs))
    payload["content_checksum"] = sha256_bytes(canonical_bytes(payload))
    return payload
