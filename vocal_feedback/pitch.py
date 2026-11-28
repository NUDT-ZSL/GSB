from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .audio import FramedAudio, hann_window
from .config import Config


@dataclass
class PitchCandidate:
    method: str
    f0_hz: float
    periodicity: float
    harmonic_score: float
    harmonic_coverage: float
    harmonic_fraction: float
    quality: float
    octave_suspect: bool = False


def hz_to_midi(frequency_hz: float | None) -> float | None:
    if frequency_hz is None or not math.isfinite(frequency_hz) or frequency_hz <= 0:
        return None
    return 69.0 + 12.0 * math.log2(frequency_hz / 440.0)


def midi_to_hz(midi: float | None) -> float | None:
    if midi is None or not math.isfinite(midi):
        return None
    return 440.0 * (2.0 ** ((midi - 69.0) / 12.0))


_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def midi_to_note_name(midi: float | None, cents: bool = False) -> str | None:
    if midi is None or not math.isfinite(midi):
        return None
    rounded = int(round(midi))
    name = _NOTE_NAMES[rounded % 12]
    octave = rounded // 12 - 1
    if cents:
        offset = int(round((midi - rounded) * 100.0))
        return f"{name}{octave}{offset:+d}c"
    return f"{name}{octave}"


def note_name_to_midi(value: str | int | float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    text = text.replace("♯", "#").replace("♭", "b")
    if not text:
        raise ValueError("empty note name")
    letter = text[0].upper()
    semitones = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
    if letter not in semitones:
        raise ValueError(f"invalid note name: {value}")
    position = 1
    accidental = 0
    while position < len(text) and text[position] in "#b":
        accidental += 1 if text[position] == "#" else -1
        position += 1
    try:
        octave = int(text[position:])
    except ValueError as exc:
        raise ValueError(f"invalid octave in note name: {value}") from exc
    return float(12 * (octave + 1) + semitones[letter] + accidental)


def _parabolic_peak(values: np.ndarray, index: int) -> tuple[float, float]:
    if index <= 0 or index >= len(values) - 1:
        return float(index), float(values[index])
    left, center, right = values[index - 1], values[index], values[index + 1]
    denominator = left - 2.0 * center + right
    if abs(denominator) < 1e-12:
        return float(index), float(center)
    delta = 0.5 * (left - right) / denominator
    delta = float(np.clip(delta, -0.5, 0.5))
    return float(index + delta), float(center - 0.25 * (left - right) * delta)


def _local_peaks(values: np.ndarray, minimum: float = 0.0) -> list[int]:
    if len(values) < 3:
        return [int(np.argmax(values))]
    peaks = np.flatnonzero(
        (values[1:-1] >= values[:-2])
        & (values[1:-1] > values[2:])
        & (values[1:-1] >= minimum)
    )
    return [int(index + 1) for index in peaks]


def _harmonic_profile(
    power_spectrum: np.ndarray, sample_rate: int, f0_hz: float
) -> tuple[float, float, float]:
    """Return comb strength, harmonic coverage and explained energy fraction."""

    if f0_hz <= 0:
        return 0.0, 0.0, 0.0
    bin_hz = sample_rate / (2.0 * (len(power_spectrum) - 1))
    nyquist = sample_rate / 2.0
    max_harmonic = min(40, int((min(nyquist * 0.92, 6000.0)) / f0_hz))
    if max_harmonic < 1:
        return 0.0, 0.0, 0.0
    total_lo = max(1, int(50.0 / bin_hz))
    total_hi = min(len(power_spectrum) - 1, int(min(nyquist * 0.95, 6000.0) / bin_hz))
    total = float(np.sum(power_spectrum[total_lo:total_hi])) + 1e-12

    values: list[float] = []
    for harmonic in range(1, max_harmonic + 1):
        center = harmonic * f0_hz / bin_hz
        if center >= len(power_spectrum) - 1:
            break
        width = max(1, int(round(0.018 * f0_hz / bin_hz)))
        lo = max(0, int(round(center)) - width)
        hi = min(len(power_spectrum), int(round(center)) + width + 1)
        values.append(float(np.max(power_spectrum[lo:hi])))
    if not values:
        return 0.0, 0.0, 0.0
    array = np.asarray(values, dtype=np.float64)
    maximum = float(np.max(array)) + 1e-12
    relative = np.clip(array / maximum, 0.0, 1.0)
    coverage = float(np.mean(relative >= 0.025))
    mean_relative = float(np.mean(relative))
    explained = float(np.minimum(np.sum(array) / total, 1.0))
    comb = 0.55 * coverage + 0.35 * mean_relative + 0.10 * explained
    return float(np.clip(comb, 0.0, 1.0)), coverage, explained


def _autocorrelation_candidate(
    acf: np.ndarray,
    power_spectrum: np.ndarray,
    sample_rate: int,
    config: Config,
    lag_min: int,
    lag_max: int,
) -> PitchCandidate | None:
    segment = acf[lag_min : lag_max + 1]
    if len(segment) < 3 or not np.all(np.isfinite(segment)):
        return None
    peaks = _local_peaks(segment, minimum=max(0.05, float(np.max(segment)) * 0.25))
    if not peaks:
        peaks = [int(np.argmax(segment))]
    candidates: list[PitchCandidate] = []
    for peak_index in peaks:
        interpolated, peak_value = _parabolic_peak(segment, peak_index)
        lag = lag_min + interpolated
        if lag <= 0:
            continue
        f0 = sample_rate / lag
        if not config.fmin_hz * 0.96 <= f0 <= config.fmax_hz * 1.04:
            continue
        harmonic, coverage, fraction = _harmonic_profile(power_spectrum, sample_rate, f0)
        periodicity = float(np.clip(peak_value, 0.0, 1.0))
        quality = 0.52 * periodicity + 0.48 * harmonic
        candidates.append(
            PitchCandidate(
                method="autocorrelation",
                f0_hz=float(f0),
                periodicity=periodicity,
                harmonic_score=harmonic,
                harmonic_coverage=coverage,
                harmonic_fraction=fraction,
                quality=float(quality),
            )
        )
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate.quality)


