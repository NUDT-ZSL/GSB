"""Helpers shared by the test modules."""

from __future__ import annotations

import json
import math
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 44100


def tone(sample_rate: int = SAMPLE_RATE, duration_ms: float = 800.0, onset_ms: float = 200.0,
         amplitude: float = 0.5, freq_hz: float = 220.0, fade_ms: float = 0.0) -> np.ndarray:
    """Float mono signal that is exactly zero before ``onset_ms``."""
    n = int(round(sample_rate * duration_ms / 1000.0))
    onset = int(round(sample_rate * onset_ms / 1000.0))
    out = np.zeros(n, dtype=np.float64)
    if onset < n:
        t = np.arange(n - onset, dtype=np.float64) / sample_rate
        # Cosine phase -> first sample at full amplitude, so the onset is
        # localisable to the exact sample.
        sig = amplitude * np.cos(2.0 * math.pi * freq_hz * t)
        if fade_ms > 0:
            fade_n = max(1, int(round(sample_rate * fade_ms / 1000.0)))
            env = np.ones_like(sig)
            ramp = min(fade_n, sig.size)
            env[:ramp] = np.linspace(0.0, 1.0, ramp, endpoint=False)
            if sig.size > ramp:
                env[-ramp:] = np.minimum(env[-ramp:], np.linspace(1.0, 0.0, ramp, endpoint=False))
            sig = sig * env
        out[onset:] = sig
    return out


def write_wav(path: Path, mono: np.ndarray, sample_rate: int = SAMPLE_RATE, channels: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(mono, -1.0, 1.0)
    if channels == 1:
        data = (pcm * 32767.0).astype("<i2")
    else:
        data = np.repeat(pcm[:, None], channels, axis=1)
        data = (data * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(data.tobytes())
    return path


def write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def make_frames(line_times: dict[str, list[float]] | list[dict]) -> dict:
    if isinstance(line_times, dict):
        rows = [{"line_id": line_id, "mouth_time_ms": t} for line_id, times in line_times.items() for t in times]
    else:
        rows = line_times
    return {"frames": rows}


def make_dialogues(entries: list[dict]) -> dict:
    return {"dialogues": entries}
