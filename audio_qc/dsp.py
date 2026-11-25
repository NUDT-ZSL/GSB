"""Deterministic PCM framing, energy, and Haar wavelet statistics."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import numpy as np

SAMPLE_RATE = 16_000
FRAME_LENGTH = 320  # 20 ms at 16 kHz
FRAME_HOP = 160  # 10 ms at 16 kHz
WAVELET_LEVELS = 3
DB_FLOOR = -120.0
DSP_VERSION = "haar3-energy-v1"


def read_pcm_s16(path: str | Path) -> tuple[np.ndarray, bytes]:
    raw = Path(path).read_bytes()
    pcm = decode_pcm_s16(raw)
    return pcm, raw


def decode_pcm_s16(raw: bytes) -> np.ndarray:
    if len(raw) % 2:
        raise ValueError("signed 16-bit PCM byte length must be even")
    return np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0


def encode_pcm_s16(pcm: np.ndarray) -> bytes:
    clipped = np.clip(np.asarray(pcm, dtype=np.float64), -1.0, 32767.0 / 32768.0)
    return np.round(clipped * 32768.0).astype("<i2").tobytes()


def write_pcm_s16(path: str | Path, pcm: np.ndarray) -> str:
    data = encode_pcm_s16(pcm)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def frame_starts(sample_count: int, frame_length: int = FRAME_LENGTH, frame_hop: int = FRAME_HOP) -> np.ndarray:
    """Return exact sample starts for all complete frames."""

    if frame_length <= 0 or frame_hop <= 0:
        raise ValueError("frame_length and frame_hop must be positive")
    if sample_count < frame_length:
        return np.empty(0, dtype=np.int64)
    last_start = sample_count - frame_length
    count = int(last_start // frame_hop) + 1
    return (np.arange(count, dtype=np.int64) * np.int64(frame_hop)).astype(np.int64)


def _db_from_rms(rms: float) -> float:
    if not np.isfinite(rms) or rms <= 0.0:
        return DB_FLOOR
    return float(max(DB_FLOOR, 20.0 * np.log10(rms)))


def energy_statistics(frame: np.ndarray) -> dict[str, float]:
    samples = np.asarray(frame, dtype=np.float64)
    energy = float(np.sum(samples * samples, dtype=np.float64))
    rms = float(np.sqrt(energy / float(samples.size), dtype=np.float64)) if samples.size else 0.0
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    return {
        "energy": energy,
        "rms": rms,
        "rms_db": _db_from_rms(rms),
        "peak": peak,
        "mean_abs": float(np.mean(np.abs(samples), dtype=np.float64)) if samples.size else 0.0,
    }


def haar_once(samples: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """One normalized Haar analysis step.

    The sqrt(2) normalization preserves squared energy between the pair domain
    and approximation/detail coefficients.
    """

    usable = samples.size - (samples.size % 2)
    pairs = np.asarray(samples[:usable], dtype=np.float64).reshape(-1, 2)
    approximation = (pairs[:, 0] + pairs[:, 1]) / np.sqrt(2.0)
    detail = (pairs[:, 0] - pairs[:, 1]) / np.sqrt(2.0)
    return approximation, detail


def wavelet_subbands(frame: np.ndarray, levels: int = WAVELET_LEVELS) -> list[dict[str, Any]]:
    work = np.asarray(frame, dtype=np.float64)
    details: list[tuple[str, np.ndarray]] = []
    for level in range(1, levels + 1):
        approximation, detail = haar_once(work)
        details.append((f"d{level}", detail))
        work = approximation

    bands: list[dict[str, Any]] = []
    total_energy = 0.0
    for name, coefficients in details:
        energy = float(np.sum(coefficients * coefficients, dtype=np.float64))
        rms = float(np.sqrt(np.mean(coefficients * coefficients, dtype=np.float64))) if coefficients.size else 0.0
        total_energy += energy
        bands.append(
            {
                "band": name,
                "coefficient_count": int(coefficients.size),
                "energy": energy,
                "rms": rms,
                "rms_db": _db_from_rms(rms),
                "mean_abs": float(np.mean(np.abs(coefficients), dtype=np.float64)) if coefficients.size else 0.0,
                "energy_fraction": 0.0,
            }
        )

    low_energy = float(np.sum(work * work, dtype=np.float64))
    low_rms = float(np.sqrt(np.mean(work * work, dtype=np.float64))) if work.size else 0.0
    total_energy += low_energy
    bands.append(
        {
            "band": f"a{levels}",
            "coefficient_count": int(work.size),
            "energy": low_energy,
            "rms": low_rms,
            "rms_db": _db_from_rms(low_rms),
            "mean_abs": float(np.mean(np.abs(work), dtype=np.float64)) if work.size else 0.0,
            "energy_fraction": 0.0,
        }
    )

    for band in bands:
        band["energy_fraction"] = float(band["energy"] / total_energy) if total_energy > 0.0 else 0.0
    return bands


def analyze_frame(
    pcm: np.ndarray,
    frame_index: int,
    start_sample: int,
    frame_length: int = FRAME_LENGTH,
    wavelet_levels: int = WAVELET_LEVELS,
) -> dict[str, Any]:
    end_sample = int(start_sample) + int(frame_length)
    frame = np.asarray(pcm[int(start_sample):end_sample], dtype=np.float64)
    if frame.size != frame_length:
        raise ValueError(f"frame {frame_index} is incomplete: {frame.size}/{frame_length}")
    energy = energy_statistics(frame)
    bands = wavelet_subbands(frame, wavelet_levels)
    return {
        "frame_index": int(frame_index),
        "start_sample": int(start_sample),
        "end_sample": end_sample,
        "sample_count": int(frame_length),
        "energy": energy,
        "subbands": bands,
        "dsp_version": DSP_VERSION,
    }


def iter_frame_jobs(
    pcm: np.ndarray, frame_length: int = FRAME_LENGTH, frame_hop: int = FRAME_HOP
) -> Iterable[tuple[int, int]]:
    starts = frame_starts(pcm.size, frame_length, frame_hop)
    for index, start in enumerate(starts):
        yield index, int(start)


def estimate_envelope_serial(
    pcm: np.ndarray,
    frame_length: int = FRAME_LENGTH,
    frame_hop: int = FRAME_HOP,
    wavelet_levels: int = WAVELET_LEVELS,
) -> list[dict[str, Any]]:
    return [
        analyze_frame(pcm, index, start, frame_length, wavelet_levels)
        for index, start in iter_frame_jobs(pcm, frame_length, frame_hop)
    ]


def estimate_envelope_oneshot(
    pcm: np.ndarray,
    frame_length: int = FRAME_LENGTH,
    frame_hop: int = FRAME_HOP,
    wavelet_levels: int = WAVELET_LEVELS,
) -> list[dict[str, Any]]:
    """Whole-buffer implementation used by the consistency self-check.

    Frame extraction is vectorized before the same deterministic statistics are
    evaluated.  It intentionally has a different call path from the chunk worker.
    """

    starts = frame_starts(pcm.size, frame_length, frame_hop)
    if starts.size == 0:
        return []
    windows = np.lib.stride_tricks.sliding_window_view(np.asarray(pcm, dtype=np.float64), frame_length)
    blocks: list[dict[str, Any]] = []
    for index, start in enumerate(starts):
        frame = np.asarray(windows[int(start)], dtype=np.float64)
        energy = energy_statistics(frame)
        bands = wavelet_subbands(frame, wavelet_levels)
        blocks.append(
            {
                "frame_index": int(index),
                "start_sample": int(start),
                "end_sample": int(start + frame_length),
                "sample_count": int(frame_length),
                "energy": energy,
                "subbands": bands,
                "dsp_version": DSP_VERSION,
            }
        )
    return blocks
