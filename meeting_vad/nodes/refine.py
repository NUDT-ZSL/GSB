"""Node 3: bounded energy-slope boundary refinement."""

from __future__ import annotations

from typing import Any

import numpy as np

from ..config import AppConfig
from ..errors import fail
from ..util import sample_to_ms
from .vad import validate_interval_bounds


NODE_VERSION = "refine-v1"


def _assert_no_swallow(
    candidate_id: str,
    new_start: int,
    new_end: int,
    own_start: int,
    own_end: int,
    candidates: list[dict[str, Any]],
) -> None:
    for other in candidates:
        other_start = int(other["start_sample"])
        other_end = int(other["end_sample"])
        if other_start == own_start and other_end == own_end:
            continue
        if new_start <= other_start and new_end >= other_end:
            fail(
                "E_REFINE_INVALID",
                "refined interval would swallow a neighboring interval",
                interval_id=candidate_id,
                affected_interval_id=other["candidate_id"],
                allowed_start_min=other_start,
                allowed_end_max=other_end,
                attempted_start_sample=new_start,
                attempted_end_sample=new_end,
            )


def compute_refine(
    envelope_payload: dict[str, Any],
    vad_payload: dict[str, Any],
    config: AppConfig,
) -> dict[str, Any]:
    window_samples = max(
        0, int(round(config.refine.window_ms * config.sample_rate / 1000.0))
    )
    clips_payload: dict[str, Any] = {}
    for clip_id in vad_payload["clip_order"]:
        clip_vad = vad_payload["clips"][clip_id]
        clip_env = envelope_payload["clips"][clip_id]
        candidates = list(clip_vad["intervals"])
        starts = np.asarray(clip_env["frame_start_samples"], dtype=np.int64)
        ends = np.asarray(clip_env["frame_end_samples"], dtype=np.int64)
        energy = np.asarray(clip_env["energy"], dtype=np.float64)
        total_samples = int(clip_env["total_samples"])
        refined_intervals: list[dict[str, Any]] = []
        previous_refined_end = 0

        for index, candidate in enumerate(candidates):
            candidate_id = candidate["candidate_id"]
            original_start = int(candidate["start_sample"])
            original_end = int(candidate["end_sample"])
            next_start = (
                int(candidates[index + 1]["start_sample"])
                if index + 1 < len(candidates)
                else total_samples
            )

            start_low = max(0, original_start - window_samples)
            start_high = min(original_end - 1, original_start + window_samples)
            start_indices = [
                frame_index
                for frame_index, frame_start in enumerate(starts.tolist())
                if start_low <= frame_start <= start_high
            ]
            start_choice = original_start
            if start_indices:
                slopes = []
                for frame_index in start_indices:
                    previous_energy = (
                        float(energy[frame_index - 1]) if frame_index > 0 else 0.0
                    )
                    slope = float(energy[frame_index]) - previous_energy
                    slopes.append((slope, frame_index))
                slope, frame_index = max(slopes, key=lambda item: (item[0], -item[1]))
                if slope > config.refine.min_slope:
                    start_choice = int(starts[frame_index])

            end_low = max(original_start + 1, original_end - window_samples)
            end_high = min(total_samples, original_end + window_samples)
            end_indices = [
                frame_index
                for frame_index, frame_end in enumerate(ends.tolist())
                if end_low <= frame_end <= end_high
            ]
            end_choice = original_end
            if end_indices:
                slopes = []
                for frame_index in end_indices:
                    previous_energy = (
                        float(energy[frame_index - 1]) if frame_index > 0 else 0.0
                    )
                    slope = float(energy[frame_index]) - previous_energy
                    slopes.append((slope, frame_index))
                slope, frame_index = min(slopes, key=lambda item: (item[0], item[1]))
                if slope < -config.refine.min_slope:
                    end_choice = int(ends[frame_index])

            if abs(start_choice - original_start) > window_samples:
                fail(
                    "E_REFINE_INVALID",
                    "refinement start exceeds the configured search window",
                    interval_id=candidate_id,
                    allowed_start_min=original_start - window_samples,
                    allowed_start_max=original_start + window_samples,
                    attempted_start_sample=start_choice,
                )
            if abs(end_choice - original_end) > window_samples:
                fail(
                    "E_REFINE_INVALID",
                    "refinement end exceeds the configured search window",
                    interval_id=candidate_id,
                    allowed_end_min=original_end - window_samples,
                    allowed_end_max=original_end + window_samples,
                    attempted_end_sample=end_choice,
                )
            if start_choice >= end_choice:
                fail(
                    "E_REFINE_INVALID",
                    "refined interval has non-positive duration",
                    interval_id=candidate_id,
                    attempted_start_sample=start_choice,
                    attempted_end_sample=end_choice,
                )
            if start_choice < previous_refined_end or end_choice > next_start:
                fail(
                    "E_REFINE_INVALID",
                    "refined interval crosses an allowed neighbor boundary",
                    interval_id=candidate_id,
                    allowed_start_min=previous_refined_end,
                    allowed_end_max=next_start,
                    attempted_start_sample=start_choice,
                    attempted_end_sample=end_choice,
                )
            _assert_no_swallow(
                candidate_id,
                start_choice,
                end_choice,
                original_start,
                original_end,
                candidates,
            )
            refined_intervals.append(
                {
                    "candidate_id": candidate_id,
                    "clip_id": clip_id,
                    "start_sample": start_choice,
                    "end_sample": end_choice,
                    "start_ms": sample_to_ms(start_choice, config.sample_rate),
                    "end_ms": sample_to_ms(end_choice, config.sample_rate),
                    "candidate_start_sample": original_start,
                    "candidate_end_sample": original_end,
                    "start_delta_ms": sample_to_ms(
                        start_choice - original_start, config.sample_rate
                    ),
                    "end_delta_ms": sample_to_ms(
                        end_choice - original_end, config.sample_rate
                    ),
                }
            )
            previous_refined_end = end_choice

        validate_interval_bounds(
            refined_intervals, float(clip_env["duration_ms"]), "candidate_id"
        )
        clips_payload[clip_id] = {
            "clip_id": clip_id,
            "duration_ms": clip_env["duration_ms"],
            "search_window_samples": window_samples,
            "intervals": refined_intervals,
        }
    return {
        "schema": NODE_VERSION,
        "clip_order": vad_payload["clip_order"],
        "refine": {
            "window_ms": config.refine.window_ms,
            "min_slope": config.refine.min_slope,
        },
        "clips": clips_payload,
    }


def refine_key_details(
    vad_payload: dict[str, Any],
    upstream_hash: str,
    config: AppConfig,
) -> dict[str, Any]:
    return {
        "upstream_hash": upstream_hash,
        "candidate_ids": {
            clip_id: [
                interval["candidate_id"]
                for interval in vad_payload["clips"][clip_id]["intervals"]
            ]
            for clip_id in vad_payload["clip_order"]
        },
        "threshold_version": config.vad.threshold_version,
        "threshold_params": {
            "noise_quantile": config.vad.noise_quantile,
            "low_multiplier": config.vad.low_multiplier,
            "high_multiplier": config.vad.high_multiplier,
            "min_silence_ms": config.vad.min_silence_ms,
        },
        "refine": {
            "window_ms": config.refine.window_ms,
            "min_slope": config.refine.min_slope,
        },
    }
