"""Deterministic PCM reading, writing and content chunk hashing."""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .canonical import hash_value
from .errors import E_INPUT_INVALID, E_SAMPLE_RATE, raise_error


@dataclass(frozen=True)
class PcmAudio:
    sample_rate: int
    channels: int
    samples: np.ndarray
    pcm_bytes: bytes

    @property
    def sample_count(self) -> int:
        return int(self.samples.shape[0])

    @property
    def duration_ms(self) -> int:
        return int(round(self.sample_count * 1000.0 / self.sample_rate))


def int16_to_float(samples: np.ndarray) -> np.ndarray:
    return np.asarray(samples, dtype=np.int16).astype(np.float64) / 32768.0


def float_to_int16(samples: np.ndarray, peak: float = 0.9) -> np.ndarray:
    values = np.asarray(samples, dtype=np.float64)
    maximum = float(np.max(np.abs(values))) if values.size else 0.0
    if maximum > peak and maximum > 0.0:
        values = values * (peak / maximum)
    values = np.clip(values, -1.0, 1.0)
    return np.rint(values * 32767.0).astype("<i2")


def read_wav(path: str | Path) -> PcmAudio:
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        sample_rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if channels != 1:
        raise_error(
            E_INPUT_INVALID,
            "only mono WAV files are supported",
            path=str(path),
            channels=channels,
        )
    if sample_rate != 16_000:
        raise_error(
            E_SAMPLE_RATE,
            "only 16 kHz audio is supported",
            expected=16_000,
            actual=sample_rate,
            path=str(path),
        )
    if sample_width == 2:
        pcm = np.frombuffer(frames, dtype="<i2").copy()
    elif sample_width == 1:
        unsigned = np.frombuffer(frames, dtype=np.uint8).astype(np.int16)
        pcm = ((unsigned.astype(np.int32) - 128) << 8).astype("<i2")
    elif sample_width == 4:
        pcm32 = np.frombuffer(frames, dtype="<i4")
        pcm = (pcm32 >> 16).astype("<i2")
    else:
        raise_error(
            E_INPUT_INVALID,
            "unsupported WAV sample width",
            path=str(path),
            sample_width=sample_width,
        )
    canonical_bytes = np.ascontiguousarray(pcm, dtype="<i2").tobytes(order="C")
    return PcmAudio(
        sample_rate=sample_rate,
        channels=channels,
        samples=int16_to_float(pcm),
        pcm_bytes=canonical_bytes,
    )


def read_npy(path: str | Path) -> PcmAudio:
    values = np.load(path, allow_pickle=False)
    if values.ndim != 1:
        raise_error(E_INPUT_INVALID, "PCM array must be one-dimensional", path=str(path))
    if np.issubdtype(values.dtype, np.integer):
        if values.dtype.itemsize <= 1:
            pcm = ((values.astype(np.int32) - 128) << 8).astype("<i2")
        elif values.dtype.itemsize == 2:
            pcm = values.astype("<i2", copy=False)
        else:
            pcm = (values.astype(np.int64) >> 16).astype("<i2")
    else:
        pcm = np.rint(np.clip(values.astype(np.float64), -1.0, 1.0) * 32767.0).astype("<i2")
    pcm = np.ascontiguousarray(pcm, dtype="<i2")
    return PcmAudio(
        sample_rate=16_000,
        channels=1,
        samples=int16_to_float(pcm),
        pcm_bytes=pcm.tobytes(order="C"),
    )


def read_raw_pcm(path: str | Path) -> PcmAudio:
    raw = Path(path).read_bytes()
    if len(raw) % 2:
        raise_error(E_INPUT_INVALID, "raw PCM byte count must be even", path=str(path))
    pcm = np.frombuffer(raw, dtype="<i2").copy()
    return PcmAudio(
        sample_rate=16_000,
        channels=1,
        samples=int16_to_float(pcm),
        pcm_bytes=np.ascontiguousarray(pcm, dtype="<i2").tobytes(order="C"),
    )


def read_pcm(path: str | Path) -> PcmAudio:
    suffix = Path(path).suffix.lower()
    if suffix == ".wav":
        return read_wav(path)
    if suffix == ".npy":
        return read_npy(path)
    if suffix in {".pcm", ".raw"}:
        return read_raw_pcm(path)
    raise_error(
        E_INPUT_INVALID,
        "unsupported PCM container",
        path=str(path),
        suffix=suffix,
    )


def write_wav(path: str | Path, samples: np.ndarray, sample_rate: int = 16_000) -> None:
    pcm = np.asarray(samples)
    if pcm.dtype != np.dtype("<i2"):
        pcm = float_to_int16(pcm)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(np.ascontiguousarray(pcm, dtype="<i2").tobytes(order="C"))


def chunk_hashes(pcm_bytes: bytes, chunk_bytes: int = 8192) -> list[str]:
    if chunk_bytes <= 0:
        raise_error(E_INPUT_INVALID, "chunk_bytes must be positive")
    return [
        hash_value({"offset": offset, "data": pcm_bytes[offset : offset + chunk_bytes].hex()})
        for offset in range(0, len(pcm_bytes), chunk_bytes)
    ]


def audio_identity(audio: PcmAudio, chunk_bytes: int = 8192) -> dict[str, Any]:
    hashes = chunk_hashes(audio.pcm_bytes, chunk_bytes=chunk_bytes)
    body = {
        "sample_rate": audio.sample_rate,
        "channels": audio.channels,
        "sample_count": audio.sample_count,
        "chunk_bytes": chunk_bytes,
        "chunk_hashes": hashes,
    }
    return {
        **body,
        "chunk_root": hash_value({"kind": "audio-chunks", **body}),
        "pcm_hash": hash_value({"pcm": audio.pcm_bytes.hex(), "sample_rate": audio.sample_rate}),
    }
