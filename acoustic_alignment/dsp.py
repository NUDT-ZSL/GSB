"""Deterministic cross-correlation and frequency-band phase measurements."""
from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Callable

import numpy as np
from scipy import signal

from .codes import BusinessFailure, Code
from .task_model import ms_to_samples


@dataclass
class DelayEstimate:
    delay_samples: int
    delay_float_samples: float
    delta_samples: float
    correlation_peak: float
    peak_index: int
    window_samples: int


def rms_level(values: np.ndarray) -> float:
    values = np.asarray(values, dtype=np.float64)
    if values.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(values * values, dtype=np.float64)))


def peak_level(values: np.ndarray) -> float:
    values = np.asarray(values, dtype=np.float64)
    if values.size == 0:
        return 0.0
    return float(np.max(np.abs(values)))


def resample_to_rate(values: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    if source_rate == target_rate or values.size == 0:
        return values.copy()
    ratio = Fraction(int(target_rate), int(source_rate))
    return signal.resample_poly(values, ratio.numerator, ratio.denominator).astype(np.float64, copy=False)


def search_window_in_rate(
    search_window_samples: float | int | None,
    search_window_ms: float | None,
    sample_rate: int,
) -> int:
    if search_window_samples is not None:
        return max(1, int(math.ceil(float(search_window_samples))))
    if search_window_ms is not None:
        return max(1, ms_to_samples(float(search_window_ms), sample_rate))
    raise BusinessFailure(Code.E_WINDOW, "no correlation search window configured")


def estimate_integer_delay(
    candidate: np.ndarray,
    reference: np.ndarray,
    window_samples: int,
    correlation_threshold: float,
) -> DelayEstimate:
    candidate = np.ascontiguousarray(candidate, dtype=np.float64)
    reference = np.ascontiguousarray(reference, dtype=np.float64)
    minimum_length = 2 * int(window_samples) + 4
    if candidate.size < minimum_length or reference.size < minimum_length:
        raise BusinessFailure(
            Code.E_SEGMENT,
            "excitation segment is shorter than the correlation search window",
            {
                "required_samples": minimum_length,
                "candidate_samples": int(candidate.size),
                "reference_samples": int(reference.size),
            },
        )

    candidate_energy = float(np.dot(candidate, candidate))
    reference_energy = float(np.dot(reference, reference))
    energy = math.sqrt(max(0.0, candidate_energy) * max(0.0, reference_energy))
    if not math.isfinite(energy) or energy <= np.finfo(np.float64).eps:
        raise BusinessFailure(Code.E_PEAK, "correlation inputs contain no effective energy")

    correlation = signal.correlate(candidate, reference, mode="full", method="fft")
    lags = signal.correlation_lags(candidate.size, reference.size, mode="full")
    normalized = correlation / energy
    mask = np.abs(lags) <= int(window_samples)
    if not np.any(mask):
        raise BusinessFailure(Code.E_PEAK, "correlation search window contains no lag")

    masked_indices = np.flatnonzero(mask)
    masked_values = normalized[masked_indices]
    local_peak = int(np.argmax(masked_values))
    peak_index = int(masked_indices[local_peak])
    peak_lag = int(lags[peak_index])
    peak_value = float(normalized[peak_index])

    if not math.isfinite(peak_value):
        raise BusinessFailure(Code.E_PEAK, "correlation peak is non-finite")
    if peak_value < float(correlation_threshold):
        raise BusinessFailure(
            Code.E_PEAK,
            "correlation peak is below the configured validity threshold",
            {"correlation_peak": peak_value, "correlation_threshold": float(correlation_threshold)},
        )

    delta = 0.0
    if 0 < peak_index < normalized.size - 1:
        y_minus = float(normalized[peak_index - 1])
        y_zero = float(normalized[peak_index])
        y_plus = float(normalized[peak_index + 1])
        denominator = y_minus - 2.0 * y_zero + y_plus
        if abs(denominator) > np.finfo(np.float64).eps:
            candidate_delta = 0.5 * (y_minus - y_plus) / denominator
            if math.isfinite(candidate_delta) and abs(candidate_delta) <= 1.0:
                delta = float(candidate_delta)

    return DelayEstimate(
        delay_samples=peak_lag,
        delay_float_samples=float(np.float64(peak_lag) + np.float64(delta)),
        delta_samples=float(delta),
        correlation_peak=peak_value,
        peak_index=peak_index,
        window_samples=int(window_samples),
    )


def common_analysis_length(*arrays: np.ndarray) -> int:
    lengths = [len(array) for array in arrays]
    if not lengths or any(length == 0 for length in lengths):
        raise BusinessFailure(Code.E_SEGMENT, "excitation analysis segment is empty")
    return int(min(lengths))


def band_phase_delay(
    reference: np.ndarray,
    candidate: np.ndarray,
    sample_rate: int,
    min_hz: float,
    max_hz: float,
    energy_floor: float,
    unwrap_limit: float,
) -> tuple[float, float, int]:
    length = common_analysis_length(reference, candidate)
    reference = np.ascontiguousarray(reference[:length], dtype=np.float64)
    candidate = np.ascontiguousarray(candidate[:length], dtype=np.float64)

    window = signal.windows.hann(length, sym=False)
    reference_fft = np.fft.rfft(reference * window)
    candidate_fft = np.fft.rfft(candidate * window)
    frequencies = np.fft.rfftfreq(length, d=1.0 / sample_rate)

    band_mask = (frequencies >= float(min_hz)) & (frequencies <= float(max_hz))
    band_indices = np.flatnonzero(band_mask)
    if band_indices.size < 3:
        raise BusinessFailure(
            Code.E_BAND,
            "frequency band contains too few FFT bins",
            {
                "bin_count": int(band_indices.size),
                "min_hz": min_hz,
                "max_hz": max_hz,
                "sample_rate": sample_rate,
            },
        )

    cross_spectrum = candidate_fft[band_indices] * np.conj(reference_fft[band_indices])
    magnitude = np.abs(cross_spectrum)
    maximum = float(np.max(magnitude))
    if maximum <= np.finfo(np.float64).eps:
        raise BusinessFailure(Code.E_PHASE, "frequency band has no cross-spectral energy")

    selected = magnitude >= (float(energy_floor) * maximum)
    if int(np.count_nonzero(selected)) < 3:
        raise BusinessFailure(
            Code.E_PHASE,
            "frequency band contains too few effective phase scatter points",
            {"point_count": int(np.count_nonzero(selected))},
        )

    selected_indices = band_indices[selected]
    selected_cross = cross_spectrum[selected]
    selected_frequencies = frequencies[selected_indices].astype(np.float64)
    phase_wrapped = np.angle(selected_cross).astype(np.float64)
    phase_unwrapped = np.unwrap(phase_wrapped)

    correction = phase_unwrapped - phase_wrapped
    if float(np.max(np.abs(correction))) > float(unwrap_limit):
        raise BusinessFailure(
            Code.E_PHASE,
            "phase unwrapping correction exceeds configured upper limit",
            {
                "point_count": int(selected_indices.size),
                "unwrap_correction_rad": float(np.max(np.abs(correction))),
                "phase_unwrap_limit": float(unwrap_limit),
            },
        )

    slope, intercept = np.polyfit(selected_frequencies, phase_unwrapped, 1)
    if not (math.isfinite(float(slope)) and math.isfinite(float(intercept))):
        raise BusinessFailure(Code.E_PHASE, "phase regression produced a non-finite slope")

    phase_delay_samples = -float(slope) * float(sample_rate) / (2.0 * math.pi)
    return float(slope), phase_delay_samples, int(selected_indices.size)


StopCheck = Callable[[], None]
BandCallback = Callable[[str, str, int], None]
