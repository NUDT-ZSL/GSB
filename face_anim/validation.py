"""Independent reference curves and per-sentence synchronization audit."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .catalog import (
    CANONICAL_CHANNELS,
    EMOTION_PROFILES,
    LIP_CHANNELS,
)
from .models import FrameCurves, Sentence, TruthData, VersionData
from .smoothing import (
    frame_index_for_time,
    frame_lowpass,
    resolve_sentence_emotion,
)


@dataclass
class _ReferenceEvent:
    start_us: int
    end_us: int
    targets: dict[str, float]
    phoneme_id: str | None


def _full_targets(targets: dict[str, float]) -> dict[str, float]:
    return {
        "jaw_open": float(targets["jaw_open"]),
        "lip_width": float(targets["lip_width"]),
        "mouth_corner": float(targets["mouth_corner"]),
        "brow_raise": 0.0,
        "brow_inner_up": 0.0,
    }


def _silence_targets() -> dict[str, float]:
    return _full_targets(
        {"jaw_open": 0.0, "lip_width": 0.12, "mouth_corner": 0.0}
    )


def _truth_events(truth: TruthData, duration_us: int) -> list[_ReferenceEvent]:
    phonemes = truth.phonemes()
    events: list[_ReferenceEvent] = []
    cursor = 0
    for phoneme in phonemes:
        if phoneme.start_us > cursor:
            events.append(
                _ReferenceEvent(
                    start_us=cursor,
                    end_us=phoneme.start_us,
                    targets=_silence_targets(),
                    phoneme_id=None,
                )
            )
        events.append(
            _ReferenceEvent(
                start_us=phoneme.start_us,
                end_us=phoneme.end_us,
                targets=_full_targets(phoneme.targets),
                phoneme_id=phoneme.id,
            )
        )
        cursor = max(cursor, phoneme.end_us)
    if cursor < duration_us:
        events.append(
            _ReferenceEvent(
                start_us=cursor,
                end_us=duration_us,
                targets=_silence_targets(),
                phoneme_id=None,
            )
        )
    return events


def build_reference_curves(
    truth: TruthData, fps: float, frame_count: int
) -> FrameCurves:
    """Build an independent cosine-interpolated reference from truth targets."""
    duration_us = truth.duration_us
    frame_times_us = [
        int(round(index * 1_000_000.0 / fps)) for index in range(frame_count)
    ]
    times = np.asarray(frame_times_us, dtype=np.float64)
    matrix = np.zeros((frame_count, len(CANONICAL_CHANNELS)), dtype=np.float64)
    events = _truth_events(truth, max(duration_us, frame_times_us[-1] + 1))
    for event in events:
        mask = (times >= event.start_us) & (times < event.end_us)
        for channel_index, channel in enumerate(CANONICAL_CHANNELS):
            matrix[mask, channel_index] = event.targets[channel]

    for previous, current in zip(events, events[1:]):
        if current.start_us != previous.end_us:
            continue
        previous_duration = max(1, previous.end_us - previous.start_us)
        current_duration = max(1, current.end_us - current.start_us)
        base = 0.18 * min(previous_duration, current_duration)
        left_width = max(1_000, min(int(round(base)), int(0.32 * previous_duration)))
        right_width = max(1_000, min(int(round(base)), int(0.32 * current_duration)))
        zone_start = current.start_us - left_width
        zone_end = current.start_us + right_width
        mask = (times >= zone_start) & (times <= zone_end)
        if not np.any(mask):
            continue
        phase = (times[mask] - zone_start) / max(1, zone_end - zone_start)
        eased = 0.5 - 0.5 * np.cos(math.pi * np.clip(phase, 0.0, 1.0))
        for channel_index, channel in enumerate(CANONICAL_CHANNELS):
            matrix[mask, channel_index] = (
                previous.targets[channel] * (1.0 - eased)
                + current.targets[channel] * eased
            )

    seconds = times / 1_000_000.0
    for sentence in truth.sentences:
        resolved = resolve_sentence_emotion(sentence)
        profile = EMOTION_PROFILES[resolved.label]
        for phoneme_id, level in resolved.selected_anchors:
            phoneme = sentence.phoneme_by_id(phoneme_id)
            if phoneme is None:
                continue
            center = phoneme.center_us / 1_000_000.0
            duration = phoneme.duration_us / 1_000_000.0
            half_width = max(0.10, min(0.42, 0.12 + 1.5 * duration))
            distance = np.abs(seconds - center)
            mask = distance < half_width
            envelope = 0.5 + 0.5 * np.cos(math.pi * distance[mask] / half_width)
            gain = resolved.intensity * level
            for channel in ("brow_raise", "brow_inner_up", "mouth_corner"):
                channel_index = CANONICAL_CHANNELS.index(channel)
                matrix[mask, channel_index] += profile[channel] * gain * envelope
    matrix[:, 0] = np.clip(matrix[:, 0], 0.0, 1.0)
    matrix[:, 1] = np.clip(matrix[:, 1], 0.0, 1.0)
    matrix[:, 2:] = np.clip(matrix[:, 2:], -1.0, 1.0)
    matrix = frame_lowpass(matrix, fps, channels=(0, 1, 2))
    return FrameCurves(
        fps=float(fps),
        frame_count=frame_count,
        frame_times_us=frame_times_us,
        channels=CANONICAL_CHANNELS,
        values=matrix,
    )


def _source_label_index(versions: list[VersionData]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for version in versions:
        for phoneme in version.phonemes():
            result.setdefault(phoneme.id, {})[version.version] = phoneme.source_label
    return result


def _phoneme_basis(
    phoneme: Any,
    sentence: Sentence,
    versions: list[VersionData],
    source_labels: dict[str, dict[str, str]],
    *,
    frame: int,
    frame_time_us: int,
    output_value: float,
    reference_value: float,
) -> dict[str, Any]:
    phonemes = sentence.phonemes
    index = phonemes.index(phoneme)
    previous_id = phonemes[index - 1].id if index > 0 else None
    next_id = phonemes[index + 1].id if index + 1 < len(phonemes) else None
    return {
        "phoneme_id": phoneme.id,
        "canonical": phoneme.canonical,
        "category": phoneme.category,
        "viseme": phoneme.viseme,
        "source_labels": source_labels.get(phoneme.id, {}),
        "start_ms": round(phoneme.start_us / 1000.0, 6),
        "end_ms": round(phoneme.end_us / 1000.0, 6),
        "truth_targets": dict(phoneme.targets),
        "frame": int(frame),
        "frame_time_seconds": round(frame_time_us / 1_000_000.0, 9),
        "output_value": float(output_value),
        "reference_value": float(reference_value),
        "previous_phoneme_id": previous_id,
        "next_phoneme_id": next_id,
    }


def _nearest_phoneme(sentence: Sentence, time_us: int) -> Any:
    containing = [
        phoneme
        for phoneme in sentence.phonemes
        if phoneme.start_us <= time_us < phoneme.end_us
    ]
    if containing:
        return containing[0]
    return min(sentence.phonemes, key=lambda item: abs(item.center_us - time_us))


def _timing_differences_ms(
    versions: list[VersionData], truth: TruthData, sentence_id: str
) -> list[dict[str, Any]]:
    truth_sentence = truth.sentence(sentence_id)
    if truth_sentence is None:
        return []
    truth_map = {item.id: item for item in truth_sentence.phonemes}
    rows = []
    for version in versions:
        sentence = version.sentence(sentence_id)
        if sentence is None:
            continue
        for phoneme in sentence.phonemes:
            truth_phoneme = truth_map.get(phoneme.id)
            if truth_phoneme is None:
                continue
            start_delta = (phoneme.start_us - truth_phoneme.start_us) / 1000.0
            end_delta = (phoneme.end_us - truth_phoneme.end_us) / 1000.0
            if abs(start_delta) > 1e-6 or abs(end_delta) > 1e-6:
                rows.append(
                    {
                        "version": version.version,
                        "phoneme_id": phoneme.id,
                        "start_delta_ms": start_delta,
                        "end_delta_ms": end_delta,
                    }
                )
    return rows


def validate_version(
    version: VersionData,
    curves: FrameCurves,
    truth: TruthData,
    versions: list[VersionData],
    *,
    threshold: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    reference = build_reference_curves(truth, curves.fps, curves.frame_count)
    diff = np.abs(curves.values - reference.values)
    source_labels = _source_label_index(versions)
    sentence_rows: list[dict[str, Any]] = []
    ledger_rows: list[dict[str, Any]] = []
    first_exceeded: dict[str, Any] | None = None
    global_max = float(np.max(diff)) if diff.size else 0.0

    for sentence_index, truth_sentence in enumerate(truth.sentences, start=1):
        start_frame = int(math.floor(truth_sentence.start_us * curves.fps / 1_000_000.0 + 1e-9))
        end_exclusive = int(
            math.ceil(truth_sentence.end_us * curves.fps / 1_000_000.0 - 1e-9)
        )
        start_frame = min(max(0, start_frame), curves.frame_count - 1)
        end_exclusive = min(max(start_frame + 1, end_exclusive), curves.frame_count)
        sentence_diff = diff[start_frame:end_exclusive]
        if sentence_diff.size:
            flat_index = int(np.argmax(sentence_diff))
            local_frame, channel_index = np.unravel_index(
                flat_index, sentence_diff.shape
            )
            max_deviation = float(sentence_diff[local_frame, channel_index])
            max_frame = start_frame + int(local_frame)
            max_channel = CANONICAL_CHANNELS[int(channel_index)]
        else:
            max_deviation = 0.0
            max_frame = start_frame
            max_channel = CANONICAL_CHANNELS[0]
        max_time_us = int(round(max_frame * 1_000_000.0 / curves.fps))
        basis_phoneme = _nearest_phoneme(truth_sentence, max_time_us)
        channel_index = CANONICAL_CHANNELS.index(max_channel)
        basis = _phoneme_basis(
            basis_phoneme,
            truth_sentence,
            versions,
            source_labels,
            frame=max_frame,
            frame_time_us=max_time_us,
            output_value=float(curves.values[max_frame, channel_index]),
            reference_value=float(reference.values[max_frame, channel_index]),
        )
        per_frame = (
            np.max(sentence_diff, axis=1).tolist()
            if sentence_diff.size
            else []
        )

        sync_max = 0.0
        sync_frame = start_frame
        sync_channel = "jaw_open"
        sync_basis = basis
        lip_indices = [CANONICAL_CHANNELS.index(channel) for channel in LIP_CHANNELS]
        for truth_phoneme in truth_sentence.phonemes:
            frame = frame_index_for_time(
                truth_phoneme.center_us, curves.fps, curves.frame_count
            )
            for index in lip_indices:
                value = abs(
                    float(curves.values[frame, index])
                    - float(reference.values[frame, index])
                )
                if value > sync_max:
                    sync_max = value
                    sync_frame = frame
                    sync_channel = CANONICAL_CHANNELS[index]
                    sync_basis = _phoneme_basis(
                        truth_phoneme,
                        truth_sentence,
                        versions,
                        source_labels,
                        frame=frame,
                        frame_time_us=int(round(frame * 1_000_000.0 / curves.fps)),
                        output_value=float(curves.values[frame, index]),
                        reference_value=float(reference.values[frame, index]),
                    )
        timing_differences = _timing_differences_ms(
            versions, truth, truth_sentence.id
        )
        max_timing = max(
            (
                max(abs(row["start_delta_ms"]), abs(row["end_delta_ms"]))
                for row in timing_differences
            ),
            default=0.0,
        )
        exceeded = max(max_deviation, sync_max) > threshold
        sentence_row = {
            "sentence_id": truth_sentence.id,
            "text": truth_sentence.text,
            "start_frame": start_frame,
            "end_frame": end_exclusive - 1,
            "end_frame_exclusive": end_exclusive,
            "frame_count": end_exclusive - start_frame,
            "phoneme_count": len(truth_sentence.phonemes),
            "target_count": sum(
                1
                for phoneme in truth_sentence.phonemes
                if phoneme.viseme != "SIL"
            ),
            "max_deviation": max_deviation,
            "max_deviation_channel": max_channel,
            "max_deviation_frame": max_frame,
            "max_deviation_time_seconds": round(max_frame / curves.fps, 9),
            "max_sync_deviation": sync_max,
            "max_sync_frame": sync_frame,
            "max_sync_channel": sync_channel,
            "max_timing_deviation_ms": max_timing,
            "threshold_exceeded": exceeded,
            "phoneme_basis": basis,
            "sync_phoneme_basis": sync_basis,
            "max_deviation_per_frame": per_frame,
            "timing_differences": timing_differences,
        }
        sentence_rows.append(sentence_row)
        ledger_rows.append(
            {
                "sentence_id": truth_sentence.id,
                "text": truth_sentence.text,
                "start_frame": start_frame,
                "end_frame": end_exclusive - 1,
                "end_frame_exclusive": end_exclusive,
                "frame_count": end_exclusive - start_frame,
                "phoneme_count": len(truth_sentence.phonemes),
                "target_count": sentence_row["target_count"],
                "max_deviation": round(max_deviation, 9),
                "max_deviation_channel": max_channel,
                "max_deviation_frame": max_frame,
                "max_sync_deviation": round(sync_max, 9),
                "max_timing_deviation_ms": round(max_timing, 6),
            }
        )
        if exceeded and first_exceeded is None:
            first_exceeded = {
                **sentence_row,
                "sentence_index": sentence_index,
                "threshold": threshold,
                "metric": (
                    "channel_deviation"
                    if max_deviation >= sync_max
                    else "sync_deviation"
                ),
            }

    validation = {
        "format": "museum-lipsync-sync-report/v1",
        "version": version.version,
        "fps": curves.fps,
        "threshold": threshold,
        "truth_source": truth.source,
        "truth_path": str(truth.path) if truth.path else None,
        "frames_checked": curves.frame_count,
        "sentences_checked": len(sentence_rows),
        "max_channel_deviation": global_max,
        "passed": first_exceeded is None,
        "first_exceeded": first_exceeded,
        "sentences": sentence_rows,
    }
    ledger = {
        "format": "museum-lipsync-sentence-ledger/v1",
        "version": version.version,
        "fps": curves.fps,
        "threshold": threshold,
        "status": "pass" if first_exceeded is None else "threshold_exceeded",
        "sentences": ledger_rows,
    }
    return validation, ledger
