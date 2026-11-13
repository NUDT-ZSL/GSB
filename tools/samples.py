"""Deterministic WAV sample construction for demos and tests.

No external audio assets are used.  Every sample is produced from a fixed
formula, so runs are byte-for-byte reproducible.
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

INT16_MIN = -32768
INT16_MAX = 32767


def _clip(value: int) -> int:
    return max(INT16_MIN, min(INT16_MAX, int(value)))


def sine_wav(
    path: Path | str,
    *,
    freq_hz: float,
    duration_ms: int,
    sample_rate: int = 44100,
    channels: int = 1,
    amplitude: float = 0.5,
) -> Path:
    """Write a mono/stereo sine tone of *duration_ms* milliseconds."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(round(sample_rate * duration_ms / 1000))
    peak = 32767 * amplitude
    values: list[int] = []
    for frame in range(frames):
        sample = _clip(round(peak * math.sin(2 * math.pi * freq_hz * frame / sample_rate)))
        values.extend([sample] * channels)
    payload = struct.pack(f"<{len(values)}h", *values)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.setcomptype("NONE", "not compressed")
        writer.writeframes(payload)
    return path


def constant_wav(
    path: Path | str,
    *,
    value: int,
    duration_ms: int,
    sample_rate: int = 44100,
    channels: int = 1,
) -> Path:
    """Write a constant-amplitude WAV; useful for clipping/fade assertions."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(round(sample_rate * duration_ms / 1000))
    values = [max(INT16_MIN, min(INT16_MAX, value))] * (frames * channels)
    payload = struct.pack(f"<{len(values)}h", *values)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.setcomptype("NONE", "not compressed")
        writer.writeframes(payload)
    return path


def eight_bit_wav(
    path: Path | str,
    *,
    duration_ms: int = 50,
    sample_rate: int = 44100,
) -> Path:
    """Write an 8-bit unsigned PCM WAV (format mismatch fixture)."""

    path = Path(path)
    frames = int(round(sample_rate * duration_ms / 1000))
    payload = bytes([128] * frames)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(1)
        writer.setframerate(sample_rate)
        writer.setcomptype("NONE", "not compressed")
        writer.writeframes(payload)
    return path


def read_all_samples(path: Path | str) -> tuple[list[int], int, int]:
    """Return ``(interleaved_samples, channels, sample_rate)`` from a WAV."""

    with wave.open(str(path), "rb") as reader:
        channels = reader.getnchannels()
        rate = reader.getframerate()
        raw = reader.readframes(reader.getnframes())
    count = len(raw) // 2
    return list(struct.unpack(f"<{count}h", raw)), channels, rate


def write_manifest(path: Path | str, clips: list[dict]) -> Path:
    import json

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"clips": clips}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path
