"""Deterministic hysteresis VAD and speaker separation masks."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import MeetingConfig, canonical_json_bytes
from .features import FeatureResult
from .timebase import SegmentSpan, build_span


@dataclass
class SeparationResult:
    candidates: list[dict[str, Any]]
    masks: np.ndarray
    segments: list[SegmentSpan]
    mask_bytes: bytes
    mask_sha256: str
    segment_bytes: bytes
    segment_sha256: str
    thresholds: dict[str, float]


def _candidate_ranges(energies: np.ndarray, multiplier: float) -> tuple[list[tuple[int, int]], dict[str, float]]:
    floor = float(np.percentile(energies, 10))
    peak = float(np.percentile(energies, 95))
    spread = max(peak - floor, 1e-12)
    high_ratio = min(0.55, 0.10 * multiplier)
    low_ratio = min(high_ratio * 0.45, 0.045 * multiplier)
    high = floor + high_ratio * spread
    low = floor + low_ratio * spread
    ranges: list[tuple[int, int]] = []
    active = False
    start = 0
    for frame_index, energy in enumerate(energies):
        if not active and energy >= high:
            active = True
            start = frame_index
        elif active and energy < low:
            ranges.append((start, frame_index))
            active = False
    if active:
        ranges.append((start, int(energies.size)))
    return ranges, {"floor": floor, "high": high, "low": low, "peak": peak}


def _best_speaker(scores: np.ndarray) -> int:
    best_index = 0
    best_score = float(scores[0])
    for index in range(1, int(scores.size)):
        score = float(scores[index])
        if score > best_score + 1e-12:
            best_index = index
            best_score = score
    return best_index + 1


def separate_speakers(
    config: MeetingConfig,
    features: FeatureResult,
    profiles: dict[int, dict[str, Any]],
) -> SeparationResult:
    energies = np.asarray([frame["energy"] for frame in features.frames], dtype=np.float64)
    ranges, thresholds = _candidate_ranges(energies, config.threshold_multiplier)
    frame_count = len(features.frames)
    profile_ids = sorted(profiles)
    declared = list(range(1, config.max_speakers + 1))
    if any(speaker_id not in declared for speaker_id in profile_ids):
        missing = sorted(set(profile_ids) - set(declared))
        raise ValueError(f"profile speakers {missing} exceed configured max_speakers")
    means = np.vstack([profiles[speaker_id]["mean"] for speaker_id in profile_ids])
    masks = np.zeros((frame_count, config.max_speakers), dtype=np.float64)
    top_scores = np.zeros(frame_count, dtype=np.float64)
    candidate_mask = np.zeros(frame_count, dtype=bool)
    for start, end in ranges:
        candidate_mask[start:end] = True
    for frame_index in np.flatnonzero(candidate_mask):
        frame_index = int(frame_index)
        similarity = means @ features.embeddings[frame_index]
        best_id = _best_speaker(similarity)
        best_score = float(similarity[best_id - 1])
        top_scores[frame_index] = best_score
        if config.mask_mode == "weighted":
            weight = max(0.05, (best_score - config.reid_threshold) / max(1e-9, 1.0 - config.reid_threshold))
            masks[frame_index, best_id - 1] = min(1.0, weight)
        else:
            masks[frame_index, best_id - 1] = 1.0

    candidates: list[dict[str, Any]] = []
    for index, (start, end) in enumerate(ranges, start=1):
        candidates.append(
            {
                "candidate_id": f"VAD-{index:03d}",
                "start_frame": start,
                "end_frame": end,
            }
        )

    preliminary: list[SegmentSpan] = []
    for speaker_index, speaker_id in enumerate(profile_ids, start=1):
        active_frames = np.flatnonzero(masks[:, speaker_index - 1] > 0.0)
        if active_frames.size == 0:
            continue
        run_start = int(active_frames[0])
        previous = run_start
        runs: list[tuple[int, int]] = []
        for value in active_frames[1:]:
            current = int(value)
            if current != previous + 1:
                runs.append((run_start, previous + 1))
                run_start = current
            previous = current
        runs.append((run_start, previous + 1))
        for run_number, (start_frame, end_frame) in enumerate(runs, start=1):
            score = float(np.mean(top_scores[start_frame:end_frame]))
            preliminary.append(
                build_span(
                    segment_id=f"PRE-{speaker_id:02d}-{run_number:03d}",
                    speaker_id=speaker_id,
                    start_frame=start_frame,
                    end_frame=end_frame,
                    sample_rate=config.sample_rate,
                    frame_samples=config.frame_samples,
                    hop_samples=config.hop_samples,
                    mask_score=score,
                )
            )
    preliminary.sort(key=lambda item: (item.speaker_id, item.start_sample, item.end_sample))
    segments = [
        build_span(
            segment_id=f"SEG-{index:04d}",
            speaker_id=item.speaker_id,
            start_frame=item.start_frame,
            end_frame=item.end_frame,
            sample_rate=config.sample_rate,
            frame_samples=config.frame_samples,
            hop_samples=config.hop_samples,
            mask_score=item.mask_score,
        )
        for index, item in enumerate(preliminary, start=1)
    ]
    mask_payload = {
        "sample_rate": config.sample_rate,
        "frame_ms": config.frame_ms,
        "hop_ms": config.hop_ms,
        "speaker_ids": declared,
        "mask_mode": config.mask_mode,
        "frames": [[float(value) for value in row] for row in masks],
    }
    segment_payload = [
        {
            "segment_id": item.segment_id,
            "speaker_id": item.speaker_id,
            "start_frame": item.start_frame,
            "end_frame": item.end_frame,
            "start_sample": item.start_sample,
            "end_sample": item.end_sample,
            "mask_score": item.mask_score,
        }
        for item in segments
    ]
    mask_bytes = canonical_json_bytes(mask_payload)
    segment_bytes = canonical_json_bytes(segment_payload)
    return SeparationResult(
        candidates=candidates,
        masks=masks,
        segments=segments,
        mask_bytes=mask_bytes,
        mask_sha256=hashlib.sha256(mask_bytes).hexdigest(),
        segment_bytes=segment_bytes,
        segment_sha256=hashlib.sha256(segment_bytes).hexdigest(),
        thresholds=thresholds,
    )
