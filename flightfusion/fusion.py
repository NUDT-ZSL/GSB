"""Complementary-filter fusion over per-sensor graph contributions."""

from __future__ import annotations

import bisect
import math
from collections import deque
from typing import Any, Iterable

from .mathutils import (
    VEC3,
    cross,
    dot,
    normalize3,
    quat_angular_distance_deg,
    quat_from_rotation_vector,
    quat_multiply,
    quat_normalize,
    quat_to_euler_zyx,
    rotate_world_to_body,
)
from .models import FusionConfig


def make_contribution(
    preprocessed: dict[str, Any],
    config: FusionConfig,
) -> dict[str, Any]:
    sensor_type = preprocessed["sensor_type"]
    values = preprocessed["values"]
    payload: dict[str, Any] = {
        "sensor_id": preprocessed["sensor_id"],
        "sensor_type": sensor_type,
        "times": list(preprocessed["times"]),
        "preprocess_content_hash": preprocessed.get("content_hash"),
        "description_version": preprocessed["description_version"],
        "calibration_version": preprocessed["calibration_version"],
    }
    if sensor_type == "gyro":
        payload["rad_s"] = [
            [math.radians(float(component)) for component in row] for row in values
        ]
        payload["weight"] = 1.0
    elif sensor_type == "accel":
        payload["values_g"] = [list(row) for row in values]
        payload["unit"] = [list(normalize3(row)) for row in values]
        payload["weight"] = float(config.accel_weight)
        payload["gravity_constant"] = float(config.gravity_constant)
    else:
        payload["unit"] = [list(normalize3(row)) for row in values]
        payload["weight"] = float(config.mag_weight)
        payload["mag_reference"] = list(config.mag_reference)
    return payload


def _matrix_to_quat(matrix: list[list[float]]) -> tuple[float, float, float, float]:
    m00, m01, m02 = matrix[0]
    m10, m11, m12 = matrix[1]
    m20, m21, m22 = matrix[2]
    trace = m00 + m11 + m22
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * scale
        x = (m21 - m12) / scale
        y = (m02 - m20) / scale
        z = (m10 - m01) / scale
    elif m00 > m11 and m00 > m22:
        scale = math.sqrt(1.0 + m00 - m11 - m22) * 2.0
        w = (m21 - m12) / scale
        x = 0.25 * scale
        y = (m01 + m10) / scale
        z = (m02 + m20) / scale
    elif m11 > m22:
        scale = math.sqrt(1.0 + m11 - m00 - m22) * 2.0
        w = (m02 - m20) / scale
        x = (m01 + m10) / scale
        y = 0.25 * scale
        z = (m12 + m21) / scale
    else:
        scale = math.sqrt(1.0 + m22 - m00 - m11) * 2.0
        w = (m10 - m01) / scale
        x = (m02 + m20) / scale
        y = (m12 + m21) / scale
        z = 0.25 * scale
    return quat_normalize((w, x, y, z))


def _initial_quaternion(
    accel: list[list[float]] | None,
    mag: list[list[float]] | None,
    mag_reference: Iterable[float],
) -> tuple[float, float, float, float]:
    if not accel:
        return (1.0, 0.0, 0.0, 0.0)
    up_body = normalize3(accel[0])
    down_body = tuple(-item for item in up_body)
    if not mag:
        mag_body = normalize3((1.0, 0.0, 0.0))
    else:
        mag_body = normalize3(mag[0])
    mag_world = normalize3(mag_reference)
    down_world = (0.0, 0.0, -1.0)
    east_world = normalize3(
        (
            mag_world[0] - dot(mag_world, down_world) * down_world[0],
            mag_world[1] - dot(mag_world, down_world) * down_world[1],
            mag_world[2] - dot(mag_world, down_world) * down_world[2],
        )
    )
    north_world = cross(down_world, east_world)
    east_body = normalize3(
        (
            mag_body[0] - dot(mag_body, down_body) * down_body[0],
            mag_body[1] - dot(mag_body, down_body) * down_body[1],
            mag_body[2] - dot(mag_body, down_body) * down_body[2],
        )
    )
    north_body = cross(down_body, east_body)
    # R * [east_body, north_body, down_body] = [east_world, north_world, down_world]
    world_basis = [
        [east_world[0], north_world[0], down_world[0]],
        [east_world[1], north_world[1], down_world[1]],
        [east_world[2], north_world[2], down_world[2]],
    ]
    body_basis = [
        [east_body[0], north_body[0], down_body[0]],
        [east_body[1], north_body[1], down_body[1]],
        [east_body[2], north_body[2], down_body[2]],
    ]
    # R = world_basis * transpose(body_basis)
    rotation = [
        [
            sum(world_basis[row][k] * body_basis[column][k] for k in range(3))
            for column in range(3)
        ]
        for row in range(3)
    ]
    return _matrix_to_quat(rotation)


