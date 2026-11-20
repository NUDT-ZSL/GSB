"""Event interval post-processing and exact timeline validation."""

from __future__ import annotations

import math
from typing import Iterable, Sequence

import numpy as np

from .catalog import ACTIVE_EVENT_TYPES, event_type_id
from .codes import BusinessCode
from .config import FeatureConfig
from .detection import validate_candidates
from .errors import AedError
from .paths import PathProfile
from .types import Candidate, TimelineSegment


def _ceil_frames(ms: float, sample_rate: int, hop_samples: int) -> int:
    return int(math.ceil((float(ms) / 1000.0) * sample_rate / hop_samples - 1e-12))


def _floor_frames(ms: float, sample_rate: int, hop_samples: int) -> int:
    return int(math.floor((float(ms) / 1000.0) * sample_rate / hop_samples + 1e-12))


def _sample_to_ms(sample: int, sample_rate: int) -> float:
    return sample * 1000.0 / sample_rate


def _merge_same_type(
    candidates: Sequence[Candidate],
    *,
    min_gap_frames: int,
    frame_count: int,
) -> list[Candidate]:
    merged: list[Candidate] = []
    for event_type in ACTIVE_EVENT_TYPES:
        typed = sorted(
            (candidate for candidate in candidates if candidate.event_type == event_type),
            key=lambda candidate: (candidate.start_frame, candidate.end_frame, -candidate.confidence),
        )
        if not typed:
            continue
        current_start = typed[0].start_frame
        current_end = typed[0].end_frame
        current_confidences = [typed[0].confidence]
        for candidate in typed[1:]:
            gap = candidate.start_frame - current_end - 1
            if gap <= min_gap_frames:
                current_confidences.append(candidate.confidence)
                current_end = max(current_end, min(frame_count - 1, candidate.end_frame))
            else:
                merged.append(
                    Candidate(
                        start_frame=current_start,
                        end_frame=current_end,
                        event_type=event_type,
                        confidence=float(np.mean(current_confidences)),
                    )
                )
                current_start = candidate.start_frame
                current_end = candidate.end_frame
                current_confidences = [candidate.confidence]
        merged.append(
            Candidate(
                start_frame=current_start,
                end_frame=current_end,
                event_type=event_type,
                confidence=float(np.mean(current_confidences)),
            )
        )
    return merged


def _resolve_to_frame_labels(
    candidates: Sequence[Candidate],
    frame_count: int,
) -> tuple[list[str], np.ndarray]:
    labels = ["background"] * frame_count
    confidences = np.zeros(frame_count, dtype=np.float64)
    priority = sorted(
        candidates,
        key=lambda candidate: (
            -round(candidate.confidence, 12),
            event_type_id(candidate.event_type),
            candidate.start_frame,
            candidate.end_frame,
        ),
    )
    for candidate in priority:
        for frame_index in range(candidate.start_frame, candidate.end_frame + 1):
            current_type = labels[frame_index]
            if current_type == "background":
                labels[frame_index] = candidate.event_type
                confidences[frame_index] = candidate.confidence
                continue
            current_key = (
                -round(float(confidences[frame_index]), 12),
                event_type_id(current_type),
            )
            candidate_key = (
                -round(candidate.confidence, 12),
                event_type_id(candidate.event_type),
            )
            if candidate_key < current_key:
                labels[frame_index] = candidate.event_type
                confidences[frame_index] = candidate.confidence
    return labels, confidences


def _labels_to_segments(
    labels: Sequence[str],
    confidences: Sequence[float],
    *,
    frame_count: int,
    duration_samples: int,
    config: FeatureConfig,
) -> list[TimelineSegment]:
    segments: list[TimelineSegment] = []
    start = 0
    for index in range(1, frame_count + 1):
        if index < frame_count and labels[index] == labels[start]:
            continue
        end = index - 1
        start_sample = start * config.hop_samples
        end_sample = min(duration_samples, (end + 1) * config.hop_samples)
        segment_values = [float(value) for value in confidences[start : end + 1]]
        segments.append(
            TimelineSegment(
                start_frame=start,
                end_frame=end,
                start_ms=_sample_to_ms(start_sample, config.sample_rate),
                end_ms=_sample_to_ms(end_sample, config.sample_rate),
                event_type=labels[start],
                confidence=float(np.mean(segment_values)) if segment_values else 0.0,
                is_event=labels[start] != "background",
                segment_id=f"seg_{len(segments):04d}",
            )
        )
        start = index
    return segments


def _suppress_short_runs(
    labels: list[str],
    confidences: np.ndarray,
    *,
    min_event_frames: int,
) -> None:
    """Apply the minimum-duration rule to final connected event runs."""

    start = 0
    while start < len(labels):
        end = start
        while end + 1 < len(labels) and labels[end + 1] == labels[start]:
            end += 1
        if labels[start] != "background" and end - start + 1 < min_event_frames:
            for frame_index in range(start, end + 1):
                labels[frame_index] = "background"
                confidences[frame_index] = 0.0
        start = end + 1