def _cepstrum_candidate(
    cepstrum: np.ndarray,
    power_spectrum: np.ndarray,
    sample_rate: int,
    lag_min: int,
    lag_max: int,
) -> PitchCandidate | None:
    segment = cepstrum[lag_min : lag_max + 1]
    if len(segment) < 3 or not np.all(np.isfinite(segment)):
        return None
    minimum = float(np.percentile(segment, 20.0))
    maximum = float(np.max(segment))
    scale = max(maximum - minimum, 1e-9)
    peaks = _local_peaks(segment, minimum=minimum)
    if not peaks:
        peaks = [int(np.argmax(segment))]
    candidates: list[PitchCandidate] = []
    for peak_index in peaks:
        interpolated, peak_value = _parabolic_peak(segment, peak_index)
        lag = lag_min + interpolated
        if lag <= 0:
            continue
        f0 = sample_rate / lag
        harmonic, coverage, fraction = _harmonic_profile(power_spectrum, sample_rate, f0)
        prominence = float(np.clip((peak_value - minimum) / scale, 0.0, 1.0))
        quality = 0.46 * prominence + 0.54 * harmonic
        candidates.append(
            PitchCandidate(
                method="cepstrum",
                f0_hz=float(f0),
                periodicity=prominence,
                harmonic_score=harmonic,
                harmonic_coverage=coverage,
                harmonic_fraction=fraction,
                quality=float(quality),
            )
        )
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate.quality)


def _select_candidate(
    ac: PitchCandidate | None,
    cep: PitchCandidate | None,
    config: Config,
) -> tuple[PitchCandidate | None, str]:
    if ac is None and cep is None:
        return None, "no_periodic_candidate"
    if ac is None:
        return cep, "cepstrum_only_candidate"
    if cep is None:
        return ac, "autocorrelation_only_candidate"

    ac_adjusted = ac.quality
    cep_adjusted = cep.quality
    if ac.f0_hz < config.low_band_split_hz:
        ac_adjusted += 0.055
    if cep.f0_hz > config.high_band_split_hz:
        cep_adjusted += 0.055
    difference_semitones = abs(12.0 * math.log2(ac.f0_hz / cep.f0_hz))
    if difference_semitones <= 0.65:
        if ac.f0_hz < config.low_band_split_hz:
            return ac, "methods_agree_low_band_autocorrelation"
        if cep.f0_hz > config.high_band_split_hz:
            return cep, "methods_agree_high_band_cepstrum"
        if ac_adjusted >= cep_adjusted:
            return ac, "methods_agree_higher_autocorrelation_quality"
        return cep, "methods_agree_higher_cepstrum_quality"
    if ac_adjusted >= cep_adjusted:
        return ac, "methods_disagree_autocorrelation_harmonic_profile"
    return cep, "methods_disagree_cepstrum_harmonic_profile"


