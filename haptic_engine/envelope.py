from __future__ import annotations

import math

import numpy as np

from .models import ControlPoint, Device, HapticEvent


def ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def event_sample_bounds(device: Device, event: HapticEvent) -> tuple[int, int]:
    start_sample = ceil_div(event.start_ms * device.sample_rate_hz, 1000)
    end_sample = ceil_div(
        (event.start_ms + event.duration_ms) * device.sample_rate_hz, 1000
    )
    if end_sample <= start_sample:
        end_sample = start_sample + 1
    return start_sample, end_sample


def physical_frame_bounds(
    device: Device, frame_index: int
) -> tuple[int, int]:
    numerator = device.sample_rate_hz * device.frame_duration_ms
    start_sample = ceil_div(frame_index * numerator, 1000)
    end_sample = ceil_div((frame_index + 1) * numerator, 1000)
    return start_sample, end_sample


def quantize_strengths(device: Device, strengths: np.ndarray) -> np.ndarray:
    scaled = (
        (strengths - device.strength_min)
        / (device.strength_max - device.strength_min)
        * (device.quantization_levels - 1)
    )
    levels = np.floor(scaled + 0.5).astype(np.int64)
    np.clip(levels, 0, device.quantization_levels - 1, out=levels)
    return levels


def quantized_strength(device: Device, level: int) -> float:
    step = (device.strength_max - device.strength_min) / (
        device.quantization_levels - 1
    )
    return round(device.strength_min + level * step, 12)


def _event_levels(device: Device, event: HapticEvent) -> tuple[int, int, np.ndarray]:
    start_sample, end_sample = event_sample_bounds(device, event)
    sample_count = end_sample - start_sample
    sample_offsets = np.arange(sample_count, dtype=np.float64)
    actual_time_ms = (start_sample + sample_offsets) * 1000.0 / device.sample_rate_hz
    local_ms = actual_time_ms - event.start_ms
    point_times = np.asarray(
        [point.offset_ms for point in event.envelope], dtype=np.float64
    )
    point_strengths = np.asarray(
        [point.strength for point in event.envelope], dtype=np.float64
    )
    if len(event.envelope) == 1:
        strengths = np.full(sample_count, point_strengths[0], dtype=np.float64)
    else:
        strengths = np.interp(local_ms, point_times, point_strengths)
    return start_sample, end_sample, quantize_strengths(device, strengths)


def build_device_levels(
    device: Device, events: tuple[HapticEvent, ...]
) -> np.ndarray:
    if not events:
        return np.zeros(0, dtype=np.int64)
    horizon = max(event_sample_bounds(device, event)[1] for event in events)
    levels = np.zeros(horizon, dtype=np.int64)
    for event in events:
        start_sample, end_sample, event_levels = _event_levels(device, event)
        np.maximum(
            levels[start_sample:end_sample],
            event_levels,
            out=levels[start_sample:end_sample],
        )
    return levels


def envelope_shape_is_valid(points: tuple[ControlPoint, ...]) -> bool:
    previous: float | None = None
    for point in points:
        if not math.isfinite(point.offset_ms) or point.offset_ms < 0:
            return False
        if previous is not None and point.offset_ms <= previous:
            return False
        previous = point.offset_ms
    return True
