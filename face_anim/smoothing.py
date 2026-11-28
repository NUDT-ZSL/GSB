"""Coarticulation interpolation, emotion envelopes and frame resampling."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .catalog import (
    CANONICAL_CHANNELS,
    CONFLICT_POLICY,
    EMOTION_PROFILES,
    normalize_emotion,
)
from .models import FrameCurves, Sentence, SmoothTrack, VersionData


INTERNAL_SAMPLE_RATE_HZ = 1000
EMOTION_ANCHOR_TOLERANCE_US = 120_000
FRAME_LIP_SIGMA_SECONDS = 0.065


@dataclass
class _SmoothEvent:
    start_us: int
    end_us: int
    targets: dict[str, float]
    phoneme_id: str | None


@dataclass(frozen=True)
class ResolvedEmotion:
    sentence_id: str
    label: str
    intensity: float
    selected_anchors: tuple[tuple[str, float], ...]
    emotion_anchor_id: str | None
    conflict: dict[str, Any] | None


def _full_targets(lip_targets: dict[str, float]) -> dict[str, float]:
    return {
        "jaw_open": float(lip_targets.get("jaw_open", 0.0)),
        "lip_width": float(lip_targets.get("lip_width", 0.12)),
        "mouth_corner": float(lip_targets.get("mouth_corner", 0.0)),
        "brow_raise": 0.0,
        "brow_inner_up": 0.0,
    }


def _silence_targets() -> dict[str, float]:
    return _full_targets(
        {"jaw_open": 0.0, "lip_width": 0.12, "mouth_corner": 0.0}
    )


def _build_events(version: VersionData) -> list[_SmoothEvent]:
    events: list[_SmoothEvent] = []
    phonemes = version.phonemes()
    if not phonemes:
        return events
    cursor = 0
    for phoneme in phonemes:
        if phoneme.start_us > cursor:
            events.append(
                _SmoothEvent(
                    start_us=cursor,
                    end_us=phoneme.start_us,
                    targets=_silence_targets(),
                    phoneme_id=None,
                )
            )
        events.append(
            _SmoothEvent(
                start_us=phoneme.start_us,
                end_us=phoneme.end_us,
                targets=_full_targets(phoneme.targets),
                phoneme_id=phoneme.id,
            )
        )
        cursor = max(cursor, phoneme.end_us)
    if cursor < version.duration_us:
        events.append(
            _SmoothEvent(
                start_us=cursor,
                end_us=version.duration_us,
                targets=_silence_targets(),
                phoneme_id=None,
            )
        )
    return events


def _blend_rows(
    values: dict[str, np.ndarray],
    start_index: int,
    end_index: int,
    left: dict[str, float],
    right: dict[str, float],
    phase: np.ndarray,
) -> None:
    eased = phase * phase * (3.0 - 2.0 * phase)
    for channel in CANONICAL_CHANNELS:
        values[channel][start_index:end_index] = (
            left[channel] * (1.0 - eased) + right[channel] * eased
        )


def build_smooth_track(
    version: VersionData,
    *,
    sample_rate_hz: int = INTERNAL_SAMPLE_RATE_HZ,
) -> SmoothTrack:
    """Build a continuous 1 kHz target track with duration-aware transitions."""
    duration_us = max(1, int(version.duration_us))
    step_us = max(1, int(round(1_000_000 / sample_rate_hz)))
    sample_times = np.arange(0, duration_us + 1, step_us, dtype=np.int64)
    values = {
        channel: np.zeros(sample_times.shape[0], dtype=np.float64)
        for channel in CANONICAL_CHANNELS
    }
    events = _build_events(version)
    if not events:
        return SmoothTrack(
            sample_rate_hz=sample_rate_hz,
            sample_times_us=sample_times.tolist(),
            values={channel: values[channel].tolist() for channel in CANONICAL_CHANNELS},
            duration_us=duration_us,
        )

    for event in events:
        start = int(np.searchsorted(sample_times, event.start_us, side="left"))
        end = int(np.searchsorted(sample_times, event.end_us, side="left"))
        if end <= start:
            continue
        for channel in CANONICAL_CHANNELS:
            values[channel][start:end] = event.targets[channel]

    # Transition half-widths depend on both phoneme durations. Longer sounds
    # retain a visible target plateau; rapid speech still has a nonzero,
    # bounded transition, so boundaries cannot jump.
    for previous, current in zip(events, events[1:]):
        if current.start_us != previous.end_us:
            continue
        previous_duration = max(1, previous.end_us - previous.start_us)
        current_duration = max(1, current.end_us - current.start_us)
        shortest = min(previous_duration, current_duration)
        base = 0.20 * shortest
        left_width_us = max(1_000, min(int(round(base)), int(0.35 * previous_duration)))
        right_width_us = max(1_000, min(int(round(base)), int(0.35 * current_duration)))
        zone_start = max(0, current.start_us - left_width_us)
        zone_end = min(duration_us, current.start_us + right_width_us)
        if zone_end <= zone_start:
            continue
        start_index = int(np.searchsorted(sample_times, zone_start, side="left"))
        end_index = int(np.searchsorted(sample_times, zone_end, side="right"))
        if end_index <= start_index:
            continue
        phase = (
            sample_times[start_index:end_index].astype(np.float64) - float(zone_start)
        ) / float(zone_end - zone_start)
        _blend_rows(
            values,
            start_index,
            end_index,
            previous.targets,
            current.targets,
            phase,
        )

    emotion_rows = _emotion_envelopes(version, sample_times)
    for channel in ("mouth_corner", "brow_raise", "brow_inner_up"):
        values[channel] = np.clip(values[channel] + emotion_rows[channel], -1.0, 1.0)
    values["jaw_open"] = np.clip(values["jaw_open"], 0.0, 1.0)
    values["lip_width"] = np.clip(values["lip_width"], 0.0, 1.0)
    values["mouth_corner"] = np.clip(values["mouth_corner"], -1.0, 1.0)

    return SmoothTrack(
        sample_rate_hz=sample_rate_hz,
        sample_times_us=sample_times.tolist(),
        values={channel: values[channel].tolist() for channel in CANONICAL_CHANNELS},
        duration_us=duration_us,
    )


def _sentence_anchor(
    sentence: Sentence, phoneme_id: str | None
) -> tuple[Any | None, int | None]:
    if phoneme_id is None:
        return None, None
    phoneme = sentence.phoneme_by_id(phoneme_id)
    return phoneme, (phoneme.center_us if phoneme is not None else None)


def resolve_sentence_emotion(sentence: Sentence) -> ResolvedEmotion:
    label = normalize_emotion(sentence.emotion_label)
    intensity = sentence.emotion_intensity
    emotion_anchor, emotion_center = _sentence_anchor(
        sentence, sentence.emotion_anchor_id
    )
    stress_anchors: list[tuple[Any, float]] = []
    for mark in sentence.stress:
        phoneme = sentence.phoneme_by_id(mark.phoneme_id)
        if phoneme is not None:
            stress_anchors.append((phoneme, mark.level))

    selected: list[tuple[str, float]]
    conflict: dict[str, Any] | None = None
    if stress_anchors:
        closest = min(
            stress_anchors,
            key=lambda item: (
                abs(item[0].center_us - emotion_center)
                if emotion_center is not None
                else 0
            ),
        )
        if (
            emotion_center is not None
            and label != "neutral"
            and intensity > 0.0
            and abs(closest[0].center_us - emotion_center) > EMOTION_ANCHOR_TOLERANCE_US
        ):
            conflict = {
                "code": "EMOTION_STRESS_CONFLICT",
                "sentence_id": sentence.id,
                "emotion_label": label,
                "emotion_intensity": intensity,
                "emotion_anchor_phoneme_id": sentence.emotion_anchor_id,
                "emotion_anchor_ms": round(emotion_center / 1000.0, 6),
                "stress_phoneme_id": closest[0].id,
                "stress_anchor_ms": round(closest[0].center_us / 1000.0, 6),
                "difference_ms": round(
                    abs(closest[0].center_us - emotion_center) / 1000.0, 6
                ),
                "policy": CONFLICT_POLICY,
                "selected": "stress",
                "resolution": "emotion envelope follows stress anchor",
            }
        selected = [
            (phoneme.id, max(0.0, min(1.0, level)))
            for phoneme, level in stress_anchors
        ]
    elif sentence.emotion_anchor_id is not None:
        selected = [(sentence.emotion_anchor_id, 1.0)]
    else:
        selected = []

    if intensity <= 0.0 or label == "neutral":
        selected = []
    return ResolvedEmotion(
        sentence_id=sentence.id,
        label=label,
        intensity=intensity,
        selected_anchors=tuple(selected),
        emotion_anchor_id=sentence.emotion_anchor_id,
        conflict=conflict,
    )


def emotion_resolutions(version: VersionData) -> list[ResolvedEmotion]:
    return [resolve_sentence_emotion(sentence) for sentence in version.sentences]


def _emotion_envelopes(
    version: VersionData, sample_times: np.ndarray
) -> dict[str, np.ndarray]:
    output = {
        channel: np.zeros(sample_times.shape[0], dtype=np.float64)
        for channel in CANONICAL_CHANNELS
    }
    time_seconds = sample_times.astype(np.float64) / 1_000_000.0
    for sentence in version.sentences:
        resolved = resolve_sentence_emotion(sentence)
        profile = EMOTION_PROFILES[resolved.label]
        for phoneme_id, stress_level in resolved.selected_anchors:
            phoneme = sentence.phoneme_by_id(phoneme_id)
            if phoneme is None:
                continue
            anchor_seconds = phoneme.center_us / 1_000_000.0
            duration_seconds = phoneme.duration_us / 1_000_000.0
            half_width = max(0.10, min(0.42, 0.12 + 1.5 * duration_seconds))
            distance = np.abs(time_seconds - anchor_seconds)
            mask = distance < half_width
            if not np.any(mask):
                continue
            phase = distance[mask] / half_width
            envelope = 0.5 + 0.5 * np.cos(math.pi * phase)
            gain = resolved.intensity * stress_level
            for channel in ("brow_raise", "brow_inner_up", "mouth_corner"):
                output[channel][mask] += profile[channel] * gain * envelope
    return output


def resample_frames(track: SmoothTrack, fps: float) -> FrameCurves:
    if not math.isfinite(fps) or fps <= 0.0:
        from .errors import fail

        fail("E_SAMPLE_RATE_MISMATCH", "target fps must be positive", fps=fps)
    exact_frames = track.duration_us * fps / 1_000_000.0
    frame_count = int(math.ceil(exact_frames - 1e-9))
    frame_count = max(1, frame_count)
    frame_times_us = [
        int(round(index * 1_000_000.0 / fps)) for index in range(frame_count)
    ]
    source_times = np.asarray(track.sample_times_us, dtype=np.float64)
    frame_times = np.asarray(frame_times_us, dtype=np.float64)
    matrix = np.empty((frame_count, len(CANONICAL_CHANNELS)), dtype=np.float64)
    for channel_index, channel in enumerate(CANONICAL_CHANNELS):
        source_values = np.asarray(track.values[channel], dtype=np.float64)
        matrix[:, channel_index] = np.interp(
            frame_times, source_times, source_values, left=source_values[0], right=source_values[-1]
        )
    matrix[:, 0] = np.clip(matrix[:, 0], 0.0, 1.0)
    matrix[:, 1] = np.clip(matrix[:, 1], 0.0, 1.0)
    matrix[:, 2] = np.clip(matrix[:, 2], -1.0, 1.0)
    matrix = frame_lowpass(matrix, fps, channels=(0, 1, 2))
    return FrameCurves(
        fps=float(fps),
        frame_count=frame_count,
        frame_times_us=frame_times_us,
        channels=CANONICAL_CHANNELS,
        values=matrix,
    )


def frame_lowpass(
    matrix: np.ndarray,
    fps: float,
    *,
    channels: tuple[int, ...] = (0, 1),
    sigma_seconds: float = FRAME_LIP_SIGMA_SECONDS,
) -> np.ndarray:
    """Zero-phase Gaussian smoothing on the final frame grid.

    A 40 ms consonant can fall entirely between two 30 fps samples. The 1 kHz
    target curve is mathematically continuous, but sampling alone would still
    expose a large frame step. This fixed-time low-pass makes the *exported*
    curve smooth regardless of target fps without shifting its peak.
    """
    sigma_frames = max(0.01, float(fps) * sigma_seconds)
    radius = max(1, int(math.ceil(3.0 * sigma_frames)))
    offsets = np.arange(-radius, radius + 1, dtype=np.float64)
    kernel = np.exp(-0.5 * (offsets / sigma_frames) ** 2)
    kernel /= np.sum(kernel)
    output = np.array(matrix, dtype=np.float64, copy=True)
    if output.shape[0] == 1:
        return output
    for channel in channels:
        padded = np.pad(output[:, channel], (radius, radius), mode="edge")
        output[:, channel] = np.convolve(padded, kernel, mode="valid")
    return output


def frame_index_for_time(time_us: float, fps: float, frame_count: int) -> int:
    index = int(round(time_us * fps / 1_000_000.0))
    return min(frame_count - 1, max(0, index))


def interpolate_channel_at(
    track: SmoothTrack, channel: str, time_us: int
) -> float:
    times = np.asarray(track.sample_times_us, dtype=np.float64)
    values = np.asarray(track.values[channel], dtype=np.float64)
    return float(
        np.interp(
            float(time_us),
            times,
            values,
            left=values[0],
            right=values[-1],
        )
    )


def sentence_frame_span(
    sentence: Sentence, fps: float, frame_count: int
) -> tuple[int, int, int]:
    start = int(math.floor(sentence.start_us * fps / 1_000_000.0 + 1e-9))
    end_exclusive = int(math.ceil(sentence.end_us * fps / 1_000_000.0 - 1e-9))
    start = min(max(0, start), frame_count - 1)
    end_exclusive = min(max(start + 1, end_exclusive), frame_count)
    return start, end_exclusive, end_exclusive - start
