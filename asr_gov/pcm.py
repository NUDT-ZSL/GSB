"""确定性 16kHz 单声道 PCM 合成：语音活动段与静音段拼接，叠加平稳底噪。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16000
DEFAULT_SEED = 20261122
DEFAULT_CLIP_ID = "clip-demo-001"
DEFAULT_NOISE_AMP = 300

DEFAULT_SEGMENTS = [
    ("speech", 1200, 220.0),
    ("silence", 400, 0.0),
    ("speech", 900, 330.0),
    ("silence", 500, 0.0),
    ("speech", 1500, 260.0),
    ("silence", 300, 0.0),
]


@dataclass
class PcmClip:
    clip_id: str
    seed: int
    sample_rate: int
    samples: np.ndarray
    segments: list

    @property
    def n_samples(self) -> int:
        return int(self.samples.shape[0])

    @property
    def duration_ms(self) -> int:
        return self.n_samples * 1000 // self.sample_rate

    def digest(self) -> str:
        return hashlib.sha256(self.samples.tobytes()).hexdigest()

    def to_bytes(self) -> bytes:
        return self.samples.astype("<i2").tobytes()


def _speech_block(rng, n, f0, sample_rate, noise_amp):
    t = np.arange(n, dtype=np.float64) / sample_rate
    signal = np.zeros(n, dtype=np.float64)
    for harmonic in (1, 2, 3):
        phase = float(rng.uniform(0.0, 2.0 * np.pi))
        signal += (9000.0 / harmonic) * np.sin(2.0 * np.pi * f0 * harmonic * t + phase)
    edge = min(n // 2, int(0.03 * sample_rate))
    if edge > 0:
        ramp = np.sin(np.linspace(0.0, np.pi / 2.0, edge)) ** 2
        envelope = np.ones(n, dtype=np.float64)
        envelope[:edge] = ramp
        envelope[-edge:] = ramp[::-1]
        signal *= envelope
    signal += rng.normal(0.0, noise_amp * 0.5, n)
    return signal


def synthesize_clip(clip_id=DEFAULT_CLIP_ID, seed=DEFAULT_SEED, segments=None,
                    noise_amp=DEFAULT_NOISE_AMP, sample_rate=SAMPLE_RATE) -> PcmClip:
    """同一 clip_id/seed/参数下逐字节确定。"""
    segments = list(segments if segments is not None else DEFAULT_SEGMENTS)
    rng = np.random.default_rng(seed)
    blocks = []
    for kind, duration_ms, f0 in segments:
        n = int(sample_rate * duration_ms // 1000)
        if kind == "speech":
            blocks.append(_speech_block(rng, n, f0, sample_rate, noise_amp))
        else:
            blocks.append(rng.normal(0.0, noise_amp, n))
    pcm = np.clip(np.round(np.concatenate(blocks)), -32768, 32767).astype("<i2")
    return PcmClip(clip_id=clip_id, seed=seed, sample_rate=sample_rate,
                   samples=pcm, segments=segments)


def speech_intervals_ms(segments=None):
    """由段规格导出语音活动段的毫秒区间（用于生成基准标注）。"""
    segments = list(segments if segments is not None else DEFAULT_SEGMENTS)
    intervals = []
    cursor = 0
    for kind, duration_ms, _f0 in segments:
        if kind == "speech":
            intervals.append((cursor, cursor + duration_ms))
        cursor += duration_ms
    return intervals
