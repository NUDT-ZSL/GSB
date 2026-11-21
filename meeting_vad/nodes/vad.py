"""Node 2: quantile noise floor and hysteretic activity detection."""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np

from ..config import AppConfig
from ..errors import fail
from ..util import sample_to_ms


NODE_VERSION = "vad-v1"


def validate_interval_bounds(
    intervals: Iterable[dict[str, Any]],
    duration_ms: float,
    id_field: str,
) -> None:
    ordered = sorted(
        intervals,
        key=lambda item: (
            float(item["start_ms"]),
            float(item["end_ms"]),
            str(item.get(id_field, "")),
        ),
    )
    previous_end = 0.0
    for interval in ordered:
        identifier = interval.get(id_field) or interval.get("segment_id")
        start = float(interval["start_ms"])
        end = float(interval["end_ms"])
        if start < 0.0 or end > duration_ms or end <= start:
            fail(
                "E_OUT_OF_RANGE",
                "interval lies outside the audio duration",
                interval_id=identifier,
                start_ms=start,
                end_ms=end,
                duration_ms=duration_ms,
            )
        if start < previous_end:
            fail(
                "E_OVERLAP",
                "intervals overlap",
                interval_id=identifier,
                previous_end_ms=previous_end,
                start_ms=start,
            )
        previous_end = end


def compute_vad(envelope_payload: dict[str, Any], config: AppConfig) -> dict[str, Any]:
    clips_payload: dict[str, Any] = {}
    min_silence_samples = max(
        0, int(round(config.vad.min_silence_ms * config.sample_rate / 1000.0))
    )
    for clip_id in envelope_payload["clip_order"]:
        clip_data = envelope_payload["clips"][clip_id]
        energy = np.asarray(clip_data["energy"], dtype=np.float64)
        if energy.size == 0:
            fail(
                "E_FRAME_MISMATCH",
                "cannot run VAD on an empty envelope",
                clip_id=clip_id,
                expected_frame_count=1,
                actual_frame_count=0,
            )
        noise_floor = float(np.quantile(energy, config.vad.noise_quantile, method="linear"))
        noise_floor = max(noise_floor, 1e-12)
        low_threshold = noise_floor * config.vad.low_multiplier
        high_threshold = noise_floor * config.vad.high_multiplier
        starts = clip_data["frame_start_samples"]
        ends = clip_data["frame_end_samples"]
        raw_spans: list[tuple[int, int]] = []
        active = False
        start_frame = 0
        for index, value in enumerate(energy.tolist()):
            if not active and value >= high_threshold:
                active = True
                start_frame = index
            elif active and value <= low_threshold:
                raw_spans.append((start_frame, index))
                active = False
        if active:
            raw_spans.append((start_frame, int(energy.size)))

        merged: list[list[int]] = []
        for start_frame, end_exclusive in raw_spans:
            start_sample = int(starts[start_frame])
            end_sample = int(ends[end_exclusive - 1])
            if merged and start_sample - merged[-1][1] <= min_silence_samples:
                merged[-1][1] = max(merged[-1][1], end_sample)
            else:
                merged.append([start_sample, end_sample])

        intervals: list[dict[str, Any]] = []
        for index, (start_sample, end_sample) in enumerate(merged):
            candidate_id = f"{clip_id}:cand:{index:04d}"
            hop_samples = int(clip_data["hop_samples"])
            intervals.append(
                {
                    "candidate_id": candidate_id,
                    "clip_id": clip_id,
                    "start_sample": start_sample,
                    "end_sample": end_sample,
                    "start_ms": sample_to_ms(start_sample, config.sample_rate),
                    "end_ms": sample_to_ms(end_sample, config.sample_rate),
                    "start_frame": start_sample // hop_samples,
                }
            )
        intervals.sort(
            key=lambda item: (
                item["start_sample"],
                item["end_sample"],
                item["candidate_id"],
            )
        )
        validate_interval_bounds(intervals, float(clip_data["duration_ms"]), "candidate_id")
        clips_payload[clip_id] = {
            "clip_id": clip_id,
            "duration_ms": clip_data["duration_ms"],
            "noise_floor": noise_floor,
            "low_threshold": low_threshold,
            "high_threshold": high_threshold,
            "min_silence_samples": min_silence_samples,
            "intervals": intervals,
        }
    return {
        "schema": NODE_VERSION,
        "clip_order": envelope_payload["clip_order"],
        "vad": {
            "noise_quantile": config.vad.noise_quantile,
            "low_multiplier": config.vad.low_multiplier,
            "high_multiplier": config.vad.high_multiplier,
            "min_silence_ms": config.vad.min_silence_ms,
            "threshold_version": config.vad.threshold_version,
        },
        "clips": clips_payload,
    }


def vad_key_details(
    envelope_payload: dict[str, Any],
    upstream_hash: str,
    config: AppConfig,
) -> dict[str, Any]:
    return {
        "upstream_hash": upstream_hash,
        "envelope_curve_hashes": {
            clip_id: envelope_payload["clips"][clip_id]["curve_sha256"]
            for clip_id in envelope_payload["clip_order"]
        },
        "threshold_version": config.vad.threshold_version,
        "thresholds": {
            "noise_quantile": config.vad.noise_quantile,
            "low_multiplier": config.vad.low_multiplier,
            "high_multiplier": config.vad.high_multiplier,
            "min_silence_ms": config.vad.min_silence_ms,
        },
    }
