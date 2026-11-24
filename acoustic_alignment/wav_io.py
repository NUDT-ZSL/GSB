"""Minimal deterministic PCM WAV reader supporting 16- and 24-bit samples."""
from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .codes import Code, EngineError

ALLOWED_SAMPLE_RATES = (16000, 44100, 48000)
ALLOWED_BIT_DEPTHS = (16, 24)


@dataclass(frozen=True)
class WavHeader:
    path: Path
    sample_rate: int
    channels: int
    bit_depth: int
    frames: int


@dataclass
class WavData:
    header: WavHeader
    samples: np.ndarray
    mono: np.ndarray
    source_sha256: str | None = None


def read_wav_header(path: str | Path) -> WavHeader:
    path = Path(path)
    if not path.is_file():
        raise EngineError(Code.E_FILE, f"WAV file does not exist: {path}", {"path": str(path)})
    try:
        with wave.open(str(path), "rb") as wav:
            sample_width = wav.getsampwidth()
            return WavHeader(
                path=path,
                sample_rate=int(wav.getframerate()),
                channels=int(wav.getnchannels()),
                bit_depth=sample_width * 8,
                frames=int(wav.getnframes()),
            )
    except EngineError:
        raise
    except (wave.Error, EOFError, OSError, ValueError) as exc:
        raise EngineError(Code.E_WAV, f"invalid WAV header: {path}", {"path": str(path)}) from exc


def _decode_pcm(raw: bytes, channels: int, sample_width: int) -> np.ndarray:
    if sample_width == 2:
        values = np.frombuffer(raw, dtype="<i2").astype(np.float64)
        values /= 32768.0
    elif sample_width == 3:
        packed = np.frombuffer(raw, dtype=np.uint8)
        if packed.size % 3:
            raise EngineError(Code.E_WAV, "24-bit PCM payload is not 3-byte aligned")
        bytes3 = packed.reshape(-1, 3)
        low = bytes3[:, 0].astype(np.int32)
        mid = bytes3[:, 1].astype(np.int32)
        high = bytes3[:, 2].astype(np.int32)
        values_i32 = low | (mid << 8) | (high << 16)
        sign_bit = values_i32 & np.int32(0x800000)
        values_i32 = values_i32 - np.where(sign_bit != 0, np.int32(1 << 24), 0).astype(np.int32)
        values = values_i32.astype(np.float64) / 8388608.0
    else:
        raise EngineError(Code.E_BITS, f"unsupported PCM byte width: {sample_width}")

    if values.size % channels:
        raise EngineError(Code.E_WAV, "PCM frame payload is not channel aligned")
    return values.reshape(-1, channels)


def read_wav(path: str | Path, digest: bool = False) -> WavData:
    path = Path(path)
    header = read_wav_header(path)
    try:
        with wave.open(str(path), "rb") as wav:
            raw = wav.readframes(header.frames)
            decoded = _decode_pcm(raw, header.channels, header.bit_depth // 8)
    except EngineError:
        raise
    except (wave.Error, EOFError, OSError, ValueError) as exc:
        raise EngineError(Code.E_WAV, f"cannot decode WAV: {path}", {"path": str(path)}) from exc

    if decoded.shape[0] != header.frames:
        raise EngineError(
            Code.E_WAV,
            "decoded frame count does not match header",
            {"header_frames": header.frames, "decoded_frames": int(decoded.shape[0])},
        )
    mono = np.mean(decoded, axis=1).astype(np.float64, copy=False) if header.channels > 1 else decoded[:, 0].copy()
    source_digest = None
    if digest:
        from .canonical import file_sha256

        source_digest = file_sha256(path)
    return WavData(header=header, samples=decoded, mono=np.ascontiguousarray(mono), source_sha256=source_digest)
