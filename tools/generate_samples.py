#!/usr/bin/env python3
"""Generate deterministic local WAV tracks and a manifest; no network access."""

from __future__ import annotations

import argparse
import json
import wave
from pathlib import Path

import numpy as np


def write_pcm_wav(path: Path, samples: np.ndarray, sample_rate: int, bit_depth: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    values = np.asarray(samples, dtype=np.float64)
    if values.ndim == 1:
        values = values.reshape(-1, 1)
    channels = values.shape[1]
    values = np.clip(values, -1.0, 1.0 - 1.0 / (2 ** (bit_depth - 1)))
    if bit_depth == 16:
        payload = np.rint(values * 32767.0).astype("<i2").tobytes()
        width = 2
    elif bit_depth == 24:
        integers = np.rint(values * 8388607.0).astype(np.int32).reshape(-1)
        packed = integers.astype("<i4").view(np.uint8).reshape(-1, 4)[:, :3]
        payload = packed.tobytes()
        width = 3
    elif bit_depth == 32:
        payload = np.rint(values * 2147483647.0).astype("<i4").tobytes()
        width = 4
    else:
        raise ValueError("supported demo bit depths: 16, 24, 32")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(sample_rate)
        handle.writeframes(payload)


def sine(duration: float, sample_rate: int, frequency: float, amplitude: float) -> np.ndarray:
    time_axis = np.arange(int(round(duration * sample_rate)), dtype=np.float64) / sample_rate
    return amplitude * np.sin(2.0 * np.pi * frequency * time_axis)


def impulse(duration: float, sample_rate: int, at_seconds: float, amplitude: float) -> np.ndarray:
    result = np.zeros(int(round(duration * sample_rate)), dtype=np.float64)
    index = int(round(at_seconds * sample_rate))
    if 0 <= index < result.size:
        result[index] = amplitude
    return result


def build_demo(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)

    mono = sine(2.0, 48000, 440.0, 0.22)
    mono[19200:28800] = 0.0  # source-time silence, 400..600 ms
    write_pcm_wav(directory / "voice_48k_mono_16.wav", mono, 48000, 16)

    stereo = np.zeros((int(round(1.6 * 44100)), 2), dtype=np.float64)
    stereo[:, 0] = sine(1.6, 44100, 660.0, 0.12)
    stereo[:, 1] = sine(1.6, 44100, 880.0, 0.09)
    write_pcm_wav(directory / "music_44k1_stereo_24.wav", stereo, 44100, 24)

    click = impulse(1.0, 22050, 0.5, 0.7)
    write_pcm_wav(directory / "click_22k_mono_32.wav", click, 22050, 32)

    manifest = {
        "tracks": [
            {
                "file": "voice_48k_mono_16.wav",
                "sample_rate": 48000,
                "bit_depth": 16,
                "offset_ms": 0,
                "gain": 1.0,
                "silences": [{"start_ms": 400, "end_ms": 600}],
            },
            {
                "file": "music_44k1_stereo_24.wav",
                "sample_rate": 44100,
                "bit_depth": 24,
                "offset_ms": 300,
                "gain": 0.8,
                "silences": [{"start_ms": 500, "end_ms": 700}],
            },
            {
                "file": "click_22k_mono_32.wav",
                "sample_rate": 22050,
                "bit_depth": 32,
                "offset_ms": 1250,
                "gain": 1.0,
                "silences": [],
            },
        ],
        "output": {
            "file": "demo_mix.wav",
            "sample_rate": 48000,
            "channels": 2,
            "channel_layout": ["left", "right"],
            "bit_depth": 16,
            "statistics_window_ms": 100,
            "max_workers": 3,
            "memory_limit_mb": 64,
            "peak_policy": "clip",
        },
        "report_file": "demo_report.json",
        "state_file": "demo_state.json",
        "work_dir": ".demo-work",
    }
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic local demo WAVs")
    parser.add_argument("--output-dir", default="demo")
    args = parser.parse_args()
    manifest = build_demo(Path(args.output_dir).resolve())
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
