"""鼓点（起振点）检出。

算法（纯标准库，离线确定性运行）：

1. 以 20 ms 为窗、10 ms 为帧移对单声道采样做分帧 RMS 能量；
2. 能量正向突变 ``novelty = max(0, rms[n] - rms[n-1])`` 作为起振特征，
   首帧取其自身能量，保证落在 0 时刻的鼓点不会被漏掉；
3. 自适应阈值 = 全曲 novelty 均值 + 3 倍标准差，并叠加绝对下限与
   噪声底能量门，避免在静音 / 弱噪段落误检；
4. 取局部极大值作为候选，相邻间隔小于 ``min_gap_ms`` 的候选按
   "同一节奏点" 聚类，整簇只保留 novelty 最大者（需求 2 的合并要求）；
5. 对每个保留下来的帧在 ±10 ms 窗口内做采样级起振点修正，
   把定位误差从"帧级"收敛到"采样级"。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .audio import AudioInfo

#: 分析窗长 / 帧移，毫秒。需求 2 中"一帧"即指此分析帧（默认 10 ms）。
DEFAULT_FRAME_MS = 20
DEFAULT_HOP_MS = 10
#: 相邻两个节奏点允许的最小间隔，小于该值视为重复检出
DEFAULT_MIN_GAP_MS = 120
#: novelty 自适应阈值的绝对下限
NOVELTY_FLOOR = 0.01
#: 起振帧能量相对噪声底的倍数门限
NOISE_GATE_RATIO = 20.0
NOISE_GATE_FLOOR = 0.02


@dataclass(frozen=True)
class Beat:
    """一个节奏点。"""

    time_ms: int
    """鼓点时间，毫秒，四舍五入到整数。"""
    strength: float
    """该点的 novelty 强度，仅用于排查，不参与对齐计算。"""

    def __lt__(self, other: "Beat") -> bool:
        return self.time_ms < other.time_ms


def frame_rms(samples: list[float], sample_rate: int, frame_ms: int, hop_ms: int):
    """生成分帧 RMS 序列，返回 ``(rms_list, frame_size, hop_size)``。"""
    frame_size = max(1, round(sample_rate * frame_ms / 1000))
    hop_size = max(1, round(sample_rate * hop_ms / 1000))
    n = len(samples)
    rms_values: list[float] = []
    start = 0
    while start + frame_size <= n:
        total = 0.0
        for j in range(start, start + frame_size):
            v = samples[j]
            total += v * v
        rms_values.append(math.sqrt(total / frame_size))
        start += hop_size
    # 末尾不足一整帧的尾巴，单独成帧，保证结尾鼓点可被覆盖
    if start < n:
        tail = samples[start:]
        total = sum(v * v for v in tail)
        rms_values.append(math.sqrt(total / len(tail)))
    return rms_values, frame_size, hop_size


def _refine_onset(
    samples: list[float],
    frame_center: int,
    window: int,
    sample_rate: int,
) -> int:
    """在帧中心 ±window 采样内寻找最大正向差分，返回修正后的毫秒时间。"""
    lo = max(1, frame_center - window)
    hi = min(len(samples), frame_center + window + 1)
    best_sample = frame_center
    best_diff = 0.0
    prev = samples[lo - 1]
    for j in range(lo, hi):
        cur = samples[j]
        diff = cur - prev
        if diff > best_diff:
            best_diff = diff
            best_sample = j
        prev = cur
    return round(best_sample * 1000 / sample_rate)


def detect_beats(
    audio: AudioInfo,
    frame_ms: int = DEFAULT_FRAME_MS,
    hop_ms: int = DEFAULT_HOP_MS,
    min_gap_ms: int = DEFAULT_MIN_GAP_MS,
) -> list[Beat]:
    """检出鼓点，返回按毫秒升序、且已合并重复检出的节奏点列表。"""
    if hop_ms <= 0 or frame_ms <= 0:
        raise ValueError("frame_ms 与 hop_ms 必须为正整数")
    if min_gap_ms < 0:
        raise ValueError("min_gap_ms 不能为负数")

    samples = audio.samples
    sr = audio.sample_rate
    rms_values, frame_size, hop_size = frame_rms(samples, sr, frame_ms, hop_ms)
    n_frames = len(rms_values)

    # --- 起振特征：能量正向突变，首帧取自身能量 ---------------------------
    novelty = [rms_values[0]]
    for i in range(1, n_frames):
        novelty.append(max(0.0, rms_values[i] - rms_values[i - 1]))

    # --- 自适应阈值 + 绝对下限 --------------------------------------------
    mean_nov = sum(novelty) / n_frames
    var_nov = sum((x - mean_nov) ** 2 for x in novelty) / n_frames
    adaptive = mean_nov + 3.0 * math.sqrt(var_nov)
    threshold = max(adaptive, NOVELTY_FLOOR)

    # 噪声底：取全部帧能量的最小值（静音段），再叠加绝对门限
    noise_floor = min(rms_values)
    energy_gate = max(noise_floor * NOISE_GATE_RATIO, NOISE_GATE_FLOOR)

    # --- 局部极大值候选 ----------------------------------------------------
    candidates: list[tuple[int, float]] = []  # (frame_index, novelty)
    for i in range(n_frames):
        if novelty[i] < threshold or rms_values[i] < energy_gate:
            continue
        left = novelty[i - 1] if i > 0 else -1.0
        right = novelty[i + 1] if i + 1 < n_frames else -1.0
        if novelty[i] >= left and novelty[i] >= right:
            candidates.append((i, novelty[i]))

    # --- 过近候选聚类合并：间隔 < min_gap_ms 的整簇保留 novelty 最大者 -----
    gap_frames = max(1, math.ceil(min_gap_ms / hop_ms))
    merged: list[tuple[int, float]] = []
    cluster: list[tuple[int, float]] = []

    def flush_cluster() -> None:
        if cluster:
            merged.append(max(cluster, key=lambda item: item[1]))

    for cand in candidates:
        if cluster and cand[0] - cluster[-1][0] < gap_frames:
            cluster.append(cand)
        else:
            flush_cluster()
            cluster = [cand]
    flush_cluster()

    # --- 采样级起振点修正 --------------------------------------------------
    beats: list[Beat] = []
    for frame_index, strength in merged:
        center = frame_index * hop_size + frame_size // 2
        time_ms = _refine_onset(samples, center, frame_size // 2, sr)
        beats.append(Beat(time_ms=time_ms, strength=round(strength, 6)))

    beats.sort()
    # 修正后理论上仍为升序，做一次去重兜底
    unique: list[Beat] = []
    for beat in beats:
        if not unique or beat.time_ms != unique[-1].time_ms:
            unique.append(beat)
    return unique
