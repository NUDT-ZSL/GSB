"""Objective audio metrics used by the demo report and consistency checks."""

from __future__ import annotations

import numpy as np

from .pcm import pcm16_to_float


def rms_db(pcm: np.ndarray) -> float:
    x = pcm16_to_float(pcm)
    energy = float(np.mean(np.square(x, dtype=np.float64)))
    if energy <= 0.0:
        return -np.inf
    return float(20.0 * np.log10(np.sqrt(energy)))


def peak_abs(pcm: np.ndarray) -> int:
    return int(np.max(np.abs(pcm))) if pcm.size else 0


def mean_abs(pcm: np.ndarray) -> float:
    return float(np.mean(np.abs(pcm16_to_float(pcm))))


def rms_difference_db(pcm_a: np.ndarray, pcm_b: np.ndarray) -> float:
    """RMS level of the difference signal, in dBFS (inf when identical)."""

    a = pcm16_to_float(pcm_a).astype(np.float64)
    b = pcm16_to_float(pcm_b).astype(np.float64)
    if a.shape != b.shape:
        raise ValueError("waveform shapes differ")
    diff = a - b
    energy = float(np.mean(np.square(diff)))
    if energy == 0.0:
        return -np.inf
    return float(20.0 * np.log10(np.sqrt(energy)))


def node_metrics(pcm: np.ndarray, sample_rate: int) -> dict:
    return {
        "samples": int(pcm.size),
        "duration_seconds": round(pcm.size / float(sample_rate), 6),
        "rms_dbfs": round(rms_db(pcm), 4),
        "peak_abs_int16": peak_abs(pcm),
        "mean_abs_float": round(mean_abs(pcm), 8),
    }
