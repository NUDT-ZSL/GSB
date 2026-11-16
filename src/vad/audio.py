"""Audio loading, container validation and deterministic framing."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .errors import E_AUDIO_FORMAT, E_NONFINITE_SAMPLE, VadError


@dataclass(frozen=True)
class Frame:
    index: int
    start_sample: int
    timestamp_ms: int
    zero_padded: bool
    samples: np.ndarray


def load_wav(path: Path, expected_sr: int = 16000,
             expected_channels: int = 1, expected_subtype: str = "PCM_16") -> np.ndarray:
    """Load a mono WAV and validate its container, sample rate, channels and bit depth."""
    path = Path(path)
    try:
        info = sf.info(str(path))
    except Exception as exc:  # unreadable / non-RIFF container
        raise VadError(E_AUDIO_FORMAT, f"unreadable container ({exc})") from exc

    problems = []
    if info.samplerate != expected_sr:
        problems.append(f"sample_rate={info.samplerate}!={expected_sr}")
    if info.channels != expected_channels:
        problems.append(f"channels={info.channels}!={expected_channels}")
    if info.format != "WAV":
        problems.append(f"format={info.format}!=WAV")
    if info.subtype != expected_subtype:
        problems.append(f"subtype={info.subtype}!={expected_subtype}")
    if problems:
        raise VadError(E_AUDIO_FORMAT, "; ".join(problems))

    data, sr = sf.read(str(path), dtype="float64", always_2d=False)
    if sr != expected_sr or data.ndim != 1:
        raise VadError(E_AUDIO_FORMAT, "post-read shape/rate mismatch")
    if not np.all(np.isfinite(data)):
        raise VadError(E_NONFINITE_SAMPLE,
                       "non-finite sample detected at read stage")
    return data


def frame_signal(samples: np.ndarray, frame_length_ms: int, frame_hop_ms: int,
                 sample_rate: int = 16000) -> list[Frame]:
    frame_len = int(round(frame_length_ms * sample_rate / 1000))
    hop_len = int(round(frame_hop_ms * sample_rate / 1000))
    total = samples.shape[0]
    if total == 0:
        return []
    n_frames = 1 + (max(0, total - frame_len) + hop_len - 1) // hop_len
    frames: list[Frame] = []
    for index in range(n_frames):
        start = index * hop_len
        end = start + frame_len
        chunk = samples[start:min(end, total)]
        padded = chunk.shape[0] < frame_len
        if padded:
            chunk = np.concatenate(
                (chunk, np.zeros(frame_len - chunk.shape[0], dtype=np.float64)))
        frames.append(Frame(
            index=index,
            start_sample=start,
            timestamp_ms=index * frame_hop_ms,  # integer math, no float accumulation
            zero_padded=padded,
            samples=chunk.astype(np.float64, copy=True),
        ))
    return frames
