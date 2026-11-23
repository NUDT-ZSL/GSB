"""Deterministic frame-level acoustic feature extraction."""

from __future__ import annotations

import math

import numpy as np

from .model import FeatureFrame, FrameTruth, Utterance
from .precision import NumericOps


def sample_bounds(
    sample_rate: int,
    frame_ms: float,
    frame_hop_ms: float,
) -> tuple[int, int]:
    frame_len = int(round(sample_rate * frame_ms / 1000.0))
    hop = int(round(sample_rate * frame_hop_ms / 1000.0))
    return frame_len, hop


def _frame_positions(
    sample_count: int,
    frame_len: int,
    hop: int,
    expected_frames: int | None = None,
) -> list[int]:
    if expected_frames is not None:
        positions = [index * hop for index in range(expected_frames)]
        if positions and positions[-1] + frame_len > sample_count:
            positions = [p for p in positions if p + frame_len <= sample_count]
        return positions
    if sample_count < frame_len:
        return []
    return list(range(0, sample_count - frame_len + 1, hop))


def _f0_candidate(frame: np.ndarray, sample_rate: int) -> tuple[float, float]:
    centered = frame - float(np.mean(frame))
    energy = float(np.dot(centered, centered))
    if energy <= 1e-12:
        return 0.0, 0.0
    autocorrelation = np.correlate(centered, centered, mode="full")[len(centered) - 1 :]
    if autocorrelation[0] <= 0:
        return 0.0, 0.0
    normalized = autocorrelation / autocorrelation[0]
    min_lag = max(1, int(math.floor(sample_rate / 420.0)))
    max_lag = min(len(normalized) - 2, int(math.ceil(sample_rate / 70.0)))
    if max_lag <= min_lag:
        return 0.0, 0.0
    segment = normalized[min_lag : max_lag + 1]
    best_offset = int(np.argmax(segment))
    lag = min_lag + best_offset
    confidence = float(max(0.0, min(1.0, segment[best_offset])))
    if confidence < 0.25:
        return 0.0, confidence
    f0 = float(sample_rate) / float(max(lag, 1))
    return f0, confidence


def _spectral_features(frame: np.ndarray, sample_rate: int) -> tuple[float, tuple[float, ...]]:
    if frame.size == 0:
        return 0.0, (0.0, 0.0, 0.0, 0.0)
    windowed = frame * np.hanning(frame.size)
    magnitude = np.abs(np.fft.rfft(windowed))
    freqs = np.fft.rfftfreq(frame.size, d=1.0 / sample_rate)
    total = float(np.sum(magnitude))
    if total <= 1e-12:
        return 0.0, (0.0, 0.0, 0.0, 0.0)
    centroid = float(np.sum(freqs * magnitude) / total)
    bands = (
        (0.0, 500.0),
        (500.0, 1200.0),
        (1200.0, 2600.0),
        (2600.0, 5000.0),
    )
    values: list[float] = []
    for low, high in bands:
        mask = (freqs >= low) & (freqs < high)
        values.append(float(np.sum(magnitude[mask])) / total if np.any(mask) else 0.0)
    return centroid, tuple(values)


def extract_utterance_features(
    utterance: Utterance,
    frame_ms: float,
    frame_hop_ms: float,
    ops: NumericOps,
    use_truth_frame_count: bool = True,
) -> tuple[FeatureFrame, ...]:
    return extract_pcm_features(
        pcm=utterance.pcm,
        sample_rate=utterance.sample_rate,
        frame_ms=frame_ms,
        frame_hop_ms=frame_hop_ms,
        ops=ops,
        utterance_id=utterance.utterance_id,
        speaker_id=utterance.speaker_id,
        expected_frames=len(utterance.truth) if use_truth_frame_count else None,
    )


def extract_pcm_features(
    pcm: np.ndarray,
    sample_rate: int,
    frame_ms: float,
    frame_hop_ms: float,
    ops: NumericOps,
    utterance_id: str,
    speaker_id: int,
    expected_frames: int | None = None,
) -> tuple[FeatureFrame, ...]:
    pcm_float = np.asarray(pcm, dtype=np.float64) / 32768.0
    frame_len, hop = sample_bounds(sample_rate, frame_ms, frame_hop_ms)
    expected = expected_frames
    positions = _frame_positions(pcm_float.size, frame_len, hop, expected)
    frames: list[FeatureFrame] = []
    for index, start in enumerate(positions):
        end = start + frame_len
        raw = pcm_float[start:end]
        energy = float(np.mean(raw * raw)) if raw.size else 0.0
        signs = np.signbit(raw)
        zcr = float(np.mean(signs[1:] != signs[:-1])) if raw.size > 1 else 0.0
        f0, _confidence = _f0_candidate(raw, sample_rate)
        centroid, bands = _spectral_features(raw, sample_rate)
        log_energy = math.log1p(max(0.0, energy))
        log_f0 = math.log1p(max(0.0, f0))
        timbre = (
            log_energy,
            zcr,
            log_f0 / 8.0,
            centroid / 4000.0,
            *bands,
        )
        cast_timbre = ops.cast(timbre)
        frames.append(
            FeatureFrame(
                utterance_id=utterance_id,
                speaker_id=speaker_id,
                frame_index=index,
                start_sample=start,
                end_sample=end,
                energy=ops.cast_scalar(energy),
                zcr=ops.cast_scalar(zcr),
                f0_candidate=ops.cast_scalar(f0),
                log_energy=ops.cast_scalar(log_energy),
                centroid_hz=ops.cast_scalar(centroid),
                band_energies=tuple(float(v) for v in ops.cast(bands)),
                timbre_vector=tuple(float(v) for v in cast_timbre),
            )
        )
    return tuple(frames)


def extract_dataset_features(
    utterances: tuple[Utterance, ...],
    frame_ms: float,
    frame_hop_ms: float,
    ops: NumericOps,
) -> dict[str, tuple[FeatureFrame, ...]]:
    return {
        utterance.utterance_id: extract_utterance_features(
            utterance, frame_ms, frame_hop_ms, ops
        )
        for utterance in utterances
    }


def validate_frame_sets(
    frames_by_utterance: dict[str, tuple[FeatureFrame, ...]],
    truth_by_utterance: dict[str, tuple[FrameTruth, ...]],
    stage: str = "features.validate",
) -> None:
    for utterance_id, frames in frames_by_utterance.items():
        expected = [item.frame_index for item in truth_by_utterance[utterance_id]]
        actual = [frame.frame_index for frame in frames]
        if actual != expected:
            first = next(
                (
                    index
                    for index, (left, right) in enumerate(zip(actual, expected))
                    if left != right
                ),
                min(len(actual), len(expected)),
            )
            raise ValueError(
                f"{stage}: frame set mismatch for {utterance_id} at position {first}"
            )
