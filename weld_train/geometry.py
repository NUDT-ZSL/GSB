"""Coordinate transforms and analytic source geometry."""

from __future__ import annotations

import math
from typing import Iterable

import numpy as np


DEFAULT_SPEAKER_LAYOUT = {
    "layout_id": "octa-ring-8",
    "path": "vector_base_amplitude_panning_2d",
    "channel_count": 8,
    "reference_distance_m": 0.5,
    "minimum_distance_m": 0.15,
    "distance_model": "clamped_inverse_amplitude",
    "power_normalization": "constant_power",
    "channels": [
        {"channel": index, "name": name, "azimuth_deg": float(azimuth)}
        for index, (name, azimuth) in enumerate(
            [
                ("front", 0),
                ("front_right", 45),
                ("right", 90),
                ("rear_right", 135),
                ("rear", 180),
                ("rear_left", 225),
                ("left", 270),
                ("front_left", 315),
            ]
        )
    ],
}


def wrap360(value: float | np.ndarray) -> float | np.ndarray:
    """Wrap degrees into [0, 360)."""
    result = np.mod(value, 360.0)
    if np.isscalar(result):
        result = float(result)
        return 0.0 if result == 360.0 else result
    return result


def wrap180(value: float | np.ndarray) -> float | np.ndarray:
    """Wrap degrees into [-180, 180)."""
    result = np.mod(np.asarray(value, dtype=float) + 180.0, 360.0) - 180.0
    if np.isscalar(value):
        return float(result)
    return result


def spherical_from_vector(vector: Iterable[float]) -> tuple[float, float, float]:
    """Return (distance_m, azimuth_deg, elevation_deg) for x-right/y-front/z-up."""
    x, y, z = (float(component) for component in vector)
    distance = math.sqrt(x * x + y * y + z * z)
    azimuth = float(wrap360(math.degrees(math.atan2(x, y))))
    elevation = math.degrees(math.atan2(z, math.hypot(x, y)))
    return distance, azimuth, elevation


def world_to_listener(
    vector_world: Iterable[float],
    head_yaw_deg: float,
    head_pitch_deg: float = 0.0,
) -> tuple[np.ndarray, float, float, float]:
    """Rotate a world-space vector into listener coordinates.

    Convention: +x right, +y forward, +z up. Azimuth is clockwise from
    straight ahead; 0 is front, 90 right, 180 rear, 270 left.
    """
    x, y, z = (float(component) for component in vector_world)
    yaw = math.radians(head_yaw_deg)
    pitch = math.radians(head_pitch_deg)

    x_yaw = math.cos(yaw) * x - math.sin(yaw) * y
    y_yaw = math.sin(yaw) * x + math.cos(yaw) * y
    z_yaw = z

    y_local = math.cos(pitch) * y_yaw + math.sin(pitch) * z_yaw
    z_local = -math.sin(pitch) * y_yaw + math.cos(pitch) * z_yaw
    local = np.array([x_yaw, y_local, z_local], dtype=float)
    distance, azimuth, elevation = spherical_from_vector(local)
    return local, azimuth, elevation, distance