def _candidate_fields(candidate: PitchCandidate | None) -> dict[str, Any]:
    if candidate is None:
        return {
            "f0_hz": None,
            "quality": None,
            "periodicity": None,
            "harmonic_score": None,
            "harmonic_coverage": None,
            "harmonic_fraction": None,
        }
    return {
        "f0_hz": round(candidate.f0_hz, 4),
        "quality": round(candidate.quality, 6),
        "periodicity": round(candidate.periodicity, 6),
        "harmonic_score": round(candidate.harmonic_score, 6),
        "harmonic_coverage": round(candidate.harmonic_coverage, 6),
        "harmonic_fraction": round(candidate.harmonic_fraction, 6),
    }


def estimate_f0(framed: FramedAudio, config: Config) -> list[dict[str, Any]]:
    window = hann_window(framed.frame_length)
    sample_rate = framed.audio.sample_rate
    lag_min = max(2, int(math.floor(sample_rate / config.fmax_hz)))
    lag_max = min(framed.frame_length - 2, int(math.ceil(sample_rate / config.fmin_hz)))
    records: list[dict[str, Any]] = []
    for index, (frame, time_s, energy_db, zcr) in enumerate(
        zip(framed.frames, framed.times, framed.energy_db, framed.zero_crossing)
    ):
        centered = np.asarray(frame, dtype=np.float64) - float(np.mean(frame))
        windowed = centered * window
        spectrum = np.fft.rfft(windowed)
        power_spectrum = np.abs(spectrum) ** 2
        analysis_lo = max(1, int(50.0 / (sample_rate / framed.frame_length)))
        analysis_hi = min(
            len(power_spectrum) - 1,
            int(min(sample_rate * 0.45, 6000.0) / (sample_rate / framed.frame_length)),
        )
        analysis_power = power_spectrum[analysis_lo:analysis_hi] + 1e-18
        if len(analysis_power):
            flatness = float(
                np.exp(np.mean(np.log(analysis_power)))
                / (np.mean(analysis_power) + 1e-18)
            )
        else:
            flatness = 1.0
        acf = np.fft.irfft(power_spectrum, n=framed.frame_length)
        if acf[0] > 1e-12:
            acf = acf / acf[0]
        else:
            acf = np.zeros_like(acf)
        log_magnitude = np.log(np.abs(spectrum) + 1e-10)
        cepstrum = np.fft.irfft(log_magnitude, n=framed.frame_length)

        ac_candidate = _autocorrelation_candidate(
            acf, power_spectrum, sample_rate, config, lag_min, lag_max
        )
        cep_candidate = _cepstrum_candidate(
            cepstrum, power_spectrum, sample_rate, lag_min, lag_max
        )
        selected, reason = _select_candidate(ac_candidate, cep_candidate, config)
        if selected is None:
            confidence = 0.0
        else:
            # Autocorrelation periodicity is the reliable voiced/unvoiced
            # evidence. Cepstral prominence alone is high for many noise
            # frames, so it may select the pitch algorithm but must not decide
            # voicing by itself.
            periodicity_evidence = (
                ac_candidate.periodicity if ac_candidate is not None else 0.0
            )
            harmonic_evidence = selected.harmonic_score
            log_ratio = math.log10(0.30 / max(flatness, 1e-9)) / math.log10(0.30 / 0.002)
            tonality = float(np.clip(log_ratio, 0.0, 1.0))
            confidence = float(
                np.clip(
                    0.72 * periodicity_evidence
                    + 0.20 * harmonic_evidence
                    + 0.08 * tonality,
                    0.0,
                    1.0,
                )
            )
        if energy_db < config.silence_dbfs:
            confidence = min(confidence, max(0.0, 0.45 * config.voicing_threshold))
        voiced = bool(energy_db >= config.silence_dbfs and confidence >= config.voicing_threshold)
        final_f0 = selected.f0_hz if selected is not None and voiced else None
        record: dict[str, Any] = {
            "index": index,
            "timestamp_s": round(float(time_s), 6),
            "f0_hz": round(final_f0, 4) if final_f0 is not None else None,
            "midi": round(hz_to_midi(final_f0), 4)
            if final_f0 is not None and hz_to_midi(final_f0) is not None
            else None,
            "voiced": voiced,
            "voicing_confidence": round(confidence, 6),
            "method": selected.method if selected is not None else None,
            "selection_reason": reason,
            "energy_dbfs": round(float(energy_db), 4),
            "zero_crossing_rate": round(float(zcr), 6),
            "spectral_flatness": round(float(flatness), 8),
            "acf_candidate": _candidate_fields(ac_candidate),
            "cepstrum_candidate": _candidate_fields(cep_candidate),
        }
        records.append(record)
    return records


