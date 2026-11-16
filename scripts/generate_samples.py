"""Deterministic local WAV sample generator for the VAD component.

All randomness comes from numpy ``default_rng`` with fixed per-sample seeds, so
re-running the script reproduces every sample bit-for-bit. Valid samples are
16 kHz / mono / 16-bit PCM. A few deliberately non-conforming files are also
written to exercise the audio-format validation layer.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 16_000
BASE_SEED = 20261117
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "input"

# Each entry: (file name, duration seconds, speech intervals in (start_s, end_s), pop times s)
SPEECH_PLAN = [
    ("sample_a_quiet.wav", 2.0, [], []),
    ("sample_b_two_speech.wav", 3.0, [(0.35, 0.95), (1.55, 2.35)], []),
    ("sample_c_gaps.wav", 3.2, [(0.30, 0.80), (0.90, 1.40), (1.90, 2.60)], []),
    ("sample_d_short.wav", 2.4, [(0.30, 1.50), (1.62, 2.00)], []),
    ("sample_e_nonfinite.wav", 1.8, [(0.25, 1.20)], []),
]


def _seed_for(name: str) -> int:
    return BASE_SEED + (sum(ord(ch) for ch in name) % 10000)


def _envelope(length: int, rng: np.random.Generator) -> np.ndarray:
    attack = max(1, length // 12)
    release = max(1, length // 10)
    env = np.ones(length, dtype=np.float64)
    env[:attack] = np.linspace(0.0, 1.0, attack)
    env[-release:] = np.linspace(1.0, 0.0, release)
    jitter = rng.normal(0.0, 0.04, length)
    return np.clip(env + jitter, 0.05, 1.0)


def _speech_segment(length: int, rng: np.random.Generator) -> np.ndarray:
    t = np.arange(length, dtype=np.float64) / SR
    f0 = 120.0 + rng.uniform(-12.0, 12.0)
    vibrato = 6.0 * np.sin(2.0 * np.pi * 4.5 * t)
    phase = 2.0 * np.pi * (f0 * t + np.cumsum(vibrato) / SR)
    signal = (
        0.55 * np.sin(phase)
        + 0.28 * np.sin(2.0 * phase)
        + 0.14 * np.sin(3.0 * phase)
        + 0.06 * np.sin(5.0 * phase)
    )
    signal *= 0.62 * _envelope(length, rng)
    return signal


def build_sample(name: str, duration: float, intervals, pops, rng):
    total = int(round(duration * SR))
    samples = rng.normal(0.0, 0.012, total).astype(np.float64)
    for start_s, end_s in intervals:
        start = int(round(start_s * SR))
        end = int(round(end_s * SR))
        samples[start:end] += _speech_segment(end - start, rng)
    for pop_s in pops:
        center = int(round(pop_s * SR))
        lo, hi = max(0, center - 4), min(total, center + 5)
        samples[lo:hi] += rng.choice([-1.0, 1.0]) * 0.85 * np.hanning(hi - lo)
    return np.clip(samples, -0.999, 0.999)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {"sample_rate_hz": SR, "subtype": "PCM_16", "channels": 1, "samples": []}
    for name, duration, intervals, pops in SPEECH_PLAN:
        rng = np.random.default_rng(_seed_for(name))
        audio = build_sample(name, duration, intervals, pops, rng)
        sf.write(OUT / name, audio.astype(np.float64), SR, subtype="PCM_16")
        manifest["samples"].append(
            {"name": name, "duration_s": duration,
             "speech_intervals_s": intervals, "pops_s": pops}
        )

    # Deliberately non-conforming files for E_AUDIO_FORMAT coverage.
    rng = np.random.default_rng(BASE_SEED + 77)
    sf.write(OUT / "bad_sample_rate.wav",
             rng.normal(0.0, 0.01, SR).astype(np.float64), 8_000, subtype="PCM_16")
    stereo = np.stack([rng.normal(0.0, 0.01, SR)] * 2, axis=1)
    sf.write(OUT / "bad_channels.wav", stereo, SR, subtype="PCM_16")
    (OUT / "not_audio.wav").write_text("this is not a RIFF container\n", encoding="utf-8")
    manifest["invalid_samples"] = ["bad_sample_rate.wav", "bad_channels.wav", "not_audio.wav"]

    with open(OUT / "samples_manifest.json", "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
    print(f"wrote {len(SPEECH_PLAN)} valid + 3 invalid samples to {OUT}")


if __name__ == "__main__":
    main()