def postprocess_candidates(
    candidates: Sequence[Candidate],
    *,
    frame_count: int,
    duration_samples: int,
    config: FeatureConfig,
    profile: PathProfile,
    min_event_ms: float,
    min_gap_ms: float,
    dialog_id: str,
) -> list[TimelineSegment]:
    """Merge candidates and return a complete, contiguous frame timeline."""

    if frame_count <= 0:
        raise AedError(
            BusinessCode.E_EVENT_RANGE,
            "frame_count must be positive",
            dialog_id=dialog_id,
            field="frame_count",
            details={"frame_count": frame_count},
        )
    if duration_samples <= 0:
        raise AedError(
            BusinessCode.E_TIMELINE_INVALID,
            "duration must be positive",
            dialog_id=dialog_id,
            field="duration_samples",
        )
    validate_candidates(candidates, frame_count, dialog_id=dialog_id)
    min_event_frames = max(1, _ceil_frames(min_event_ms, config.sample_rate, config.hop_samples))
    min_gap_frames = max(0, _floor_frames(min_gap_ms, config.sample_rate, config.hop_samples))

    prepared: list[Candidate] = []
    stride = profile.analysis_stride_frames
    for candidate in candidates:
        if profile.postprocess_mode == "threshold" and candidate.confidence < profile.detection_threshold:
            continue
        start = candidate.start_frame
        end = candidate.end_frame
        if profile.postprocess_mode == "threshold":
            start = (start // stride) * stride
            end = min(frame_count - 1, ((end + 1 + stride - 1) // stride) * stride - 1)
        prepared.append(
            Candidate(
                start_frame=start,
                end_frame=end,
                event_type=candidate.event_type,
                confidence=candidate.confidence,
            )
        )

    merged = _merge_same_type(
        prepared,
        min_gap_frames=min_gap_frames,
        frame_count=frame_count,
    )
    merged = [
        candidate
        for candidate in merged
        if candidate.end_frame - candidate.start_frame + 1 >= min_event_frames
    ]
    labels, confidences = _resolve_to_frame_labels(merged, frame_count)
    _suppress_short_runs(
        labels,
        confidences,
        min_event_frames=min_event_frames,
    )
    segments = _labels_to_segments(
        labels,
        confidences,
        frame_count=frame_count,
        duration_samples=duration_samples,
        config=config,
    )
    validate_timeline(segments, frame_count=frame_count, duration_ms=_sample_to_ms(duration_samples, config.sample_rate), dialog_id=dialog_id)
    return segments


def validate_timeline(
    segments: Sequence[TimelineSegment],
    *,
    frame_count: int,
    duration_ms: float,
    dialog_id: str,
) -> None:
    """Validate a full contiguous timeline and its event intervals."""

    if not segments:
        raise AedError(
            BusinessCode.E_INCONSISTENT,
            "timeline has no segments",
            dialog_id=dialog_id,
            field="segments",
            details={"category": "timeline_coverage"},
        )
    expected_start = 0
    previous_end_ms: float | None = None
    previous_frame_end: int | None = None
    for segment in segments:
        interval = {
            "start_frame": segment.start_frame,
            "end_frame": segment.end_frame,
            "start_ms": round(segment.start_ms, 6),
            "end_ms": round(segment.end_ms, 6),
        }
        if (
            segment.start_frame != expected_start
            or segment.end_frame < segment.start_frame
            or segment.end_frame >= frame_count
        ):
            raise AedError(
                BusinessCode.E_OUT_OF_RANGE,
                "timeline segment is not in the canonical frame range",
                dialog_id=dialog_id,
                field="event_range",
                details={"interval": interval, "expected_start_frame": expected_start},
            )
        if (
            segment.start_ms < 0
            or segment.end_ms > duration_ms + 1e-6
            or segment.end_ms <= segment.start_ms
        ):
            raise AedError(
                BusinessCode.E_OUT_OF_RANGE,
                "timeline segment is outside the audio duration",
                dialog_id=dialog_id,
                field="event_time",
                details={"interval": interval, "duration_ms": duration_ms},
            )
        if previous_end_ms is not None and segment.start_ms < previous_end_ms - 1e-6:
            raise AedError(
                BusinessCode.E_OVERLAP,
                "timeline segments overlap",
                dialog_id=dialog_id,
                field="event_time",
                details={"previous_end_ms": previous_end_ms, "interval": interval},
            )
        expected_start = segment.end_frame + 1
        previous_end_ms = segment.end_ms
        previous_frame_end = segment.end_frame

    if previous_frame_end != frame_count - 1:
        raise AedError(
            BusinessCode.E_INCONSISTENT,
            "timeline does not cover every detected frame",
            dialog_id=dialog_id,
            field="segments",
            details={
                "category": "timeline_coverage",
                "covered_until_frame": previous_frame_end,
                "frame_count": frame_count,
            },
        )
    event_segments = [segment for segment in segments if segment.is_event]
    validate_event_intervals(event_segments, duration_ms=duration_ms, dialog_id=dialog_id)


def validate_event_intervals(
    events: Iterable[TimelineSegment],
    *,
    duration_ms: float,
    dialog_id: str,
) -> None:
    ordered = sorted(events, key=lambda segment: (segment.start_ms, segment.end_ms))
    previous: TimelineSegment | None = None
    for segment in ordered:
        if (
            segment.start_ms < 0
            or segment.end_ms > duration_ms + 1e-6
            or segment.end_ms <= segment.start_ms
        ):
            raise AedError(
                BusinessCode.E_OUT_OF_RANGE,
                "event interval is outside the audio duration",
                dialog_id=dialog_id,
                field="event_time",
                details={
                    "interval": {
                        "start_ms": round(segment.start_ms, 6),
                        "end_ms": round(segment.end_ms, 6),
                    },
                    "duration_ms": duration_ms,
                },
            )
        if previous is not None and segment.start_ms < previous.end_ms - 1e-6:
            raise AedError(
                BusinessCode.E_OVERLAP,
                "event intervals overlap",
                dialog_id=dialog_id,
                field="event_time",
                details={
                    "first": {
                        "start_ms": round(previous.start_ms, 6),
                        "end_ms": round(previous.end_ms, 6),
                    },
                    "second": {
                        "start_ms": round(segment.start_ms, 6),
                        "end_ms": round(segment.end_ms, 6),
                    },
                },
            )
        previous = segment
