"""Minimal mono PCM WAV I/O using only the Python standard library and NumPy."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np


def read_mono_wav(path: str | Path) -> tuple[np.ndarray, int]:
    path = Path(path)
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        sample_rate = handle.getframerate()
        n_frames = handle.getnframes()
        raw = handle.readframes(n_frames)
    if channels != 1:
        raise ValueError(f"{path}: expected mono WAV, found {channels} channels")
    if sample_width == 1:
        data = np.frombuffer(raw, dtype=np.uint8).astype(np.float64)
        signal = (data - 128.0) / 128.0
    elif sample_width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float64)
        signal = data / 32768.0
    elif sample_width == 3:
        data = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        values = data[:, 0] | (data[:, 1] << 8) | (data[:, 2] << 16)
        values = np.where(values & 0x800000, values - 0x1000000, values)
        signal = values.astype(np.float64) / 8388608.0
    elif sample_width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float64)
        signal = data / 2147483648.0
    else:
        raise ValueError(f"{path}: unsupported sample width {sample_width} bytes")
    return signal, int(sample_rate)


def write_mono_wav(path: str | Path, signal: np.ndarray, sample_rate: int, sample_width: int = 2) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    signal = np.asarray(signal, dtype=np.float64).reshape(-1)
    if sample_width == 1:
        payload = np.clip(np.round(signal * 127.0 + 128.0), 0, 255).astype(np.uint8).tobytes()
    elif sample_width == 2:
        payload = np.clip(np.round(signal * 32767.0), -32768, 32767).astype("<i2").tobytes()
    elif sample_width == 3:
        values = np.clip(np.round(signal * 8388607.0), -8388608, 8388607).astype(np.int32)
        unsigned = values & 0xFFFFFF
        payload = np.stack(
            [(unsigned & 0xFF), ((unsigned >> 8) & 0xFF), ((unsigned >> 16) & 0xFF)], axis=1
        ).astype(np.uint8).tobytes()
    elif sample_width == 4:
        payload = np.clip(np.round(signal * 2147483647.0), -2147483648, 2147483647).astype("<i4").tobytes()
    else:
        raise ValueError(f"unsupported sample width {sample_width}")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(sample_width)
        handle.setframerate(int(sample_rate))
        handle.writeframes(payload)
