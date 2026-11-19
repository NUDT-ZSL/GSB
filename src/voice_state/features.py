"""Exact sample-grid framing plus deterministic acoustic descriptors."""

from __future__ import annotations

import math

import numpy as np

from .config import AnalysisConfig
from .errors import BusinessError, E_CONFIG_INVALID


FEATURE_DIMENSION = 8


def frame_count(num_samples: int, frame_samples: int, hop_samples: int) -> int:
    if num_samples < frame_samples:
        return 0
    return 1 + (num_samples - frame_samples) // hop_samples


def extract_energy_zcr(
    samples: np.ndarray,
    config: AnalysisConfig,
) -> list[dict[str, float | int]]:
    frame_samples = config.frame_samples()
    hop_samples = config.hop_samples()
    if hop_samples > frame_samples:
        raise BusinessError(
            E_CONFIG_INVALID,
            "hop_ms must not exceed frame_ms",
            {"field": "hop_ms", "hop_samples": hop_samples, "frame_samples": frame_samples},
        )
    count = frame_count(int(samples.size), frame_samples, hop_samples)
    rows: list[dict[str, float | int]] = []
    signs = samples >= 0.0
    for index in range(count):
        start = index * hop_samples
        frame = samples[start : start + frame_samples]
        energy = float(np.mean(np.square(frame)))
        sign_slice = signs[start : start + frame_samples]
        changes = int(np.count_nonzero(sign_slice[1:] != sign_slice[:-1]))
        zcr = changes / float(max(1, frame_samples - 1))
        rows.append(
            {
                "frame_index": index,
                "energy": round(energy, config.feature_places),
                "zcr": round(zcr, config.feature_places),
            }
        )
    return rows


def _window(frame_samples: int) -> np.ndarray:
    return np.hanning(frame_samples)


def spectral_descriptors(samples: np.ndarray, config: AnalysisConfig) -> np.ndarray:
    """Return [log_energy, zcr, centroid, flatness, four band ratios] per frame."""
    frame_samples = config.frame_samples()
    hop_samples = config.hop_samples()
    count = frame_count(int(samples.size), frame_samples, hop_samples)
    result = np.zeros((count, FEATURE_DIMENSION), dtype=np.float64)
    if count == 0:
        return result
    window = _window(frame_samples)
    frequencies = np.fft.rfftfreq(frame_samples, d=1.0 / config.sample_rate)
    band_edges = (
        (0.0, 500.0),
        (500.0, 1500.0),
        (1500.0, 3000.0),
        (3000.0, 6000.0),
    )
    band_masks = [
        (frequencies >= low) & (frequencies < high) for low, high in band_edges
    ]
    for index in range(count):
        start = index * hop_samples
        frame = samples[start : start + frame_samples]
        energy = float(np.mean(np.square(frame)))
        if energy < 1e-12:
            result[index] = 0.0
            continue
        zcr = float(np.count_nonzero((frame[:-1] >= 0.0) != (frame[1:] >= 0.0))) / max(
            1, frame_samples - 1
        )
        spectrum = np.abs(np.fft.rfft(frame * window))
        power = np.square(spectrum)
        total = float(np.sum(power))
        if total <= 0.0:
            result[index] = [math.log1p(energy * 1_000_000.0), zcr, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
            continue
        centroid = float(np.sum(frequencies * power) / total) / (config.sample_rate / 2.0)
        flatness = float(np.exp(np.mean(np.log(power + 1e-18))) / np.mean(power + 1e-18))
        band_ratios = [float(np.sum(power[mask]) / total) for mask in band_masks]
        values = [
            math.log1p(energy * 1_000_000.0),
            zcr,
            centroid,
            min(flatness, 1.0),
            *band_ratios,
        ]
        result[index] = values
    return np.round(result, config.feature_places)


def compute_embedding_statistics(descriptors: np.ndarray, config: AnalysisConfig) -> dict:
    active = descriptors[descriptors[:, 0] > 0.05]
    if active.shape[0] == 0:
        active = descriptors
    mean = np.mean(active, axis=0)
    std = np.std(active, axis=0, ddof=0)
    return {
        "mean": np.round(mean, config.feature_places).astype(np.float64).tolist(),
        "std": np.round(std, config.feature_places).astype(np.float64).tolist(),
        "frame_count": int(active.shape[0]),
    }


def speaker_distances(
    descriptor: np.ndarray,
    speaker_id: int,
    statistics: dict,
    config: AnalysisConfig,
) -> float:
    mean = np.asarray(statistics["mean"], dtype=np.float64)
    std = np.asarray(statistics["std"], dtype=np.float64)
    scale = np.maximum(std, 0.035 + 0.25 / math.sqrt(max(1, int(statistics["frame_count"]))))
    distance = float(np.sum(np.square((descriptor - mean) / scale)))
    return round(distance, config.score_places)


def assign_speaker_id(
    descriptor: np.ndarray,
    statistics: dict[int, dict],
    config: AnalysisConfig,
) -> int:
    """Find the nearest speaker; sorted IDs make exact ties deterministic."""
    best_id: int | None = None
    best_score = math.inf
    for speaker_id in sorted(statistics):
        score = speaker_distances(descriptor, speaker_id, statistics[speaker_id], config)
        if best_id is None or score < best_score:
            best_id = speaker_id
            best_score = score
    if best_id is None:
        raise BusinessError(E_CONFIG_INVALID, "Speaker statistics are empty", {"field": "registrations"})
    return best_id


def assign_speaker_sequence(
    descriptors: np.ndarray,
    statistics: dict[int, dict],
    config: AnalysisConfig,
) -> list[int]:
    labels: list[int] = []
    previous = sorted(statistics)[0]
    for descriptor in descriptors:
        if float(descriptor[0]) <= 0.05:
            labels.append(previous)
            continue
        speaker_id = assign_speaker_id(descriptor, statistics, config)
        labels.append(speaker_id)
        previous = speaker_id
    return labels


def contiguous_segments(labels: list[int], frame_ms: float) -> tuple[list[dict], list[dict]]:
    if not labels:
        return [], []
    segments: list[dict] = []
    transitions: list[dict] = []
    start = 0
    current = labels[0]
    for index in range(1, len(labels)):
        if labels[index] == current:
            continue
        segments.append(_segment_row(start, index - 1, current, frame_ms))
        transitions.append(
            {
                "frame_index": index,
                "time_ms": round(index * frame_ms, 6),
                "from_speaker_id": current,
                "to_speaker_id": labels[index],
            }
        )
        start = index
        current = labels[index]
    segments.append(_segment_row(start, len(labels) - 1, current, frame_ms))
    return segments, transitions


def _segment_row(start: int, end: int, speaker_id: int, frame_ms: float) -> dict:
    frame_count_value = end - start + 1
    return {
        "speaker_id": int(speaker_id),
        "start_frame": int(start),
        "end_frame": int(end),
        "start_ms": round(start * frame_ms, 6),
        "end_ms": round((end + 1) * frame_ms, 6),
        "frame_count": int(frame_count_value),
        "duration_ms": round(frame_count_value * frame_ms, 6),
    }

