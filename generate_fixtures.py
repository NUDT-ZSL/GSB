#!/usr/bin/env python3
"""Deterministically generate test WAV masters and JSON manifests.

No third-party libraries, no network. Everything is derived from fixed
parameters so repeated runs produce byte-identical fixtures.

Layout of the main 16 kHz / 4 s master (single channel, 16-bit PCM):
  0.0-1.0 s  sine at amplitude 20000
  1.0-2.0 s  sine at amplitude  8000
  2.0-3.0 s  all zeros (silence)
  3.0-4.0 s  sine at amplitude 30000
"""

from __future__ import annotations

import array
import json
import math
import os
import struct
import wave
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def sine_segment(sample_rate: int, duration_s: float, freq: float,
                 amplitude: int, phase_sample_offset: int = 0) -> array.array:
    n = int(round(sample_rate * duration_s))
    out = array.array("h", (0,)) * 0
    for i in range(n):
        value = amplitude * math.sin(
            2.0 * math.pi * freq * (i + phase_sample_offset) / sample_rate
        )
        out.append(max(-32768, min(32767, int(math.floor(value + 0.5)))))
    return out


def silence_segment(sample_rate: int, duration_s: float) -> array.array:
    return array.array("h", [0]) * int(round(sample_rate * duration_s))


def build_master(sample_rate: int) -> array.array:
    parts = [
        sine_segment(sample_rate, 1.0, 440.0, 20000, 0),
        sine_segment(sample_rate, 1.0, 440.0, 8000, sample_rate),
        silence_segment(sample_rate, 1.0),
        sine_segment(sample_rate, 1.0, 440.0, 30000, sample_rate * 3),
    ]
    samples = array.array("h")
    for part in parts:
        samples.extend(part)
    return samples


def write_mono_wav(path: Path, sample_rate: int, samples: array.array) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.tobytes())


def write_stereo_wav(path: Path, sample_rate: int, samples: array.array) -> None:
    stereo = array.array("h")
    for s in samples:
        stereo.append(s)
        stereo.append(s)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(stereo.tobytes())


def write_8bit_wav(path: Path, sample_rate: int, samples: array.array) -> None:
    # 8-bit PCM in WAV is unsigned: 128 is silence.
    data = bytearray()
    for s in samples:
        u = max(0, min(255, (s >> 8) + 128))
        data.append(u)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(data))


def corrupt_header(path: Path) -> None:
    """Overwrite the RIFF magic so the header parser rejects the file."""
    raw = path.read_bytes()
    path.write_bytes(b"XXXX" + raw[4:])


def truncate_body(path: Path) -> None:
    """Keep an intact header but chop the PCM payload in half."""
    with wave.open(str(path), "rb") as wf:
        n_frames = wf.getnframes()
        rate = wf.getframerate()
    raw = path.read_bytes()
    # Header for plain PCM is 44 bytes; drop the second half of the payload.
    payload_bytes = n_frames * 2
    path.write_bytes(raw[: 44 + payload_bytes // 2])
    # Also patch the declared sizes so the mismatch is in the data chunk.
    _ = rate


def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    master_samples = build_master(16000)

    # 1. Accepted masters at several sample rates.
    for rate in (8000, 16000, 48000):
        write_mono_wav(FIXTURE_DIR / f"master_{rate}.wav", rate, build_master(rate))

    # 2. Rejected inputs.
    write_stereo_wav(FIXTURE_DIR / "stereo_16000.wav", 16000, master_samples)
    write_8bit_wav(FIXTURE_DIR / "8bit_16000.wav", 16000, master_samples)

    bad = FIXTURE_DIR / "bad_header.wav"
    write_mono_wav(bad, 16000, master_samples)
    corrupt_header(bad)

    truncated = FIXTURE_DIR / "truncated.wav"
    write_mono_wav(truncated, 16000, master_samples)
    truncate_body(truncated)

    # 3. Mixed manifest against the 4 s / 16 kHz master.
    mixed_manifest = [
        {"start_ms": 0, "end_ms": 500, "output_name": "intro.wav"},
        {"start_ms": 1000, "end_ms": 1500, "output_name": "middle.wav"},
        {"start_ms": 2000, "end_ms": 2500, "output_name": "silent_part.wav"},  # silence
        {"start_ms": 3000, "end_ms": 5000, "output_name": "over_end.wav"},     # clamped
        {"start_ms": 9000, "end_ms": 9500, "output_name": "beyond.wav"},       # skipped
        {"end_ms": 100},                                  # missing start_ms
        {"start_ms": 100},                                # missing end_ms
        {"start_ms": -50, "end_ms": 100},                 # illegal time
        {"start_ms": 200, "end_ms": 200},                 # empty interval
        {"start_ms": "100", "end_ms": 200},               # wrong type
        {"start_ms": 3200, "end_ms": 3600},               # valid, auto name
    ]
    (FIXTURE_DIR / "manifest_mixed.json").write_text(
        json.dumps(mixed_manifest, indent=2), encoding="utf-8"
    )

    # 4. Single clip for precise target-peak checks.
    (FIXTURE_DIR / "manifest_one.json").write_text(
        json.dumps([{"start_ms": 3000, "end_ms": 4000, "output_name": "loud.wav"}],
                   indent=2),
        encoding="utf-8",
    )

    # 5. Two valid clips for the read-only mid-write failure test.
    (FIXTURE_DIR / "manifest_readonly.json").write_text(
        json.dumps([
            {"start_ms": 0, "end_ms": 400, "output_name": "clip_a.wav"},
            {"start_ms": 500, "end_ms": 900, "output_name": "clip_b.wav"},
        ], indent=2),
        encoding="utf-8",
    )

    # 6. Manifest that is not even valid JSON.
    (FIXTURE_DIR / "manifest_broken.json").write_text(
        '[{"start_ms": 0, "end_ms": 100},', encoding="utf-8"
    )

    print(f"fixtures written under {FIXTURE_DIR}")
    for p in sorted(FIXTURE_DIR.iterdir()):
        print(f"  {p.name}  ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
