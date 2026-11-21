from __future__ import annotations

import math
from typing import Any

import numpy as np

from .config import section_revision
from .errors import fail
from .util import seal_record, sha256_json


def _db(value: float) -> float:
    return 10.0 * math.log10(max(float(value), 1e-18))


def _build_windows(
    frame_count: int, window_frames: int, step_frames: int, call_id: str
) -> tuple[list[int], list[dict[str, Any]]]:
    if window_frames <= 0 or frame_count < window_frames:
        fail(
            "E_WINDOW_INVALID",
            call_id=call_id,
            frame_count=frame_count,
            window_frames=window_frames,
        )
    if step_frames <= 0:
        fail("E_STEP_INVALID", call_id=call_id, step_frames=step_frames)
    if window_frames == frame_count:
        starts = [0]
    else:
        starts = list(range(0, frame_count - window_frames + 1, step_frames))
        if starts[-1] != frame_count - window_frames:
            starts.append(frame_count - window_frames)
    windows = [
        {
            "window_index": index,
            "start_frame": start,
            "end_frame": start + window_frames,
            "frame_count": window_frames,
        }
        for index, start in enumerate(starts)
    ]
    return starts, windows


def _detect_steps(
    curve: list[float],
    is_speech: list[bool],
    threshold_db: float,
    min_step_frames: int,
    min_segment_frames: int,
) -> list[dict[str, Any]]:
    frame_count = len(curve)
    candidates: list[tuple[float, int, float, float]] = []
    runs: list[tuple[int, int]] = []
    cursor = 0
    while cursor < frame_count:
        if is_speech[cursor]:
            cursor += 1
            continue
        end = cursor + 1
        while end < frame_count and not is_speech[end]:
            end += 1
        runs.append((cursor, end))
        cursor = end

    run_levels = [
        float(np.median(np.asarray(curve[start:end], dtype=np.float64)))
        for start, end in runs
    ]
    baseline_level = float(
        np.median(np.asarray(run_levels, dtype=np.float64))
    )
    for run, level in zip(runs, run_levels):
        run_length = run[1] - run[0]
        if run_length < min_step_frames:
            continue
        delta = abs(_db(level) - _db(baseline_level))
        if delta >= threshold_db:
            candidates.append(
                (delta, run[0], _db(baseline_level), _db(level))
            )
            candidates.append(
                (delta, run[1], _db(level), _db(baseline_level))
            )

    half = max(1, min_step_frames)
    for run_start, run_end in runs:
        for boundary in range(
            run_start + min_segment_frames,
            run_end - min_segment_frames + 1,
        ):
            if boundary - half < run_start or boundary + half > run_end:
                continue
            left = float(np.median(curve[boundary - half : boundary]))
            right = float(np.median(curve[boundary : boundary + half]))
            left_db = _db(left)
            right_db = _db(right)
            delta = abs(right_db - left_db)
            if delta >= threshold_db:
                candidates.append((delta, boundary, left_db, right_db))
    candidates.sort(key=lambda item: item[0], reverse=True)
    selected: list[tuple[float, int, float, float]] = []
    for candidate in candidates:
        boundary = candidate[1]
        if boundary < min_segment_frames or boundary > frame_count - min_segment_frames:
            continue
        if all(abs(boundary - existing[1]) >= min_step_frames for existing in selected):
            selected.append(candidate)
    selected.sort(key=lambda item: item[1])
    return [
        {
            "step_index": index,
            "boundary_frame": boundary,
            "left_estimate_db": round(left_db, 12),
            "right_estimate_db": round(right_db, 12),
            "delta_db": round(delta, 12),
        }
        for index, (delta, boundary, left_db, right_db) in enumerate(selected)
    ]


