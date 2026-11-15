"""Deterministic synthetic piano sample generation.

No real recordings and no remote assets: the piano tone is an additive
harmonic model (MIDI note -> inharmonic partials, exponential decays) plus an
ADSR envelope and a small amount of fixed-seed broadband noise that the
denoise operation can work on. Output is always 48 kHz / 16-bit / mono.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from .pcm import float_to_pcm16
from .constants import SAMPLE_RATE

SYNTH_SEED = 48000


def midi_to_freq(note: int) -> float:
    return 440.0 * (2.0 ** ((note - 69) / 12.0))


def synthesize_piano(
    duration_seconds: float = 2.0,
    midi_note: int = 60,
    sample_rate: int = SAMPLE_RATE,
    noise_level: float = 0.01,
    seed: int = SYNTH_SEED,
) -> np.ndarray:
    """Return a deterministic mono piano tone as int16 little-endian PCM."""

    length = int(round(duration_seconds * sample_rate))
    t = np.arange(length, dtype=np.float64) / sample_rate
    fundamental = midi_to_freq(midi_note)

    tone = np.zeros(length, dtype=np.float64)
    # Partial index, relative amplitude and per-partial decay time (seconds).
    partials = [
        (1, 1.00, 1.6),
        (2, 0.55, 1.1),
        (3, 0.30, 0.7),
        (4, 0.16, 0.45),
        (5, 0.09, 0.30),
        (6, 0.05, 0.20),
    ]
    for harmonic, amplitude, decay in partials:
        # Slight inharmonicity makes the tone closer to a struck string.
        freq = fundamental * harmonic * np.sqrt(1.0 + 0.0004 * harmonic * harmonic)
        tone += amplitude * np.sin(2.0 * np.pi * freq * t) * np.exp(-t / decay)

    # ADSR-ish amplitude envelope (fast attack, natural release).
    attack = int(0.005 * sample_rate)
    envelope = np.ones(length, dtype=np.float64)
    if attack > 0:
        envelope[:attack] = np.linspace(0.0, 1.0, attack)
    release = int(0.12 * sample_rate)
    envelope[-release:] *= np.linspace(1.0, 0.0, release)
    tone *= envelope

    # Fixed-seed broadband noise stands in for recording background hiss.
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(length) * noise_level
    mixed = (tone * 0.6) + noise

    peak = float(np.max(np.abs(mixed))) or 1.0
    normalized = (mixed / peak * 0.85).astype(np.float32)
    return float_to_pcm16(normalized)


def write_wav(path: str | Path, pcm: np.ndarray, sample_rate: int = SAMPLE_RATE) -> Path:
    """Write int16 PCM as a 48 kHz mono 16-bit WAV file and return the path."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.asarray(pcm, dtype="<i2"), sample_rate, subtype="PCM_16")
    return path
