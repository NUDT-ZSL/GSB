"""Per-frame acoustic features, all computed in float64 deterministically."""
from __future__ import annotations

import math

import numpy as np

from .audio import Frame


def log_energy(frame: np.ndarray) -> float:
    rms = math.sqrt(float(np.mean(np.square(frame, dtype=np.float64))))
    if rms <= 0.0:
        return -230.258509  # log(1e-100), fixed sentinel for digital silence
    return math.log(rms)


def zero_crossing_rate(frame: np.ndarray) -> float:
    shifted = frame[1:]
    crossings = np.count_nonzero(np.signbit(shifted) != np.signbit(frame[:-1]))
    return float(crossings) / float(frame.shape[0] - 1)


def spectral_flatness(frame: np.ndarray) -> float:
    windowed = frame * np.hanning(frame.shape[0]).astype(np.float64)
    spectrum = np.abs(np.fft.rfft(windowed))[1:]  # drop DC bin
    if spectrum.shape[0] == 0 or not np.all(np.isfinite(spectrum)):
        return 0.0
    nonzero = spectrum[spectrum > 0.0]
    if nonzero.shape[0] == 0:
        return 0.0
    geometric_mean = math.exp(float(np.mean(np.log(nonzero, dtype=np.float64))))
    arithmetic_mean = float(np.mean(spectrum, dtype=np.float64))
    if arithmetic_mean <= 0.0:
        return 0.0
    return geometric_mean / arithmetic_mean


def compute_frame_features(frame: Frame, decimals: int) -> dict:
    energy = round(log_energy(frame.samples), decimals)
    zcr = round(zero_crossing_rate(frame.samples), decimals)
    flatness = round(spectral_flatness(frame.samples), decimals)
    return {
        "frame": frame.index,
        "timestamp_ms": frame.timestamp_ms,
        "zero_padded": frame.zero_padded,
        "log_energy": energy,
        "zcr": zcr,
        "flatness": flatness,
    }


def estimate_noise_floor(feature_rows: list[dict], window_frames: int) -> float:
    """Adaptive low threshold: mean log-energy of the quietest window frames."""
    if not feature_rows:
        return -230.258509
    # Digital-silence frames (e.g. injected mute) do not represent the noise floor.
    usable = [row["log_energy"] for row in feature_rows
              if row["log_energy"] > -230.258509]
    pool = usable or [row["log_energy"] for row in feature_rows]
    window = min(window_frames, len(pool))
    energies = sorted(pool)[:window]
    return float(sum(energies) / len(energies))