def compute_noise_record(
    feature_record: dict[str, Any],
    noise_config: dict[str, Any],
    config: dict[str, Any],
    version: int,
) -> dict[str, Any]:
    call_id = str(feature_record["call_id"])
    frames = feature_record["frames"]
    energies = [float(frame["energy"]) for frame in frames]
    is_speech = [bool(frame.get("is_speech", False)) for frame in frames]
    frame_count = len(energies)
    window_frames = int(noise_config.get("window_frames", 0))
    step_frames = int(noise_config.get("step_frames", 0))
    starts, windows = _build_windows(
        frame_count, window_frames, step_frames, call_id
    )

    quantile = float(noise_config.get("quantile", 0.2))
    if not 0.0 <= quantile <= 1.0:
        fail(
            "E_WINDOW_INVALID",
            call_id=call_id,
            detail="quantile must be between 0 and 1",
            quantile=quantile,
        )
    energy_array = np.asarray(energies, dtype=np.float64)
    valid_estimates: list[float | None] = []
    minimum_silence_samples = max(3, min(window_frames, window_frames // 4))
    for window in windows:
        start = int(window["start_frame"])
        end = int(window["end_frame"])
        silence_values = [
            energy_array[index]
            for index in range(start, end)
            if not is_speech[index]
        ]
        window["silence_sample_count"] = len(silence_values)
        if len(silence_values) >= minimum_silence_samples:
            estimate = float(
                np.quantile(
                    np.asarray(silence_values, dtype=np.float64),
                    quantile,
                    method="linear",
                )
            )
            valid_estimates.append(round(estimate, 12))
            window["insufficient_silence_samples"] = False
        else:
            valid_estimates.append(None)
            window["insufficient_silence_samples"] = True
    if not any(estimate is not None for estimate in valid_estimates):
        for window in windows:
            start = int(window["start_frame"])
            end = int(window["end_frame"])
            estimate = float(
                np.quantile(
                    energy_array[start:end],
                    quantile,
                    method="linear",
                )
            )
            index = int(window["window_index"])
            valid_estimates[index] = round(estimate, 12)
            window["insufficient_silence_samples"] = False
    valid_window_indices = [
        index for index, estimate in enumerate(valid_estimates) if estimate is not None
    ]
    for window, estimate in zip(windows, valid_estimates):
        if estimate is None:
            continue
        window["estimate_energy"] = estimate
        window["estimate_db"] = round(_db(estimate), 12)

    frame_guesses: list[float | None] = []
    for frame_index in range(frame_count):
        if is_speech[frame_index]:
            frame_guesses.append(None)
            continue
        covering = [
            index
            for index, start in enumerate(starts)
            if start <= frame_index < start + window_frames
            and valid_estimates[index] is not None
        ]
        if covering:
            frame_guesses.append(
                float(
                    np.median(
                        [float(valid_estimates[index]) for index in covering]
                    )
                )
            )
        else:
            nearest = min(
                valid_window_indices,
                key=lambda index: abs(
                    (starts[index] + window_frames / 2.0)
                    - (frame_index + 0.5)
                ),
            )
            frame_guesses.append(float(valid_estimates[nearest]))

    known_indices = [
        index for index, value in enumerate(frame_guesses) if value is not None
    ]
    if not known_indices:
        fail(
            "E_WINDOW_INVALID",
            call_id=call_id,
            frame_count=frame_count,
            window_frames=window_frames,
            detail="no usable non-speech samples in the call",
        )
    curve: list[float] = []
    assigned_windows: list[int] = []
    for frame_index in range(frame_count):
        if frame_guesses[frame_index] is not None:
            curve.append(round(float(frame_guesses[frame_index]), 12))
            candidate = min(frame_index // step_frames, len(starts) - 1)
            start = starts[candidate]
            if frame_index < start or frame_index >= start + window_frames:
                candidate = len(starts) - 1
            if valid_estimates[candidate] is None:
                candidate = min(
                    valid_window_indices,
                    key=lambda index: abs(
                        (starts[index] + window_frames / 2.0)
                        - (frame_index + 0.5)
                    ),
                )
            assigned_windows.append(candidate)
            continue
        left_index = max(
            (index for index in known_indices if index < frame_index),
            default=None,
        )
        right_index = min(
            (index for index in known_indices if index > frame_index),
            default=None,
        )
        if left_index is None:
            value = float(frame_guesses[right_index])
            window_index = min(
                valid_window_indices,
                key=lambda index: abs(starts[index] - right_index),
            )
        elif right_index is None:
            value = float(frame_guesses[left_index])
            window_index = min(
                valid_window_indices,
                key=lambda index: abs(starts[index] - left_index),
            )
        else:
            left_value = float(frame_guesses[left_index])
            right_value = float(frame_guesses[right_index])
            fraction = (frame_index - left_index) / (right_index - left_index)
            value = left_value + (right_value - left_value) * fraction
            window_index = min(
                valid_window_indices,
                key=lambda index: abs(
                    (starts[index] + window_frames / 2.0)
                    - (frame_index + 0.5)
                ),
            )
        curve.append(round(value, 12))
        assigned_windows.append(window_index)

    threshold_db = float(noise_config.get("step_threshold_db", 6.0))
    min_step_frames = int(noise_config.get("min_step_frames", 8))
    min_segment_frames = int(noise_config.get("min_segment_frames", 8))
    steps = _detect_steps(
        curve, is_speech, threshold_db, min_step_frames, min_segment_frames
    )

    boundaries = [0] + [int(step["boundary_frame"]) for step in steps] + [frame_count]
    segments: list[dict[str, Any]] = []
    for index in range(len(boundaries) - 1):
        start = boundaries[index]
        end = boundaries[index + 1]
        values = np.asarray(curve[start:end], dtype=np.float64)
        estimate = float(np.median(values))
        dispersion = float(np.std(values, ddof=0))
        segments.append(
            {
                "noise_segment_id": f"{call_id}-N{index + 1:03d}",
                "start_frame": start,
                "end_frame": end,
                "frame_count": end - start,
                "estimate_energy": round(estimate, 12),
                "estimate_db": round(_db(estimate), 12),
                "dispersion_energy": round(dispersion, 12),
                "dispersion_db": round(
                    _db(max(estimate + dispersion, 1e-18))
                    - _db(max(estimate, 1e-18)),
                    12,
                ),
            }
        )

    cover_records = [
        {
            "window_index": window["window_index"],
            "start_frame": window["start_frame"],
            "end_frame": window["end_frame"],
            "frame_count": window["frame_count"],
            "silence_sample_count": window["silence_sample_count"],
            "insufficient_silence_samples": window["insufficient_silence_samples"],
            "estimate_energy": window.get("estimate_energy"),
            "estimate_db": window.get("estimate_db"),
        }
        for window in windows
    ]
    noise_config_revision = section_revision(config, "noise_floor")
    record: dict[str, Any] = {
        "call_id": call_id,
        "noise_floor_version": int(version),
        "noise_config_revision": noise_config_revision,
        "source_feature_version": int(feature_record["feature_version"]),
        "source_feature_checksum": feature_record["record_checksum"],
        "frame_count": frame_count,
        "window_frames": window_frames,
        "step_frames": step_frames,
        "quantile": quantile,
        "noise_floor_curve": curve,
        "assigned_window_index": assigned_windows,
        "windows": cover_records,
        "steps": steps,
        "step_count": len(steps),
        "segments": segments,
        "curve_checksum": sha256_json(curve),
    }
    record["dependencies"] = {
        "source_feature_checksum": feature_record["record_checksum"],
        "noise_config_revision": noise_config_revision,
        "frame_count": frame_count,
    }
    return seal_record(record)


def validate_noise_record(record: dict[str, Any]) -> None:
    curve = record.get("noise_floor_curve")
    if not isinstance(curve, list) or len(curve) != record.get("frame_count"):
        fail("E_INCONSISTENT", stage="noise_floor", call_id=record.get("call_id"))
    if record.get("curve_checksum") != sha256_json(curve):
        fail("E_INCONSISTENT", stage="noise_floor", call_id=record.get("call_id"))
    if record.get("step_count") != len(record.get("steps", [])):
        fail("E_INCONSISTENT", stage="noise_floor", call_id=record.get("call_id"))

