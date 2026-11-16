"""Deterministic synthetic WAV generator.

Fixed seed, 16 kHz mono 16-bit PCM.  Every speaker owns a stable voiceprint:
a fixed set of harmonic frequencies.  Utterances alternate between speakers,
some overlap, gaps are silent, and stationary Gaussian floor noise is added.
Re-running the script always produces byte-identical files.
"""
from __future__ import annotations

import json
import os
import wave
from pathlib import Path

import numpy as np
import soundfile as sf

SEED = 20240517
SAMPLE_RATE = 16000

SPEAKERS = {
    "S1": {"base": 180.0, "partials": (1.0, 2.0, 3.1, 4.7), "gains": (1.0, 0.55, 0.35, 0.2)},
    "S2": {"base": 245.0, "partials": (1.0, 2.2, 3.4, 5.1), "gains": (1.0, 0.5, 0.3, 0.18)},
    "S3": {"base": 320.0, "partials": (1.0, 2.5, 5.3, 7.8), "gains": (1.0, 0.72, 0.48, 0.34)},
}


def speaker_tone(speaker: str, duration_ms: float, rng: np.random.Generator) -> np.ndarray:
    n = int(round(SAMPLE_RATE * duration_ms / 1000.0))
    params = SPEAKERS[speaker]
    t = np.arange(n, dtype=np.float64) / SAMPLE_RATE
    tone = np.zeros(n, dtype=np.float64)
    for partial, gain in zip(params["partials"], params["gains"]):
        freq = params["base"] * partial
        # deterministic slow vibrato, still fully seed-driven
        vibrato = 1.0 + 0.004 * np.sin(2.0 * np.pi * (4.0 + partial) * t)
        phase = 2.0 * np.pi * freq * np.cumsum(vibrato) / SAMPLE_RATE
        tone += gain * np.sin(phase + rng.uniform(0.0, 2.0 * np.pi))
    # smooth attack/release envelope avoids boundary clicks
    ramp = max(1, n // 16)
    envelope = np.ones(n, dtype=np.float64)
    envelope[:ramp] = np.linspace(0.0, 1.0, ramp)
    envelope[-ramp:] = np.linspace(1.0, 0.0, ramp)
    tone *= envelope
    peak = np.max(np.abs(tone)) or 1.0
    return 0.35 * tone / peak


def build_timeline(file_index: int) -> list[dict]:
    """Speaker activity plan: alternating speech, silences, one overlap."""
    plans = [
        [
            {"speaker": "S1", "start_ms": 200, "end_ms": 1200},
            {"speaker": "S2", "start_ms": 1400, "end_ms": 2400},
            {"speaker": "S3", "start_ms": 2600, "end_ms": 3500},
            {"speaker": "S1", "start_ms": 3700, "end_ms": 4350},
            {"speaker": "S2", "start_ms": 4480, "end_ms": 5200, "overlap_with": "S1"},
            {"speaker": "S3", "start_ms": 5600, "end_ms": 6500},
        ],
        [
            {"speaker": "S2", "start_ms": 150, "end_ms": 1050},
            {"speaker": "S3", "start_ms": 1300, "end_ms": 2200},
            {"speaker": "S1", "start_ms": 2450, "end_ms": 3200},
            {"speaker": "S3", "start_ms": 3330, "end_ms": 4000, "overlap_with": "S1"},
            {"speaker": "S2", "start_ms": 4300, "end_ms": 5200},
        ],
        [
            {"speaker": "S3", "start_ms": 300, "end_ms": 1100},
            {"speaker": "S1", "start_ms": 1350, "end_ms": 2250},
            {"speaker": "S2", "start_ms": 2500, "end_ms": 3400},
            {"speaker": "S1", "start_ms": 3650, "end_ms": 4550},
            {"speaker": "S3", "start_ms": 4800, "end_ms": 5700},
        ],
    ]
    return plans[file_index]


def render_file(file_index: int) -> tuple[np.ndarray, list[dict]]:
    timeline = build_timeline(file_index)
    total_ms = max(item["end_ms"] for item in timeline) + 400
    n = int(round(SAMPLE_RATE * total_ms / 1000.0))
    mix = np.zeros(n, dtype=np.float64)
    rng = np.random.default_rng(SEED + 1000 + file_index)
    for item in timeline:
        start = int(round(SAMPLE_RATE * item["start_ms"] / 1000.0))
        tone = speaker_tone(item["speaker"], item["end_ms"] - item["start_ms"], rng)
        end = min(n, start + tone.shape[0])
        mix[start:end] += tone[: end - start]
    # stationary floor noise from an independent fixed-seed stream
    noise_rng = np.random.default_rng(SEED + 5000 + file_index)
    mix += 0.004 * noise_rng.standard_normal(n)
    peak = np.max(np.abs(mix))
    if peak > 0.98:
        mix *= 0.98 / peak
    return mix.astype(np.float64), timeline


def write_pcm16(path: Path, samples: np.ndarray) -> None:
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = np.round(pcm * 32767.0).astype("<i2")
    sf.write(str(path), pcm, SAMPLE_RATE, subtype="PCM_16")


def write_bad_samples(bad_dir: Path) -> list[dict]:
    bad_dir.mkdir(parents=True, exist_ok=True)
    records = []

    # wrong sample rate (8 kHz)
    rng = np.random.default_rng(SEED + 9001)
    wrong_rate = rng.standard_normal(8000) * 0.1
    rate_path = bad_dir / "bad_rate_8k.wav"
    sf.write(str(rate_path), np.round(wrong_rate * 32767).astype("<i2"), 8000, subtype="PCM_16")
    records.append({"file": rate_path.name, "expected_code": "E_AUDIO_FORMAT", "reason": "sample_rate=8000"})

    # truncated container: valid header then missing data
    trunc_path = bad_dir / "bad_truncated.wav"
    good = bad_dir / "_tmp_good.wav"
    write_pcm16(good, rng.standard_normal(16000) * 0.1)
    raw = good.read_bytes()
    trunc_path.write_bytes(raw[: len(raw) // 2])
    good.unlink()
    records.append({"file": trunc_path.name, "expected_code": "E_AUDIO_FORMAT", "reason": "truncated data chunk"})

    # stereo file
    stereo = (rng.standard_normal((16000, 2)) * 0.1)
    stereo_path = bad_dir / "bad_stereo.wav"
    sf.write(str(stereo_path), np.round(stereo * 32767).astype("<i2"), SAMPLE_RATE, subtype="PCM_16")
    records.append({"file": stereo_path.name, "expected_code": "E_AUDIO_FORMAT", "reason": "channels=2"})
    return records


def main() -> None:
    root = Path(__file__).resolve().parent
    audio_dir = root / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"seed": SEED, "sample_rate": SAMPLE_RATE, "subtype": "PCM_16", "files": []}
    for index in range(3):
        samples, timeline = render_file(index)
        name = f"sample_{index + 1}.wav"
        path = audio_dir / name
        write_pcm16(path, samples)
        manifest["files"].append({"file": name, "timeline": timeline})
    with open(audio_dir / "manifest.json", "w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
    bad_records = write_bad_samples(root / "bad_audio")
    with open(root / "bad_audio" / "manifest.json", "w", encoding="utf-8", newline="\n") as handle:
        json.dump({"files": bad_records}, handle, indent=2, sort_keys=True)
    print(f"generated {len(manifest['files'])} WAV files in {audio_dir}")
    print(f"generated {len(bad_records)} bad WAV files in {root / 'bad_audio'}")


if __name__ == "__main__":
    main()
