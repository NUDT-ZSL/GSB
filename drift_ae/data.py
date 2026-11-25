"""Deterministic local sensor-window fixture generation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .codec import to_native


FIXTURE_SEED = 406
DEFAULT_TIME_STEPS = 32
DEFAULT_SENSORS = 4
DEFAULT_WINDOW_COUNT = 96


@dataclass
class GeneratedWindow:
    window_id: str
    window_index: int
    stream_id: int
    segment_id: str
    values: np.ndarray
    true_label: int
    polluted_label: int
    anomaly_kind: str | None
    anomaly_detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "window_index": self.window_index,
            "stream_id": self.stream_id,
            "segment_id": self.segment_id,
            "values": to_native(self.values),
            "true_label": int(self.true_label),
            "polluted_label": int(self.polluted_label),
            "anomaly_kind": self.anomaly_kind,
            "anomaly_detail": to_native(self.anomaly_detail),
        }


def generate_windows(
    seed: int = FIXTURE_SEED,
    n_windows: int = DEFAULT_WINDOW_COUNT,
    time_steps: int = DEFAULT_TIME_STEPS,
    n_sensors: int = DEFAULT_SENSORS,
) -> list[GeneratedWindow]:
    rng = np.random.default_rng(seed)
    t = np.linspace(0.0, 2.0 * np.pi, time_steps, endpoint=False)
    offsets = np.array(
        [
            [0.00, 0.52, -0.44, 0.18],
            [1.08, -0.74, 0.63, -0.28],
            [-1.02, 0.78, 0.71, -0.62],
        ],
        dtype=np.float64,
    )
    sine_amplitudes = np.array([0.24, 0.19, 0.27])
    anomaly_plan = {
        6: "spike",
        13: "trend",
        19: "block",
        25: "spike",
        31: "block",
        37: "trend",
        43: "spike",
        49: "block",
        55: "trend",
        61: "spike",
        67: "block",
        73: "trend",
        79: "spike",
        85: "block",
        91: "trend",
    }
    anomaly_plan = {index: kind for index, kind in anomaly_plan.items() if index < n_windows}
    false_negative_indices = {index for index in (13, 43, 73) if index < n_windows}
    false_positive_indices = {index for index in (3, 22, 40, 58, 82) if index < n_windows}

    windows: list[GeneratedWindow] = []
    for index in range(n_windows):
        stream_id = index % 3
        phase = index * 0.137
        mean = (
            offsets[stream_id][None, :]
            + sine_amplitudes[stream_id] * np.sin(t[:, None] * (1.0 + 0.22 * stream_id) + phase)
            + 0.08 * np.cos(2.0 * t[:, None] + phase * 0.7)
            + 0.12 * np.sin(index / 8.0 + stream_id)
        )
        scale = 0.31 + 0.035 * np.cos(t[:, None] + index * 0.051)
        values = rng.normal(mean, np.maximum(scale, 0.22))

        # Deterministic distribution drift on stream 1 in the later batch.
        if stream_id == 1 and index >= 64:
            ramp = min(1.0, (index - 64) / 12.0)
            values = values * (1.0 + 0.18 * ramp)
            values += 0.52 * ramp + 0.018 * t[:, None] * ramp

        kind = anomaly_plan.get(index)
        detail: dict[str, Any] = {}
        if kind == "spike":
            values, detail = _inject_spike(rng, values)
        elif kind == "trend":
            values, detail = _inject_trend(rng, values, time_steps, n_sensors)
        elif kind == "block":
            values, detail = _inject_block(rng, values, time_steps, n_sensors)

        true_label = 1 if kind is not None else 0
        polluted_label = true_label
        if index in false_negative_indices:
            polluted_label = 0
        if index in false_positive_indices:
            polluted_label = 1

        windows.append(
            GeneratedWindow(
                window_id=f"w{index:04d}",
                window_index=index,
                stream_id=stream_id,
                segment_id=f"stream-{stream_id}",
                values=values,
                true_label=true_label,
                polluted_label=polluted_label,
                anomaly_kind=kind,
                anomaly_detail=detail,
            )
        )
    return windows


def default_corrections() -> list[dict[str, Any]]:
    return [
        {
            "window_id": "w0013",
            "corrected_label": "anomaly",
            "reason": "false_negative_fix",
            "affected_segment": "stream-1",
        },
        {
            "window_id": "w0043",
            "corrected_label": "anomaly",
            "reason": "false_negative_fix",
            "affected_segment": "stream-1",
        },
        {
            "window_id": "w0003",
            "corrected_label": "normal",
            "reason": "false_positive_fix",
            "affected_segment": "stream-0",
        },
        {
            "window_id": "w0022",
            "corrected_label": "normal",
            "reason": "false_positive_fix",
            "affected_segment": "stream-1",
        },
        {
            "window_id": "w0040",
            "corrected_label": "normal",
            "reason": "false_positive_fix",
            "affected_segment": "stream-1",
        },
        {
            "window_id": "w0058",
            "corrected_label": "normal",
            "reason": "false_positive_fix",
            "affected_segment": "stream-1",
        },
        {
            "window_id": "w0082",
            "corrected_label": "normal",
            "reason": "false_positive_fix",
            "affected_segment": "stream-1",
        },
    ]


def historical_drift_events(seed: int = FIXTURE_SEED) -> list[dict[str, Any]]:
    return [
        {
            "event_id": "DRIFT-HIST-01",
            "seed": seed,
            "window_range": [28, 43],
            "affected_time_windows": ["w0028:w0043"],
            "affected_segments": ["stream-0", "stream-2"],
            "old_threshold_version": 1,
            "new_threshold_version": 2,
            "attribution": [
                {"metric": "smooth_residual", "segment_id": "stream-2", "z_score": 2.31},
                {"metric": "score", "segment_id": "stream-0", "z_score": 1.87},
            ],
            "module_set": ["drift_monitor", "threshold_model", "feature_extraction"],
            "resolution": "segmented_quantile_recalibration",
        },
        {
            "event_id": "DRIFT-CURRENT-01",
            "seed": seed,
            "window_range": [64, 79],
            "affected_time_windows": ["w0064:w0079"],
            "affected_segments": ["stream-1"],
            "old_threshold_version": 2,
            "new_threshold_version": 3,
            "attribution": [
                {"metric": "score", "segment_id": "stream-1", "z_score": 3.42},
                {"metric": "mean_residual", "segment_id": "stream-1", "z_score": 3.16},
                {"metric": "smooth_residual", "segment_id": "stream-1", "z_score": 2.88},
            ],
            "module_set": ["drift_monitor", "threshold_model", "autoencoder", "feature_extraction"],
            "resolution": "local_segment_recalibration_with_ae_review",
        },
    ]


def _inject_spike(rng: np.random.Generator, values: np.ndarray) -> tuple[np.ndarray, dict]:
    time_steps, n_sensors = values.shape
    time_index = rng.integers(2, time_steps - 2, size=3)
    sensor_index = rng.integers(0, n_sensors, size=3)
    magnitude = float(rng.uniform(4.6, 6.4) * rng.choice([-1.0, 1.0]))
    for t_index, s_index in zip(time_index, sensor_index):
        values[int(t_index), int(s_index)] += magnitude
    return values, {
        "type": "spike",
        "time_index": [int(x) for x in time_index],
        "sensor_index": [int(x) for x in sensor_index],
        "magnitude": magnitude,
    }


def _inject_trend(
    rng: np.random.Generator,
    values: np.ndarray,
    time_steps: int,
    n_sensors: int,
) -> tuple[np.ndarray, dict]:
    changepoint = int(rng.integers(time_steps // 4, time_steps // 2))
    sensor = int(rng.integers(0, n_sensors))
    slope = float(rng.uniform(0.11, 0.17))
    ramp = np.zeros(time_steps, dtype=np.float64)
    ramp[changepoint:] = slope * np.arange(time_steps - changepoint, dtype=np.float64)
    values[:, sensor] += ramp
    return values, {
        "type": "trend_changepoint",
        "changepoint": changepoint,
        "sensor_index": sensor,
        "slope": slope,
        "end_shift": float(ramp[-1]),
    }


def _inject_block(
    rng: np.random.Generator,
    values: np.ndarray,
    time_steps: int,
    n_sensors: int,
) -> tuple[np.ndarray, dict]:
    start = int(rng.integers(4, time_steps - 10))
    block_time = 5
    sensor = int(rng.integers(0, max(1, n_sensors - 1)))
    shift = float(rng.uniform(2.3, 3.2) * rng.choice([-1.0, 1.0]))
    values[start : start + block_time, sensor : sensor + 2] += shift
    return values, {
        "type": "outlier_block",
        "start_time": start,
        "time_length": block_time,
        "sensor_start": sensor,
        "sensor_length": 2,
        "shift": shift,
    }
