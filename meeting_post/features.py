"""Deterministic short-time features aligned to the sample/frame grid."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import MeetingConfig, canonical_json_bytes
from .synthesis import SynthesisResult


@dataclass
class FeatureResult:
    frames: list[dict[str, Any]]
    matrix: np.ndarray
    embeddings: np.ndarray
    feature_bytes: bytes
    feature_sha256: str


def _band_energy(samples: np.ndarray, sample_rate: int) -> list[float]:
    window = np.hanning(samples.size).astype(np.float64)
    spectrum = np.fft.rfft(samples * window)
    power = np.abs(spectrum) ** 2
    frequencies = np.fft.rfftfreq(samples.size, 1.0 / sample_rate)
    edges = np.geomspace(80.0, min(4000.0, sample_rate / 2.0), 9)
    bands: list[float] = []
    for low, high in zip(edges[:-1], edges[1:]):
        mask = (frequencies >= low) & (frequencies < high)
        bands.append(float(np.sum(power[mask])))
    return bands


def _normalized_embedding(bands: list[float]) -> np.ndarray:
    vector = np.log1p(np.asarray(bands, dtype=np.float64) * 1.0e7)
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        return np.zeros_like(vector)
    return vector / norm


def compute_features(config: MeetingConfig, session: SynthesisResult) -> FeatureResult:
    pcm = session.pcm.astype(np.float64) / 32768.0
    embedding_dtype = np.float32 if config.precision == "float32" else np.float64
    frames: list[dict[str, Any]] = []
    embeddings: list[np.ndarray] = []
    matrix_rows: list[list[float]] = []
    expected = session.frame_count
    computed = 0
    for frame_index, start in enumerate(
        range(0, session.total_samples - config.frame_samples + 1, config.hop_samples)
    ):
        end = start + config.frame_samples
        samples = pcm[start:end]
        if samples.size == 0:
            break
        if config.normalization == "rms":
            energy = float(np.sqrt(np.mean(samples * samples)))
        elif config.normalization == "peak":
            energy = float(np.max(np.abs(samples)))
        else:
            energy = float(np.mean(np.abs(samples)))
        if samples.size > 1:
            zcr = float(np.mean(samples[:-1] * samples[1:] < 0.0))
        else:
            zcr = 0.0
        bands = _band_energy(samples, session.sample_rate)
        embedding = _normalized_embedding(bands).astype(embedding_dtype)
        embeddings.append(embedding)
        weights = np.abs(samples)
        frequencies = np.fft.rfftfreq(samples.size, 1.0 / session.sample_rate)
        spectrum = np.abs(np.fft.rfft(samples * np.hanning(samples.size)))
        spectral_centroid = float(
            np.sum(frequencies * spectrum) / (np.sum(spectrum) + 1e-15)
        )
        frames.append(
            {
                "frame_index": frame_index,
                "sample_start": start,
                "sample_end": end,
                "energy": energy,
                "zcr": zcr,
                "spectral_centroid_hz": spectral_centroid,
                "band_energy": bands,
            }
        )
        matrix_rows.append([energy, zcr, spectral_centroid, *bands])
        computed += 1
        if computed >= expected:
            break
    if computed != expected:
        raise ValueError(
            f"feature frame count {computed} does not match truth frame count {expected}"
        )
    matrix = np.asarray(matrix_rows, dtype=np.float64)
    embedding_matrix = np.vstack(embeddings) if embeddings else np.zeros((0, 8))
    feature_bytes = canonical_json_bytes(frames)
    return FeatureResult(
        frames=frames,
        matrix=matrix,
        embeddings=embedding_matrix,
        feature_bytes=feature_bytes,
        feature_sha256=hashlib.sha256(feature_bytes).hexdigest(),
    )


def registration_embeddings(
    config: MeetingConfig,
    session: SynthesisResult,
    features: FeatureResult,
) -> dict[int, dict[str, Any]]:
    """Estimate one deterministic embedding mean/std per declared speaker."""
    profiles: dict[int, dict[str, Any]] = {}
    embedding_dtype = np.float32 if config.precision == "float32" else np.float64
    for speaker_id in sorted(session.registrations):
        pcm = session.registrations[speaker_id].astype(np.float64) / 32768.0
        frame_vectors: list[np.ndarray] = []
        for start in range(0, max(1, pcm.size - config.frame_samples + 1), config.hop_samples):
            end = start + config.frame_samples
            samples = pcm[start:end]
            if samples.size < config.frame_samples:
                break
            rms = float(np.sqrt(np.mean(samples * samples)))
            if rms < 0.004:
                continue
            frame_vectors.append(
                _normalized_embedding(_band_energy(samples, config.sample_rate)).astype(
                    embedding_dtype
                )
            )
        if not frame_vectors:
            raise ValueError(f"registration clip for speaker {speaker_id} has no voiced frames")
        stacked = np.vstack(frame_vectors)
        mean = np.mean(stacked, axis=0, dtype=embedding_dtype)
        norm = float(np.linalg.norm(mean))
        mean = (mean / norm if norm else mean).astype(embedding_dtype)
        profiles[speaker_id] = {
            "mean": mean,
            "std": np.std(stacked, axis=0).astype(embedding_dtype),
            "frames": int(stacked.shape[0]),
        }
    return profiles
