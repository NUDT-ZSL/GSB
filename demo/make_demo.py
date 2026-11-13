#!/usr/bin/env python3
"""Deterministically generate demo WAVs + manifests for podcast_concat.py.

Creates, relative to ``demo/``:

  media/seg01_intro.wav    1000 ms, 44100 Hz, mono 16-bit, peak +/-1200
  media/seg02_host.wav     1000 ms, 44100 Hz, mono 16-bit, peak +/-900
  media/seg03_guest.wav    1000 ms, 44100 Hz, mono 16-bit, peak +/-1500
  media/seg04_48k.wav      1000 ms, 48000 Hz (deliberately wrong rate)
  manifest.json            3 segments; seg02 requests [800, 1200) so the run
                           also demonstrates truncation-to-EOF warning handling.
  manifest_with_errors.json
    valid segment, missing file, wrong sample rate, valid segment — shows that
    bad media never contaminates successful ones.
  manifest_all_mismatch.json
    every segment at 48000 Hz against target 44100 — shows the zero-length
    but valid WAV path.
  manifest_silent.json
    a single all-zero segment — shows the "silent input" warning path.

The samples are a deterministic alternating pattern — no randomness, no
external assets.  Run:  python demo/make_demo.py
"""

from __future__ import annotations

import array
import json
import wave
from pathlib import Path

SAMPLE_RATE = 44100
DEMO_DIR = Path(__file__).resolve().parent

SEGMENTS = [
    # (file, frames, amplitude, manifest start_ms, manifest end_ms)
    ("media/seg01_intro.wav", SAMPLE_RATE, 1200, 0, 1000),
    ("media/seg02_host.wav", SAMPLE_RATE, 900, 800, 1200),  # intentionally over-runs
    ("media/seg03_guest.wav", SAMPLE_RATE, 1500, 0, 1000),
]


def write_tone(path: Path, frames: int, amplitude: int, sample_rate: int = SAMPLE_RATE) -> None:
    # A simple, deterministic periodic pattern: alternating amplitude values.
    samples = array.array(
        "h", [amplitude if index % 2 == 0 else -amplitude for index in range(frames)]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(samples.tobytes())


def write_silence(path: Path, frames: int, sample_rate: int = SAMPLE_RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * frames)


def write_manifest(name: str, gap_ms: int, segments: list[dict]) -> Path:
    path = DEMO_DIR / name
    payload = {
        "target_sample_rate": SAMPLE_RATE,
        "gap_ms": gap_ms,
        "segments": segments,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> None:
    manifest_segments = []
    for file_name, frames, amplitude, start_ms, end_ms in SEGMENTS:
        write_tone(DEMO_DIR / file_name, frames, amplitude)
        manifest_segments.append({"file": file_name, "start_ms": start_ms, "end_ms": end_ms})

    write_manifest("manifest.json", 80, manifest_segments)

    write_tone(DEMO_DIR / "media/seg04_48k.wav", 48000, 2000, sample_rate=48000)
    write_manifest(
        "manifest_with_errors.json",
        80,
        [
            {"file": "media/seg01_intro.wav", "start_ms": 0, "end_ms": 1000},
            {"file": "media/does_not_exist.wav", "start_ms": 0, "end_ms": 1000},
            {"file": "media/seg04_48k.wav", "start_ms": 0, "end_ms": 1000},
            {"file": "media/seg03_guest.wav", "start_ms": 0, "end_ms": 1000},
        ],
    )
    write_manifest(
        "manifest_all_mismatch.json",
        80,
        [
            {"file": "media/seg04_48k.wav", "start_ms": 0, "end_ms": 1000},
            {"file": "media/seg04_48k.wav", "start_ms": 0, "end_ms": 1000},
        ],
    )

    write_silence(DEMO_DIR / "media/silence.wav", SAMPLE_RATE)
    write_manifest(
        "manifest_silent.json",
        80,
        [{"file": "media/silence.wav", "start_ms": 0, "end_ms": 1000}],
    )

    print(f"wrote demo assets and manifests under {DEMO_DIR}")


if __name__ == "__main__":
    main()