def merge_complementary_filter(
    contributions: dict[str, dict[str, Any]],
    config: FusionConfig,
    *,
    target_times: Iterable[float],
    sensor_versions: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    times = tuple(float(item) for item in target_times)
    gyro_values = contributions.get("gyro", {}).get("rad_s", [])
    accel_units = contributions.get("accel", {}).get("unit", [])
    accel_values_g = contributions.get("accel", {}).get("values_g", [])
    mag_units = contributions.get("mag", {}).get("unit", [])
    accel_weight = max(0.0, float(config.accel_weight))
    mag_weight = max(0.0, float(config.mag_weight))
    gain = max(0.0, min(1.0, float(config.complementary_gain)))
    window = max(1, int(config.gyro_bias_window))
    gravity = max(1e-12, float(config.gravity_constant))

    q = _initial_quaternion(accel_units, mag_units, config.mag_reference)
    bias_history: deque[tuple[float, float, float]] = deque(maxlen=window)
    bias_estimate: VEC3 = (0.0, 0.0, 0.0)
    quaternions: list[list[float]] = []
    eulers: list[list[float]] = []
    residuals: list[float] = []
    accel_residuals: list[float] = []
    accel_residuals_mps2: list[float] = []
    mag_residuals: list[float] = []
    bias_estimates: list[list[float]] = []

    previous_time = times[0] if times else 0.0
    for index, timestamp in enumerate(times):
        dt = max(0.0, timestamp - previous_time)
        previous_time = timestamp
        if index == 0:
            dt = 1.0 / max(float(config.output_rate_hz), 1.0)

        gyro = (
            tuple(float(item) for item in gyro_values[index])
            if index < len(gyro_values)
            else (0.0, 0.0, 0.0)
        )
        used_gyro = tuple(gyro[axis] - bias_estimate[axis] for axis in range(3))
        q = quat_multiply(q, quat_from_rotation_vector(tuple(item * dt for item in used_gyro)))

        predicted_up = rotate_world_to_body((0.0, 0.0, 1.0), q)
        accel_unit = (
            tuple(float(item) for item in accel_units[index])
            if index < len(accel_units)
            else predicted_up
        )
        measured_accel_g = (
            tuple(float(item) for item in accel_values_g[index])
            if index < len(accel_values_g)
            else accel_unit
        )
        predicted_specific_force = tuple(item * gravity for item in predicted_up)
        measured_specific_force = tuple(item * gravity for item in measured_accel_g)
        accel_residual_mps2 = math.sqrt(
            sum(
                (measured_specific_force[axis] - predicted_specific_force[axis]) ** 2
                for axis in range(3)
            )
        )
        accel_residual = 1.0 - max(-1.0, min(1.0, dot(accel_unit, predicted_up)))

        predicted_mag = rotate_world_to_body(
            normalize3(config.mag_reference), q
        )
        mag_unit = (
            tuple(float(item) for item in mag_units[index])
            if index < len(mag_units)
            else predicted_mag
        )
        mag_residual = 1.0 - max(-1.0, min(1.0, dot(mag_unit, predicted_mag)))

        accel_correction = tuple(
            gain * accel_weight * item
            for item in cross(accel_unit, predicted_up)
        )
        mag_correction = tuple(
            gain * mag_weight * item
            for item in cross(mag_unit, predicted_mag)
        )
        total_correction = tuple(
            accel_correction[axis] + mag_correction[axis] for axis in range(3)
        )
        q = quat_multiply(q, quat_from_rotation_vector(total_correction))
        q = quat_normalize(q)
        bias_history.append(total_correction)
        historical_mean = tuple(
            sum(item[axis] for item in bias_history) / len(bias_history)
            for axis in range(3)
        )
        bias_estimate = tuple(gain * item for item in historical_mean)  # type: ignore[assignment]

        quaternions.append([float(item) for item in q])
        eulers.append([math.degrees(item) for item in quat_to_euler_zyx(q)])
        accel_residuals.append(float(accel_residual))
        accel_residuals_mps2.append(float(accel_residual_mps2))
        mag_residuals.append(float(mag_residual))
        residuals.append(
            float(accel_weight * accel_residual + mag_weight * mag_residual)
        )
        bias_estimates.append([float(item) for item in bias_estimate])

    return {
        "config_version": config.config_version,
        "config_hash": config.content_hash(),
        "times": list(times),
        "quaternions": quaternions,
        "euler_deg": eulers,
        "residual": residuals,
        "accel_residual": accel_residuals,
        "accel_residual_mps2": accel_residuals_mps2,
        "mag_residual": mag_residuals,
        "gyro_bias_estimate": bias_estimates,
        "sensor_versions": sensor_versions,
        "complementary_gain": gain,
        "accel_weight": accel_weight,
        "mag_weight": mag_weight,
        "gyro_bias_window": window,
        "gravity_constant": float(config.gravity_constant),
    }


def _percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    position = fraction * (len(sorted_values) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return sorted_values[lower]
    blend = position - lower
    return sorted_values[lower] * (1.0 - blend) + sorted_values[upper] * blend


def make_output(
    fusion: dict[str, Any],
    truth_times: Iterable[float],
    truth_quaternions: Iterable[Iterable[float]],
) -> dict[str, Any]:
    times = [float(item) for item in fusion["times"]]
    truth_time_values = [float(item) for item in truth_times]
    truth_quats = [tuple(float(item) for item in q) for q in truth_quaternions]
    truth_eulers: list[list[float]] = []
    errors: list[float] = []
    for timestamp, estimate in zip(times, fusion["quaternions"]):
        position = bisect.bisect_left(truth_time_values, timestamp)
        if position >= len(truth_quats):
            position = len(truth_quats) - 1
        if position > 0 and abs(truth_time_values[position - 1] - timestamp) <= abs(
            truth_time_values[position] - timestamp
        ):
            position -= 1
        truth_q = truth_quats[position]
        truth_eulers.append(
            [math.degrees(item) for item in quat_to_euler_zyx(truth_q)]
        )
        errors.append(quat_angular_distance_deg(estimate, truth_q))
    sorted_errors = sorted(errors)
    residuals = [float(item) for item in fusion["residual"]]
    accel_residuals = [float(item) for item in fusion["accel_residual"]]
    accel_residuals_mps2 = [
        float(item) for item in fusion.get("accel_residual_mps2", [])
    ]
    mag_residuals = [float(item) for item in fusion["mag_residual"]]
    stats = {
        "sample_count": len(times),
        "attitude_error_deg_mean": (
            sum(errors) / len(errors) if errors else 0.0
        ),
        "attitude_error_deg_p95": _percentile(sorted_errors, 0.95),
        "attitude_error_deg_max": max(errors) if errors else 0.0,
        "attitude_error_deg_final": errors[-1] if errors else 0.0,
        "accel_residual_mean": (
            sum(accel_residuals) / len(accel_residuals) if accel_residuals else 0.0
        ),
        "accel_residual_mps2_mean": (
            sum(accel_residuals_mps2) / len(accel_residuals_mps2)
            if accel_residuals_mps2
            else 0.0
        ),
        "mag_residual_mean": (
            sum(mag_residuals) / len(mag_residuals) if mag_residuals else 0.0
        ),
        "fusion_residual_mean": (
            sum(residuals) / len(residuals) if residuals else 0.0
        ),
    }
    return {
        "config_version": fusion["config_version"],
        "config_hash": fusion["config_hash"],
        "sensor_versions": fusion["sensor_versions"],
        "times": times,
        "quaternions": fusion["quaternions"],
        "euler_deg": fusion["euler_deg"],
        "truth_euler_deg": truth_eulers,
        "residual": residuals,
        "accel_residual": accel_residuals,
        "accel_residual_mps2": accel_residuals_mps2,
        "mag_residual": mag_residuals,
        "gyro_bias_estimate": fusion["gyro_bias_estimate"],
        "stats": stats,
        "fusion_content_hash": fusion.get("content_hash"),
    }

