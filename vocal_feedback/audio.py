from __future__ import annotations

import math
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class AudioData:
    samples: np.ndarray
    sample_rate: int
    duration_s: float
    source: str


@dataclass
class FramedAudio:
    audio: AudioData
    frame_length: int
    hop_length: int
    starts: np.ndarray
    times: np.ndarray
    frames: np.ndarray
    energy_db: np.ndarray
    zero_crossing: np.ndarray


def _decode_pcm(raw: bytes, sample_width: int) -> np.ndarray:
    if sample_width == 1:
        values = np.frombuffer(raw, dtype=np.uint8).astype(np.float64)
        return (values - 128.0) / 128.0
    if sample_width == 2:
        values = np.frombuffer(raw, dtype="<i2").astype(np.float64)
        return values / 32768.0
    if sample_width == 3:
        byte_values = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        values = (
            byte_values[:, 0].astype(np.int32)
            | (byte_values[:, 1].astype(np.int32) << 8)
            | (byte_values[:, 2].astype(np.int32) << 16)
        )
        values = np.where(values >= 1 << 23, values - (1 << 24), values)
        return values.astype(np.float64) / float(1 << 23)
    if sample_width == 4:
        values = np.frombuffer(raw, dtype="<i4").astype(np.float64)
        return values / float(1 << 31)
    raise ValueError(f"unsupported PCM sample width: {sample_width} bytes")


def read_wav(path: str | Path) -> AudioData:
    path = Path(path)
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        sample_rate = handle.getframerate()
        frame_count = handle.getnframes()
        compression = handle.getcomptype()
        raw = handle.readframes(frame_count)
    if compression != "NONE":
        raise ValueError(f"{path}: only uncompressed PCM WAV is supported")
    if channels != 1:
        raise ValueError(f"{path}: expected mono audio, found {channels} channels")
    if sample_rate <= 0:
        raise ValueError(f"{path}: invalid sample rate")
    samples = _decode_pcm(raw, sample_width)
    # The generator and ordinary phone recordings should be finite. Refuse
    # malformed values rather than silently letting them poison F0 statistics.
    if not np.all(np.isfinite(samples)):
        raise ValueError(f"{path}: decoded audio contains non-finite samples")
    return AudioData(
        samples=samples,
        sample_rate=sample_rate,
        duration_s=float(len(samples) / sample_rate),
        source=str(path),
    )


def frame_audio(audio: AudioData, frame_length_s: float, hop_length_s: float) -> FramedAudio:
    frame_length = max(2, int(round(frame_length_s * audio.sample_rate)))
    hop_length = max(1, int(round(hop_length_s * audio.sample_rate)))
    samples = audio.samples
    if len(samples) < frame_length:
        padded = np.zeros(frame_length, dtype=np.float64)
        padded[: len(samples)] = samples
        samples = padded
    starts = np.arange(0, len(samples) - frame_length + 1, hop_length, dtype=np.int64)
    if len(starts) == 0:
        starts = np.array([0], dtype=np.int64)
    frames = np.stack([samples[start : start + frame_length] for start in starts])
    times = (starts + frame_length / 2.0) / audio.sample_rate

    energy = np.sqrt(np.mean(frames * frames, axis=1))
    energy_db = np.maximum(20.0 * np.log10(np.maximum(energy, 1e-9)), -120.0)
    signs = np.signbit(frames)
    zero_crossing = np.mean(signs[:, 1:] != signs[:, :-1], axis=1)
    return FramedAudio(
        audio=audio,
        frame_length=frame_length,
        hop_length=hop_length,
        starts=starts,
        times=times,
        frames=frames,
        energy_db=energy_db,
        zero_crossing=zero_crossing,
    )


def hann_window(length: int) -> np.ndarray:
    if length <= 1:
        return np.ones(length, dtype=np.float64)
    return 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(length) / (length - 1))


def db_to_amplitude(db: float) -> float:
    return math.pow(10.0, db / 20.0)

