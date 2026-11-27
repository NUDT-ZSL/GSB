"""Two-dimensional vector-base amplitude panning on a horizontal ring."""

from __future__ import annotations

import bisect
import math

import numpy as np

from .geometry import channel_unit_vector, wrap360


def _speaker_azimuths(layout: dict) -> tuple[list[float], np.ndarray]:
    azimuths = sorted(
        float(channel["azimuth_deg"]) for channel in layout["channels"]
    )
    if len(azimuths) < 4:
        raise ValueError("VBAP layout requires at least four channels")
    vectors = np.array(
        [channel_unit_vector(azimuth) for azimuth in azimuths], dtype=float
    )
    return azimuths, vectors


def panning_gains(azimuth_deg: float, layout: dict) -> np.ndarray:
    """Return constant-power VBAP gains in layout channel order.

    The horizontal ring uses two adjacent basis speakers and solves
    p = L*g. Gains are normalized so sum(g^2) == 1, preserving analytic
    direction and per-source energy.
    """
    azimuths, speaker_vectors = _speaker_azimuths(layout)
    channels = layout["channels"]
    target = float(wrap360(azimuth_deg))
    output = np.zeros(len(channels), dtype=float)

    distances = np.abs(((target - np.asarray(azimuths) + 180.0) % 360.0) - 180.0)
    nearest = int(np.argmin(distances))
    if distances[nearest] <= 1e-10:
        output[int(channels[nearest]["channel"])] = 1.0
        return output

    right_index = bisect.bisect_right(azimuths, target)
    left_index = (right_index - 1) % len(azimuths)
    right_index %= len(azimuths)
    basis = np.column_stack(
        [speaker_vectors[left_index], speaker_vectors[right_index]]
    )
    target_vector = channel_unit_vector(target)
    gains_pair = np.linalg.solve(basis, target_vector)
    gains_pair[np.abs(gains_pair) < 1e-12] = 0.0
    if np.any(gains_pair < -1e-9):
        raise ValueError(f"target azimuth {target} is outside VBAP basis cone")
    gains_pair = np.maximum(gains_pair, 0.0)
    norm = float(np.sqrt(np.sum(gains_pair * gains_pair)))
    if norm <= 0:
        raise ValueError("degenerate VBAP solution")
    gains_pair /= norm
    output[int(channels[left_index]["channel"])] = float(gains_pair[0])
    output[int(channels[right_index]["channel"])] = float(gains_pair[1])
    return output


def virtual_azimuth_from_gains(gains: np.ndarray, layout: dict) -> float:
    """Recover the virtual direction from VBAP gains."""
    channels = layout["channels"]
    vector = np.zeros(2, dtype=float)
    for channel, gain in zip(channels, gains, strict=True):
        vector += float(gain) * channel_unit_vector(float(channel["azimuth_deg"]))
    return float(wrap360(math.degrees(math.atan2(vector[0], vector[1]))))


def panning_metrics(azimuth_deg: float, layout: dict) -> dict:
    gains = panning_gains(azimuth_deg, layout)
    return {
        "gains": gains,
        "gain_energy": float(np.sum(gains * gains)),
        "reconstructed_azimuth_deg": virtual_azimuth_from_gains(gains, layout),
        "active_channels": [
            int(layout["channels"][index]["channel"])
            for index in np.flatnonzero(gains > 1e-12)
        ],
    }
