from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import Config
from .pitch import midi_to_hz, midi_to_note_name


@dataclass
class _RawSegment:
    start_frame: int
    end_frame: int
    start_s: float
    end_s: float
    start_evidence: str
    end_evidence: str
    boundary_strength: float


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    order = np.argsort(values)
    sorted_values = values[order]
    sorted_weights = np.maximum(weights[order], 1e-9)
    cumulative = np.cumsum(sorted_weights)
    return float(sorted_values[np.searchsorted(cumulative, 0.5 * cumulative[-1])])


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(mask):
        if value and start is None:
            start = index
        elif not value and start is not None:
            runs.append((start, index - 1))
            start = None
    if start is not None:
        runs.append((start, len(mask) - 1))
    return runs


def _split_boundaries(
    start: int,
    end: int,
    midi: np.ndarray,
    confidence: np.ndarray,
    energy: np.ndarray,
    times: np.ndarray,
    config: Config,
) -> list[tuple[int, str, float]]:
    hop = float(np.median(np.diff(times))) if len(times) > 1 else config.hop_length_s
    half_window = max(2, int(round(config.note_boundary_window_s / hop)))
    boundaries: list[tuple[int, str, float]] = []
    length = end - start + 1
    if length < 2 * half_window + 2:
        return boundaries

    candidates: list[tuple[int, float, str]] = []
    for index in range(start + half_window, end - half_window + 1):
        left_values = midi[index - half_window : index]
        right_values = midi[index : index + half_window]
        left_values = left_values[np.isfinite(left_values)]
        right_values = right_values[np.isfinite(right_values)]
        if len(left_values) < 2 or len(right_values) < 2:
            continue
        left_pitch = float(np.median(left_values))
        right_pitch = float(np.median(right_values))
        pitch_jump = abs(right_pitch - left_pitch)
        left_energy = float(np.median(energy[index - half_window : index]))
        right_energy = float(np.median(energy[index : index + half_window]))
        local_energy = float(np.min(energy[max(start, index - 1) : min(end, index + 2) + 1]))
        energy_dip = 0.5 * (left_energy + right_energy) - local_energy
        left_confidence = float(np.median(confidence[index - half_window : index]))
        right_confidence = float(np.median(confidence[index : index + half_window]))
        confidence_drop = 0.5 * (left_confidence + right_confidence) - float(confidence[index])
        evidence = ""
        strength = 0.0
        if pitch_jump >= config.note_pitch_jump_semitones:
            evidence = "pitch_jump"
            strength = pitch_jump + 0.02 * max(0.0, energy_dip)
            if energy_dip >= config.energy_dip_db:
                evidence = "pitch_jump_and_energy_dip"
        elif pitch_jump >= 0.55 and energy_dip >= config.energy_dip_db:
            evidence = "energy_dip_with_pitch_change"
            strength = 0.55 * pitch_jump + 0.08 * energy_dip
        elif pitch_jump >= 0.55 and confidence_drop >= 0.22:
            evidence = "voicing_drop_with_pitch_change"
            strength = 0.55 * pitch_jump + confidence_drop
        if evidence:
            candidates.append((index, strength, evidence))

    candidates.sort(key=lambda item: item[1], reverse=True)
    selected: list[tuple[int, str, float]] = []
    minimum_separation = max(2, int(round(0.070 / hop)))
    for index, strength, evidence in candidates:
        if all(abs(index - existing[0]) >= minimum_separation for existing in selected):
            selected.append((index, evidence, strength))
    selected.sort(key=lambda item: item[0])
    return selected


def _make_segment(
    start: int,
    end: int,
    times: np.ndarray,
    hop: float,
    start_evidence: str,
    end_evidence: str,
    boundary_strength: float,
) -> _RawSegment:
    start_s = float(times[start] - 0.5 * hop) if start > 0 else float(times[start] - 0.5 * hop)
    end_s = float(times[end] + 0.5 * hop)
    return _RawSegment(
        start_frame=start,
        end_frame=end,
        start_s=max(0.0, start_s),
        end_s=max(start_s, end_s),
        start_evidence=start_evidence,
        end_evidence=end_evidence,
        boundary_strength=boundary_strength,
    )