def source_position_world(
    source_type: str,
    frame_index: int,
    segment: dict,
    event: dict | None = None,
    samples_per_frame: int | None = None,
) -> np.ndarray:
    """Analytic source position in world coordinates for one frame."""
    del samples_per_frame
    pool = np.asarray(segment["pool"]["center_m"][frame_index], dtype=float)
    torch = segment["torch"]
    standoff = float(torch["standoff_m"][frame_index])
    yaw = math.radians(float(torch["yaw_deg"][frame_index]))
    pitch = math.radians(float(torch["pitch_deg"][frame_index]))

    tip = pool + np.array(
        [
            standoff * math.sin(pitch) * math.sin(yaw),
            standoff * math.sin(pitch) * math.cos(yaw),
            standoff * math.cos(pitch),
        ],
        dtype=float,
    )

    if source_type == "arc":
        return pool + 0.12 * (tip - pool) + np.array([0.0, 0.0, 0.012])
    if source_type == "pool_boil":
        return pool + np.array([0.0, 0.0, 0.004])
    if source_type == "droplet":
        phase = (frame_index % 8) / 8.0
        arc_anchor = pool + 0.12 * (tip - pool)
        return arc_anchor + phase * (tip - arc_anchor) + np.array([0.0, 0.0, 0.02])
    if source_type == "spatter":
        if event is None:
            raise ValueError("spatter source requires an event")
        frame_rate = float(segment["frame_rate_hz"])
        age_s = (frame_index - int(event["frame_index"])) / frame_rate
        azimuth = math.radians(float(event["launch_azimuth_deg"]))
        elevation = math.radians(float(event["launch_elevation_deg"]))
        speed = float(event["speed_m_s"])
        horizontal_speed = speed * math.cos(elevation)
        initial_distance = float(event.get("initial_distance_m", 0.0))
        origin = pool + np.array(
            [
                initial_distance * math.sin(azimuth),
                initial_distance * math.cos(azimuth),
                0.010,
            ],
            dtype=float,
        )
        horizontal = np.array(
            [
                horizontal_speed * math.sin(azimuth),
                horizontal_speed * math.cos(azimuth),
                0.0,
            ],
            dtype=float,
        )
        vertical = np.array([0.0, 0.0, speed * math.sin(elevation)], dtype=float)
        position = origin + horizontal * age_s + vertical * age_s
        position[2] -= 0.5 * 9.81 * age_s * age_s
        position[2] = max(0.002, float(position[2]))
        return position
    raise ValueError(f"unknown source type: {source_type}")


def attenuation_amplitude(
    distance_m: float,
    reference_distance_m: float,
    minimum_distance_m: float,
) -> float:
    """Clamped inverse distance attenuation in amplitude units."""
    return float(
        min(1.0, reference_distance_m / max(distance_m, minimum_distance_m))
    )


def channel_unit_vector(azimuth_deg: float) -> np.ndarray:
    """Horizontal unit vector for an azimuth (x=right, y=front)."""
    azimuth = math.radians(azimuth_deg)
    return np.array([math.sin(azimuth), math.cos(azimuth)], dtype=float)


def azimuth_from_panning_gains(
    gains: Iterable[float], channel_azimuths_deg: Iterable[float]
) -> float:
    """Recover a virtual azimuth from VBAP amplitude gains.

    Because p = sum(g_i * speaker_i), the normalized gain vector recovers p's
    direction exactly for a valid cone pair.
    """
    gains_array = np.asarray(list(gains), dtype=float)
    azimuths = np.asarray(list(channel_azimuths_deg), dtype=float)
    unit_vectors = np.column_stack(
        [np.sin(np.radians(azimuths)), np.cos(np.radians(azimuths))]
    )
    vector = unit_vectors @ gains_array
    return float(wrap360(math.degrees(math.atan2(vector[0], vector[1]))))


def circular_resultant(
    azimuth_deg: Iterable[float], weights: Iterable[float] | None = None
) -> dict:
    """Energy/count weighted circular resultant summary."""
    azimuths = np.asarray(list(azimuth_deg), dtype=float)
    if weights is None:
        weights_array = np.ones_like(azimuths)
    else:
        weights_array = np.asarray(list(weights), dtype=float)
    total = float(np.sum(weights_array))
    if azimuths.size == 0 or total <= 0:
        return {
            "resultant_length": 0.0,
            "resultant_azimuth_deg": None,
            "mean_azimuth_deg": None,
        }
    radians = np.radians(azimuths)
    cos_sum = float(np.sum(weights_array * np.cos(radians)))
    sin_sum = float(np.sum(weights_array * np.sin(radians)))
    resultant = math.hypot(cos_sum, sin_sum) / total
    mean_azimuth = float(wrap360(math.degrees(math.atan2(sin_sum, cos_sum))))
    return {
        "resultant_length": resultant,
        "resultant_azimuth_deg": mean_azimuth,
        "mean_azimuth_deg": mean_azimuth,
    }
