"""PCM 分帧特征与切片边界初判节点（DAG 第 1、2 层）。

帧边界以样本索引精确确定；能量用 int64 平方和，过零率为整数计数，
保证同 clip_id/seed/配置下逐字节一致。平局按起始样本索引升序打破。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import numpy as np

NOISE_BASELINE_PERCENTILE = 20


@dataclass
class FrameFeatures:
    sample_rate: int
    frame_length: int
    frame_shift: int
    starts: np.ndarray
    energy: np.ndarray
    zcr: np.ndarray

    @property
    def n_frames(self) -> int:
        return int(self.starts.shape[0])

    def resident_bytes(self) -> int:
        return int(self.starts.nbytes + self.energy.nbytes + self.zcr.nbytes)

    def digest(self) -> str:
        h = hashlib.sha256()
        h.update(self.starts.tobytes())
        h.update(self.energy.tobytes())
        h.update(self.zcr.tobytes())
        h.update(json.dumps([self.sample_rate, self.frame_length, self.frame_shift]).encode())
        return h.hexdigest()


def frame_pcm(samples, sample_rate: int, frame_length_ms: int,
              frame_shift_ms: int) -> FrameFeatures:
    frame_length = sample_rate * frame_length_ms // 1000
    frame_shift = sample_rate * frame_shift_ms // 1000
    n_samples = int(samples.shape[0])
    if n_samples < frame_length:
        starts = np.zeros(0, dtype=np.int64)
        return FrameFeatures(sample_rate, frame_length, frame_shift, starts,
                             np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64))
    n_frames = 1 + (n_samples - frame_length) // frame_shift
    starts = np.arange(n_frames, dtype=np.int64) * frame_shift
    energy = np.zeros(n_frames, dtype=np.int64)
    zcr = np.zeros(n_frames, dtype=np.int64)
    pcm = samples.astype(np.int64)
    for i in range(n_frames):
        s = int(starts[i])
        window = pcm[s:s + frame_length]
        energy[i] = int(np.dot(window, window))
        signs = np.signbit(window)
        zcr[i] = int(np.count_nonzero(signs[1:] != signs[:-1]))
    return FrameFeatures(sample_rate, frame_length, frame_shift, starts, energy, zcr)


@dataclass
class CandidateSlice:
    start_ms: int
    end_ms: int
    start_sample: int
    end_sample: int
    total_energy: int

    def to_dict(self):
        return {
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "start_sample": self.start_sample,
            "end_sample": self.end_sample,
            "total_energy": self.total_energy,
        }


def detect_boundaries(features: FrameFeatures, high_mult: float, low_mult: float,
                      min_slice_ms: int, max_slices: int) -> list:
    """滞回判决：能量升破高门限进入活动态，跌破低门限退出；合并为候选切片。"""
    n = features.n_frames
    if n == 0:
        return []
    baseline = float(np.percentile(features.energy, NOISE_BASELINE_PERCENTILE))
    high = baseline * high_mult
    low = baseline * low_mult
    segments = []
    active = False
    seg_start = 0
    for i in range(n):
        e = float(features.energy[i])
        if not active and e > high:
            active = True
            seg_start = i
        elif active and e < low:
            segments.append((seg_start, i))
            active = False
    if active:
        segments.append((seg_start, n))
    sr = features.sample_rate
    min_frames = max(1, (min_slice_ms * sr // 1000) // features.frame_shift)
    candidates = []
    for first, last in segments:
        if last - first < min_frames:
            continue
        start_sample = int(features.starts[first])
        end_sample = int(features.starts[last - 1]) + features.frame_length
        total_energy = int(features.energy[first:last].sum())
        start_ms = start_sample * 1000 // sr
        end_ms = (end_sample * 1000 + sr - 1) // sr
        candidates.append(CandidateSlice(start_ms, end_ms, start_sample, end_sample, total_energy))
    if len(candidates) > max_slices:
        ranked = sorted(candidates, key=lambda c: (-c.total_energy, c.start_sample))
        candidates = sorted(ranked[:max_slices], key=lambda c: c.start_sample)
    return candidates