def _segment_pitch(
    segment: _RawSegment,
    midi: np.ndarray,
    confidence: np.ndarray,
    energy: np.ndarray,
    voiced: np.ndarray,
) -> tuple[float, float, float]:
    indices = np.arange(segment.start_frame, segment.end_frame + 1)
    valid = indices[np.isfinite(midi[indices]) & voiced[indices]]
    if len(valid) == 0:
        return float("nan"), 0.0, 0.0
    if len(valid) >= 5:
        trim = max(1, len(valid) // 6)
        stable = valid[trim:-trim]
    else:
        stable = valid
    values = midi[stable]
    weights = confidence[stable]
    representative = _weighted_median(values, weights)
    spread = float(np.median(np.abs(values - np.median(values))))
    variability = float(np.std(values))
    stability = math.exp(-1.8 * spread) * math.exp(-0.35 * variability)
    mean_confidence = float(np.mean(confidence[valid]))
    note_confidence = float(np.clip(0.72 * mean_confidence + 0.28 * stability, 0.0, 1.0))
    return representative, note_confidence, float(np.mean(energy[indices]))


def segment_notes(frames: list[dict[str, Any]], config: Config) -> list[dict[str, Any]]:
    if not frames:
        return []
    midi = np.asarray(
        [frame["midi"] if frame["midi"] is not None else np.nan for frame in frames],
        dtype=np.float64,
    )
    confidence = np.asarray([frame["voicing_confidence"] for frame in frames], dtype=np.float64)
    energy = np.asarray([frame["energy_dbfs"] for frame in frames], dtype=np.float64)
    voiced = np.asarray([bool(frame["voiced"]) for frame in frames], dtype=bool)
    times = np.asarray([frame["timestamp_s"] for frame in frames], dtype=np.float64)
    hop = float(np.median(np.diff(times))) if len(times) > 1 else config.hop_length_s

    active = voiced & np.isfinite(midi)
    # Bridge only very short dropouts so a note is not fragmented by a single
    # unreliable analysis frame. Longer gaps remain boundaries.
    active_runs = _runs(active)
    bridged = active.copy()
    for (_, previous_end), (next_start, _) in zip(active_runs, active_runs[1:]):
        gap = next_start - previous_end - 1
        if 0 < gap <= max(1, int(round(config.max_note_gap_s / hop))):
            bridged[previous_end + 1 : next_start] = True

    raw_segments: list[_RawSegment] = []
    for run_start, run_end in _runs(bridged):
        run_is_voiced = active[run_start : run_end + 1]
        if not np.any(run_is_voiced):
            continue
        split_points = _split_boundaries(
            run_start, run_end, midi, confidence, energy, times, config
        )
        onset_strength = float(split_points[0][2]) if split_points else 0.0
        boundaries = [(run_start, "voiced_onset", onset_strength)]
        boundaries.extend(split_points)
        for position, (start_index, evidence, strength) in enumerate(boundaries):
            end_index = (
                boundaries[position + 1][0] - 1
                if position + 1 < len(boundaries)
                else run_end
            )
            if position + 1 < len(boundaries):
                end_evidence = boundaries[position + 1][1]
            else:
                end_evidence = "voiced_offset"
            if end_index >= start_index:
                raw_segments.append(
                    _make_segment(
                        start_index,
                        end_index,
                        times,
                        hop,
                        evidence,
                        end_evidence,
                        strength,
                    )
                )

    if not raw_segments:
        return []

    # Merge very short fragments into the adjacent note with the closest pitch.
    segments = raw_segments
    while True:
        if len(segments) <= 1:
            break
        durations = [segment.end_s - segment.start_s for segment in segments]
        short_indices = [
            index for index, duration in enumerate(durations) if duration < config.min_note_duration_s
        ]
        if not short_indices:
            break
        short_index = min(short_indices, key=lambda index: durations[index])
        left = short_index - 1 if short_index > 0 else None
        right = short_index + 1 if short_index + 1 < len(segments) else None
        if left is None and right is None:
            break
        short_pitch, _, _ = _segment_pitch(
            segments[short_index], midi, confidence, energy, voiced
        )

        def merge_cost(neighbor: int) -> float:
            neighbor_pitch, _, _ = _segment_pitch(
                segments[neighbor], midi, confidence, energy, voiced
            )
            if not math.isfinite(short_pitch) or not math.isfinite(neighbor_pitch):
                return float("inf")
            gap = max(
                0.0,
                segments[min(neighbor, short_index)].start_s
                - segments[max(neighbor, short_index)].end_s,
            )
            return abs(short_pitch - neighbor_pitch) + 2.0 * gap

        options = [index for index in (left, right) if index is not None]
        target = min(options, key=merge_cost)
        first, second = sorted((short_index, target))
        merged = _RawSegment(
            start_frame=segments[first].start_frame,
            end_frame=segments[second].end_frame,
            start_s=min(segments[first].start_s, segments[second].start_s),
            end_s=max(segments[first].end_s, segments[second].end_s),
            start_evidence=segments[first].start_evidence,
            end_evidence=segments[second].end_evidence,
            boundary_strength=max(
                segments[first].boundary_strength, segments[second].boundary_strength
            ),
        )
        segments = [
            segment
            for index, segment in enumerate(segments)
            if index not in {first, second}
        ]
        segments.insert(first, merged)

    notes: list[dict[str, Any]] = []
    for index, segment in enumerate(segments):
        representative, note_confidence, mean_energy = _segment_pitch(
            segment, midi, confidence, energy, voiced
        )
        if not math.isfinite(representative):
            continue
        indices = np.arange(segment.start_frame, segment.end_frame + 1)
        voiced_ratio = float(np.mean(voiced[indices])) if len(indices) else 0.0
        note_confidence *= float(np.clip(0.55 + 0.45 * voiced_ratio, 0.0, 1.0))
        note_id = f"n{index + 1:03d}"
        notes.append(
            {
                "id": note_id,
                "index": index,
                "start_s": round(segment.start_s, 6),
                "end_s": round(segment.end_s, 6),
                "duration_s": round(segment.end_s - segment.start_s, 6),
                "midi": round(representative, 4),
                "midi_median": round(representative, 4),
                "note_name": midi_to_note_name(representative, cents=True),
                "f0_hz": round(midi_to_hz(representative), 4),
                "confidence": round(float(np.clip(note_confidence, 0.0, 1.0)), 6),
                "mean_energy_dbfs": round(mean_energy, 4),
                "voiced_ratio": round(voiced_ratio, 6),
                "start_evidence": segment.start_evidence,
                "end_evidence": segment.end_evidence,
                "boundary_strength": round(segment.boundary_strength, 6),
                "frame_start": segment.start_frame,
                "frame_end": segment.end_frame,
                "mapped": False,
                "syllable_index": None,
                "mapping_role": None,
            }
        )
    for index, note in enumerate(notes):
        note["index"] = index
        note["id"] = f"n{index + 1:03d}"
    return notes