def _nanmedian(values: np.ndarray) -> float:
    finite = values[np.isfinite(values)]
    if len(finite) == 0:
        return float("nan")
    return float(np.median(finite))


def smooth_and_correct_pitch(
    frames: list[dict[str, Any]], config: Config
) -> list[dict[str, Any]]:
    """Octave-repair and adaptive median smoothing without losing raw observations."""

    n = len(frames)
    raw_midi = np.asarray(
        [frame["midi"] if frame["midi"] is not None else np.nan for frame in frames],
        dtype=np.float64,
    )
    corrected = raw_midi.copy()
    octave_flags = np.zeros(n, dtype=bool)

    for index in range(n):
        value = raw_midi[index]
        if not math.isfinite(value):
            continue
        previous_values: list[float] = []
        next_values: list[float] = []
        cursor = index - 1
        while cursor >= 0 and len(previous_values) < 3:
            if math.isfinite(raw_midi[cursor]):
                previous_values.append(float(raw_midi[cursor]))
            cursor -= 1
        cursor = index + 1
        while cursor < n and len(next_values) < 3:
            if math.isfinite(raw_midi[cursor]):
                next_values.append(float(raw_midi[cursor]))
            cursor += 1
        # Isolated octave outliers have supporting pitch on both sides. This
        # avoids flattening a real, sustained octave leap at a note boundary.
        if len(previous_values) < 2 or len(next_values) < 2:
            continue
        context = 0.5 * (float(np.median(previous_values)) + float(np.median(next_values)))
        difference = value - context
        if 9.5 <= abs(difference) <= 14.5:
            shift = -12.0 if difference > 0 else 12.0
            corrected[index] = value + shift
            octave_flags[index] = True

    smoothing_windows = np.zeros(n, dtype=np.int64)
    smoothed = corrected.copy()
    finite_indices = np.flatnonzero(np.isfinite(corrected))
    for index in range(n):
        if not math.isfinite(corrected[index]):
            continue
        rate_values: list[float] = []
        for neighbor in range(max(0, index - 3), min(n, index + 4) - 1):
            if (
                neighbor >= 0
                and neighbor + 1 < n
                and math.isfinite(corrected[neighbor])
                and math.isfinite(corrected[neighbor + 1])
            ):
                rate_values.append(abs(float(corrected[neighbor + 1] - corrected[neighbor])))
        rate = float(np.median(rate_values)) if rate_values else 0.0
        if rate >= 0.22:
            window = 3
        elif rate >= 0.09:
            window = 5
        elif rate >= 0.035:
            window = 7
        else:
            window = 9
        smoothing_windows[index] = window
        half = window // 2
        lo = max(0, index - half)
        hi = min(n, index + half + 1)
        smoothed[index] = _nanmedian(corrected[lo:hi])

    for index, frame in enumerate(frames):
        raw_value = raw_midi[index]
        corrected_value = corrected[index]
        smoothed_value = smoothed[index]
        frame["raw_f0_hz"] = frame["f0_hz"]
        frame["raw_midi"] = frame["midi"]
        frame["octave_corrected"] = bool(octave_flags[index])
        frame["octave_corrected_f0_hz"] = (
            round(midi_to_hz(corrected_value), 4) if math.isfinite(corrected_value) else None
        )
        frame["octave_corrected_midi"] = (
            round(float(corrected_value), 4) if math.isfinite(corrected_value) else None
        )
        frame["smoothing_window"] = int(smoothing_windows[index])
        if math.isfinite(smoothed_value):
            frame["f0_hz"] = round(midi_to_hz(smoothed_value), 4)
            frame["midi"] = round(float(smoothed_value), 4)
        else:
            frame["f0_hz"] = None
            frame["midi"] = None
        frame["pitch_rate_semitones_per_frame"] = (
            round(abs(float(smoothed[index] - smoothed[index - 1])), 6)
            if index > 0 and math.isfinite(smoothed[index]) and math.isfinite(smoothed[index - 1])
            else 0.0
        )
    return frames

