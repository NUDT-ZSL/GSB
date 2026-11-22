"""Deterministic per-sensor reorder, calibration and time alignment."""

from __future__ import annotations

import bisect
import math
from typing import Any, Iterable

from .models import FlightDataset, SensorDescription, SensorSequence
from .serialization import content_hash


def target_times(
    dataset: FlightDataset,
    output_rate_hz: float,
) -> tuple[float, ...]:
    if not math.isfinite(output_rate_hz) or output_rate_hz <= 0:
        output_rate_hz = 1.0
    starts = [sequence.times[0] for sequence in dataset.sequences.values() if sequence.times]
    ends = [sequence.times[-1] for sequence in dataset.sequences.values() if sequence.times]
    if not starts or not ends:
        return ()
    start = max(starts)
    end = min(ends)
    if end < start:
        return ()
    count = int(math.floor((end - start) * output_rate_hz + 1e-9)) + 1
    step = 1.0 / output_rate_hz
    values = [round(start + index * step, 12) for index in range(count)]
    if values[-1] > end + 1e-9:
        values.pop()
    return tuple(values)


def time_grid_digest(times: Iterable[float]) -> str:
    return content_hash({"times": list(times)})


def _unique_timestamp_rows(
    sequence: SensorSequence,
) -> tuple[list[float], list[int], list[tuple[float, float, float]]]:
    """Sort samples and retain the lowest sample index for duplicate timestamps."""

    rows = sorted(
        (
            (float(timestamp), sequence.sensor_id, index, row)
            for index, (timestamp, row) in enumerate(zip(sequence.times, sequence.values))
        ),
        key=lambda item: (item[0], item[1], item[2]),
    )
    seen: set[float] = set()
    unique_times: list[float] = []
    original_indices: list[int] = []
    unique_values: list[tuple[float, float, float]] = []
    for timestamp, _sensor_id, index, row in rows:
        key = round(timestamp, 12)
        if key in seen:
            continue
        seen.add(key)
        unique_times.append(key)
        original_indices.append(index)
        unique_values.append(row)
    return unique_times, original_indices, unique_values


def _apply_axis_order(
    raw: tuple[float, float, float],
    description: SensorDescription,
) -> tuple[float, float, float]:
    mapping = description.axis_mapping
    return (
        raw[mapping["x"]],
        raw[mapping["y"]],
        raw[mapping["z"]],
    )


def _calibrate(
    canonical: tuple[float, float, float],
    description: SensorDescription,
) -> tuple[float, float, float]:
    return (
        canonical[0] * description.scale[0] + description.bias[0],
        canonical[1] * description.scale[1] + description.bias[1],
        canonical[2] * description.scale[2] + description.bias[2],
    )


def preprocess_sensor(
    sequence: SensorSequence,
    description: SensorDescription,
    target: Iterable[float],
    *,
    effective_sample_rate: float | None = None,
    degradations: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    target_values = tuple(float(item) for item in target)
    unique_times, original_indices, unique_values = _unique_timestamp_rows(sequence)
    if not unique_times:
        calibrated: list[tuple[float, float, float]] = []
    else:
        calibrated = [
            _calibrate(_apply_axis_order(row, description), description)
            for row in unique_values
        ]

    aligned_values: list[list[float]] = []
    alignment_indices: list[int] = []
    for timestamp in target_values:
        if not unique_times:
            aligned_values.append([0.0, 0.0, 0.0])
            alignment_indices.append(-1)
            continue
        position = bisect.bisect_right(unique_times, timestamp + 1e-12) - 1
        if position < 0:
            position = 0
        alignment_indices.append(original_indices[position])
        aligned_values.append([float(item) for item in calibrated[position]])

    rate = (
        float(effective_sample_rate)
        if effective_sample_rate is not None
        else float(description.sample_rate)
    )
    return {
        "sensor_id": sequence.sensor_id,
        "sensor_type": sequence.sensor_type,
        "description_version": description.description_version,
        "description_hash": description.content_hash(),
        "calibration_version": description.calibration_version,
        "axis_order": list(description.axis_order),
        "axis_mapping": description.axis_mapping,
        "range": description.range_limit,
        "sample_rate": rate,
        "target_rate_hz": (
            round(1.0 / (target_values[1] - target_values[0]), 12)
            if len(target_values) > 1
            else 0.0
        ),
        "grid_digest": time_grid_digest(target_values),
        "times": list(target_values),
        "values": aligned_values,
        "alignment_indices": alignment_indices,
        "unique_timestamp_count": len(unique_times),
        "degradations": list(degradations),
    }


def preprocess_all(
    dataset: FlightDataset,
    descriptions: dict[str, SensorDescription],
    times: Iterable[float],
    *,
    effective_rates: dict[str, float] | None = None,
    degradations: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, dict[str, Any]]:
    target = tuple(times)
    result = {}
    for sensor_id in sorted(dataset.sequences):
        description = descriptions[sensor_id]
        result[sensor_id] = preprocess_sensor(
            dataset.sequences[sensor_id],
            description,
            target,
            effective_sample_rate=(effective_rates or {}).get(sensor_id),
            degradations=(degradations or {}).get(sensor_id, ()),
        )
    return result

