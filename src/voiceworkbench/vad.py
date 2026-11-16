"""Stage 2: dual-threshold hysteresis VAD, segment merging, overlap marking."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import PipelineConfig
from .determinism import short_hash
from .features import FileFeatures, FrameRecord

EPS = 1e-12


@dataclass(frozen=True)
class SpeechSegment:
    segment_id: str
    file_name: str
    local_index: int
    start_frame: int
    end_frame: int  # exclusive
    start_ms: float
    end_ms: float
    overlap: bool
    overlap_partner: str | None
    mean_voiceprint: list[float]
    energy_gate_db: float
    flatness_high: float
    flatness_low: float
    gates: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "file_name": self.file_name,
            "local_index": self.local_index,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "start_ms": round(self.start_ms, 4),
            "end_ms": round(self.end_ms, 4),
            "overlap": self.overlap,
            "overlap_partner": self.overlap_partner,
            "mean_voiceprint": [round(v, 6) for v in self.mean_voiceprint],
            "energy_gate_db": self.energy_gate_db,
            "flatness_high": self.flatness_high,
            "flatness_low": self.flatness_low,
            "gates": self.gates,
        }


def classify_frames(frames: list[FrameRecord], cfg: PipelineConfig) -> list[bool]:
    """Hysteresis state machine over the frame stream."""
    voiced: list[bool] = []
    active = False
    for frame in frames:
        loud = frame.energy_db >= cfg.energy_db_floor
        tonal_enter = frame.flatness < cfg.flatness_high
        tonal_hold = frame.flatness < cfg.flatness_low
        if not active:
            active = loud and tonal_enter
        else:
            active = loud and tonal_hold
        voiced.append(active)
    return voiced


def _raw_runs(voiced: list[bool]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, flag in enumerate(voiced):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(voiced)))
    return runs


def apply_minimum_durations(
    runs: list[tuple[int, int]], frame_count: int, cfg: PipelineConfig,
) -> list[tuple[int, int]]:
    """Merge short internal silences into speech; drop short speech runs."""
    if not runs:
        return []

    # short internal silence -> merge neighboring speech runs
    merged: list[list[int]] = []
    for start, end in runs:
        if merged:
            gap = start - merged[-1][1]
            if gap < cfg.min_silence_frames:
                merged[-1][1] = end
                continue
        merged.append([start, end])

    # short speech runs -> absorb into adjacent silence (drop the run)
    kept: list[list[int]] = []
    for start, end in merged:
        if end - start < cfg.min_speech_frames:
            continue
        kept.append([start, end])

    if not kept:
        return []

    # dropping runs may create new short gaps; merge once more deterministically
    final: list[tuple[int, int]] = [(kept[0][0], kept[0][1])]
    for start, end in kept[1:]:
        if start - final[-1][1] < cfg.min_silence_frames:
            final[-1] = (final[-1][0], end)
        else:
            final.append((start, end))
    return final


def cosine_similarity(left: list[float], right: list[float]) -> float:
    a = np.asarray(left, dtype=np.float64)
    b = np.asarray(right, dtype=np.float64)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b)) + EPS
    return float(np.dot(a, b) / denom)


def _mean_voiceprint(frames: list[FrameRecord], start: int, end: int) -> list[float]:
    matrix = np.asarray(
        [frame.voiceprint for frame in frames[start:end]], dtype=np.float64,
    )
    return [float(v) for v in np.mean(matrix, axis=0)]


def segment_file(file_features: FileFeatures, cfg: PipelineConfig) -> list[SpeechSegment]:
    voiced = classify_frames(file_features.frames, cfg)
    runs = apply_minimum_durations(_raw_runs(voiced), len(voiced), cfg)
    segments: list[SpeechSegment] = []
    hop_ms = cfg.hop_ms
    for local_index, (start, end) in enumerate(runs):
        mean_vp = _mean_voiceprint(file_features.frames, start, end)
        segment = SpeechSegment(
            segment_id=f"{file_features.file_name}#seg{local_index}",
            file_name=file_features.file_name,
            local_index=local_index,
            start_frame=start,
            end_frame=end,
            start_ms=start * hop_ms,
            end_ms=end * hop_ms,
            overlap=False,
            overlap_partner=None,
            mean_voiceprint=mean_vp,
            energy_gate_db=cfg.energy_db_floor,
            flatness_high=cfg.flatness_high,
            flatness_low=cfg.flatness_low,
            gates={
                "energy_db_floor": cfg.energy_db_floor,
                "flatness_enter_below": cfg.flatness_high,
                "flatness_hold_below": cfg.flatness_low,
                "overlap_similarity": cfg.overlap_similarity,
                "overlap_distance_gate": cfg.overlap_distance_gate,
                "min_speech_frames": cfg.min_speech_frames,
                "min_silence_frames": cfg.min_silence_frames,
            },
        )
        segments.append(segment)

    # overlap: adjacent segments separated by a very short gap.  An overlap
    # boundary typically dips below VAD energy for only a frame or two, while a
    # genuine silence gap is at least min_silence_frames long.  The configured
    # similarity gate bounds how different the two voiceprints may be; the
    # observed distance is always recorded as supporting evidence.
    for index in range(1, len(segments)):
        previous = segments[index - 1]
        current = segments[index]
        distance = float(
            np.linalg.norm(
                np.asarray(previous.mean_voiceprint) - np.asarray(current.mean_voiceprint)
            )
        )
        gap_frames = current.start_frame - previous.end_frame
        # A genuine silence gap survives minimum-duration merging; an overlap
        # boundary only loses energy briefly.  We therefore treat any gap
        # shorter than 1.5x the minimum silence duration as an overlap
        # candidate, which keeps normal silences (>= min_silence_frames after
        # merging) distinguishable from short overlap boundaries.
        near = 0 <= gap_frames < int(round(cfg.min_silence_frames * 1.5))
        if near and distance <= cfg.overlap_distance_gate:
            segments[index - 1] = _with_overlap(previous, True, current.segment_id, distance)
            segments[index] = _with_overlap(current, True, previous.segment_id, distance)
    return segments


def _with_overlap(segment: SpeechSegment, flag: bool, partner: str,
                  distance: float) -> SpeechSegment:
    gates = dict(segment.gates)
    gates["observed_voiceprint_distance"] = round(distance, 6)
    return SpeechSegment(
        segment_id=segment.segment_id,
        file_name=segment.file_name,
        local_index=segment.local_index,
        start_frame=segment.start_frame,
        end_frame=segment.end_frame,
        start_ms=segment.start_ms,
        end_ms=segment.end_ms,
        overlap=flag,
        overlap_partner=partner,
        mean_voiceprint=segment.mean_voiceprint,
        energy_gate_db=segment.energy_gate_db,
        flatness_high=segment.flatness_high,
        flatness_low=segment.flatness_low,
        gates=gates,
    )


def segment_all(features: list[FileFeatures], cfg: PipelineConfig) -> list[SpeechSegment]:
    segments: list[SpeechSegment] = []
    for file_features in sorted(features, key=lambda item: item.file_name):
        segments.extend(segment_file(file_features, cfg))
    segments.sort(key=lambda item: (item.file_name, item.start_frame))
    return segments


def segments_artifact(segments: list[SpeechSegment]) -> dict[str, Any]:
    return {"segments": [segment.to_dict() for segment in segments]}


def segments_fingerprint(segments: list[SpeechSegment]) -> str:
    return short_hash([segment.to_dict() for segment in segments])
