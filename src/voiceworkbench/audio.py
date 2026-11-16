"""WAV container validation and deterministic audio reading."""
from __future__ import annotations

import os
import wave
from dataclasses import dataclass
from typing import Iterator

import numpy as np
import soundfile as sf

from .errors import AudioFormatError

EXPECTED_RATE = 16000
EXPECTED_CHANNELS = 1
EXPECTED_SAMPLE_WIDTH = 2  # 16-bit PCM


@dataclass(frozen=True)
class AudioFile:
    path: str
    name: str
    samples: np.ndarray  # float64 mono in [-1, 1]
    sample_count: int
    sample_rate: int

    @property
    def duration_ms(self) -> float:
        return self.sample_count * 1000.0 / self.sample_rate


def validate_wav_header(path: str) -> None:
    """Validate RIFF/WAVE container header and PCM format fields."""
    try:
        with wave.open(path, "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            rate = wav.getframerate()
            frames = wav.getnframes()
            comp = wav.getcomptype()
    except (wave.Error, EOFError, OSError) as exc:
        raise AudioFormatError(
            f"unreadable WAV container: {os.path.basename(path)}",
            path=path,
        ) from exc
    if comp != "NONE":
        raise AudioFormatError("unsupported compression", path=path)
    if channels != EXPECTED_CHANNELS:
        raise AudioFormatError(
            f"channels={channels}, expected {EXPECTED_CHANNELS}", path=path,
        )
    if sample_width != EXPECTED_SAMPLE_WIDTH:
        raise AudioFormatError(
            f"sample_width={sample_width * 8}bit, expected 16bit", path=path,
        )
    if rate != EXPECTED_RATE:
        raise AudioFormatError(
            f"sample_rate={rate}, expected {EXPECTED_RATE}", path=path,
        )
    if frames <= 0:
        raise AudioFormatError("empty audio stream", path=path)
    # detect truncated data chunks: the actual PCM byte count must match the
    # declared frame count (wave only validates the header, not short reads)
    file_size = os.path.getsize(path)
    expected_data_bytes = frames * channels * sample_width
    if expected_data_bytes > file_size:
        raise AudioFormatError(
            "truncated PCM data chunk",
            path=path,
            expected_bytes=expected_data_bytes,
            file_size=file_size,
        )


def read_wav(path: str) -> AudioFile:
    validate_wav_header(path)
    name = os.path.basename(path)
    try:
        data, rate = sf.read(path, dtype="float64", always_2d=False)
    except Exception as exc:  # soundfile/libsndfile failure
        raise AudioFormatError(f"decode failure: {name}", path=path) from exc
    if rate != EXPECTED_RATE or data.ndim != 1:
        raise AudioFormatError(f"decoded stream mismatch: {name}", path=path)
    if not np.all(np.isfinite(data)):
        raise AudioFormatError(f"non-finite samples: {name}", path=path)
    return AudioFile(
        path=path,
        name=name,
        samples=np.asarray(data, dtype=np.float64),
        sample_count=int(data.shape[0]),
        sample_rate=rate,
    )


def scan_wav_files(input_dir: str) -> list[str]:
    """Return deterministically ordered .wav paths inside *input_dir*."""
    if not os.path.isdir(input_dir):
        raise AudioFormatError(f"input directory missing: {input_dir}")
    paths = [
        os.path.join(input_dir, name)
        for name in sorted(os.listdir(input_dir))
        if name.lower().endswith(".wav")
    ]
    return paths


def iter_frames(samples: np.ndarray, frame_size: int, hop_size: int) -> Iterator[np.ndarray]:
    """Yield frames with zero padding for the final short frame."""
    total = samples.shape[0]
    if total == 0:
        return
    start = 0
    while start < total:
        end = start + frame_size
        frame = samples[start:end]
        if frame.shape[0] < frame_size:
            padded = np.zeros(frame_size, dtype=np.float64)
            padded[: frame.shape[0]] = frame
            frame = padded
        yield frame
        start += hop_size
