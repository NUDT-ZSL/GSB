"""Shared deterministic speaker-tone synthesis (generator and workbench)."""
from __future__ import annotations

import numpy as np

SPEAKERS = {
    "S1": {"base": 180.0, "partials": (1.0, 2.0, 3.1, 4.7), "gains": (1.0, 0.55, 0.35, 0.2)},
    "S2": {"base": 245.0, "partials": (1.0, 2.2, 3.4, 5.1), "gains": (1.0, 0.5, 0.3, 0.18)},
    "S3": {"base": 320.0, "partials": (1.0, 2.5, 5.3, 7.8), "gains": (1.0, 0.72, 0.48, 0.34)},
}

SAMPLE_RATE = 16000
_SEED = 20240517


def speaker_tone(speaker: str, duration_ms: float, stream_key: int) -> np.ndarray:
    if speaker not in SPEAKERS:
        raise KeyError(f"unknown synthetic speaker: {speaker}")
    n = max(1, int(round(SAMPLE_RATE * duration_ms / 1000.0)))
    params = SPEAKERS[speaker]
    rng = np.random.default_rng(_SEED + stream_key)
    t = np.arange(n, dtype=np.float64) / SAMPLE_RATE
    tone = np.zeros(n, dtype=np.float64)
    for partial, gain in zip(params["partials"], params["gains"]):
        freq = params["base"] * partial
        vibrato = 1.0 + 0.004 * np.sin(2.0 * np.pi * (4.0 + partial) * t)
        phase = 2.0 * np.pi * freq * np.cumsum(vibrato) / SAMPLE_RATE
        tone += gain * np.sin(phase + rng.uniform(0.0, 2.0 * np.pi))
    ramp = max(1, n // 16)
    envelope = np.ones(n, dtype=np.float64)
    envelope[:ramp] = np.linspace(0.0, 1.0, ramp)
    envelope[-ramp:] = np.linspace(1.0, 0.0, ramp)
    tone *= envelope
    peak = np.max(np.abs(tone)) or 1.0
    return (0.35 * tone / peak).astype(np.float64)
