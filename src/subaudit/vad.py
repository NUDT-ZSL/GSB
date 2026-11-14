"""离线语音发声区间检测（能量 VAD）。

设计目标：
* 完全离线，只依赖 numpy；不引入随机数，同一输入多次运行结果逐毫秒一致。
* 帧级能量判定 + 样本级边界精修，边界可精确到 1 个采样点（约 0.02ms）。
* 短间隙桥接（塞音/停顿）与最小发声时长过滤，避免一句话被切碎。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np


@dataclass(frozen=True)
class VoiceSegment:
    """一段检测到的人声区间，时间单位均为毫秒。"""

    start_ms: float
    end_ms: float

    def as_tuple(self) -> tuple[float, float]:
        return self.start_ms, self.end_ms


# ---- 默认参数（可通过 audit_manifest 覆盖）---------------------------------
DEFAULT_FRAME_MS = 20.0
DEFAULT_HOP_MS = 10.0
# 绝对能量阈值：RMS 低于该值视为静音（归一化到 [-1, 1] 的浮点信号）。
DEFAULT_ABS_RMS = 0.01
# 自适应阈值 = 噪声底估计 * 该倍数，再与绝对阈值取较大值。
DEFAULT_REL_FACTOR = 4.0
# 低于此时长的发声段丢弃（毫秒）。
DEFAULT_MIN_VOICED_MS = 60.0
# 两段发声间隔小于该值则桥接为一段（毫秒）。
DEFAULT_MERGE_GAP_MS = 120.0
# 样本级边界精修时使用的瞬时幅度阈值（相对帧级阈值更灵敏）。
DEFAULT_EDGE_AMP_RATIO = 0.35
# 边界精修搜索半径（毫秒）。
DEFAULT_EDGE_SEARCH_MS = 100.0


def _to_mono_float(samples: np.ndarray) -> np.ndarray:
    """转单声道 float64，取值归一化到 [-1, 1]。"""
    audio = np.asarray(samples)
    if audio.ndim == 2:  # 多声道取平均
        audio = audio.mean(axis=1)
    audio = audio.astype(np.float64, copy=False)
    if np.issubdtype(np.asarray(samples).dtype, np.integer):
        max_val = float(np.iinfo(np.asarray(samples).dtype).max)
        audio = audio / max_val
    # 防御性裁剪，避免个别越界样本影响统计。
    return np.clip(audio, -1.0, 1.0)


def _half_up_round(value: float) -> int:
    """确定性四舍五入（避开 Python banker's rounding，正负值统一半值向上）。"""
    return int(np.floor(np.float64(value) + 0.5))


def samples_to_ms(sample_index: int, sample_rate: int) -> int:
    """采样点序号 -> 毫秒，确定性四舍五入。"""
    return _half_up_round(sample_index * 1000.0 / sample_rate)


def detect_voice_segments(
    samples: np.ndarray,
    sample_rate: int,
    *,
    frame_ms: float = DEFAULT_FRAME_MS,
    hop_ms: float = DEFAULT_HOP_MS,
    abs_rms: float = DEFAULT_ABS_RMS,
    rel_factor: float = DEFAULT_REL_FACTOR,
    min_voiced_ms: float = DEFAULT_MIN_VOICED_MS,
    merge_gap_ms: float = DEFAULT_MERGE_GAP_MS,
    edge_amp_ratio: float = DEFAULT_EDGE_AMP_RATIO,
    edge_search_ms: float = DEFAULT_EDGE_SEARCH_MS,
) -> list[VoiceSegment]:
    """检测整段音频中的所有人声发声区间。

    参数
    ----
    samples: soundfile 读出的音频数组（整数或浮点、单/多声道均可）。
    sample_rate: 采样率（Hz）。

    返回
    ----
    VoiceSegment 列表，按开始时间升序；纯静音返回空列表。
    """
    audio = _to_mono_float(samples)
    n = audio.shape[0]
    if n == 0:
        return []

    frame_len = max(1, _half_up_round(sample_rate * frame_ms / 1000.0))
    hop_len = max(1, _half_up_round(sample_rate * hop_ms / 1000.0))
    min_voiced_samples = sample_rate * min_voiced_ms / 1000.0
    merge_gap_samples = sample_rate * merge_gap_ms / 1000.0
    edge_search = max(1, _half_up_round(sample_rate * edge_search_ms / 1000.0))

    # 1) 分帧并计算 RMS 能量。
    starts = np.arange(0, max(1, n - frame_len + 1), hop_len)
    if starts.size == 0:
        starts = np.array([0])
    rms = np.empty(starts.size, dtype=np.float64)
    for i, start in enumerate(starts):
        frame = audio[start : min(start + frame_len, n)]
        rms[i] = float(np.sqrt(np.mean(frame * frame))) if frame.size else 0.0

    # 2) 阈值：噪声底（低能量分位）自适应 + 绝对下限。
    noise_floor = float(np.percentile(rms, 20))
    threshold = max(abs_rms, noise_floor * rel_factor)
    voiced_mask = rms > threshold
    if not voiced_mask.any():
        return []

    # 3) 帧掩码转成候选区间（样本序号）。
    frame_ends = np.minimum(starts + frame_len, n)
    voiced_idx = np.flatnonzero(voiced_mask)
    groups = np.split(voiced_idx, np.flatnonzero(np.diff(voiced_idx) > 1) + 1)

    edge_threshold = threshold * edge_amp_ratio
    abs_audio = np.abs(audio)
    segments: list[list[int]] = []
    for group in groups:
        rough_start = int(starts[group[0]])
        rough_end = int(frame_ends[group[-1]])

        # 4) 样本级精修：在粗边界附近找第一个/最后一个超过幅度阈值的样本。
        lo = max(0, rough_start - edge_search)
        hi = min(n, rough_start + frame_len + edge_search)
        start_hits = np.flatnonzero(abs_audio[lo:hi] > edge_threshold)
        refined_start = int(lo + start_hits[0]) if start_hits.size else rough_start

        lo = max(0, rough_end - frame_len - edge_search)
        hi = min(n, rough_end + edge_search)
        end_hits = np.flatnonzero(abs_audio[lo:hi] > edge_threshold)
        # 发声区间为 [start, end)，end 取最后一个超阈样本之后。
        refined_end = int(lo + end_hits[-1] + 1) if end_hits.size else rough_end
        if refined_end <= refined_start:
            continue
        segments.append([refined_start, refined_end])

    # 5) 桥接短间隙。
    merged: list[list[int]] = []
    for seg in segments:
        if merged and seg[0] - merged[-1][1] <= merge_gap_samples:
            merged[-1][1] = seg[1]
        else:
            merged.append([seg[0], seg[1]])

    # 6) 过滤过短发声段并转为毫秒。
    result: list[VoiceSegment] = []
    for start_sample, end_sample in merged:
        if end_sample - start_sample < min_voiced_samples:
            continue
        result.append(
            VoiceSegment(
                start_ms=samples_to_ms(start_sample, sample_rate),
                end_ms=samples_to_ms(end_sample, sample_rate),
            )
        )
    return result


def find_voice_in_window(
    segments: Sequence[VoiceSegment],
    window_start_ms: float,
    window_end_ms: float,
) -> VoiceSegment | None:
    """在给定时间窗内找发声段：优先重叠最大者，否则取最近者。"""
    if not segments:
        return None
    best: VoiceSegment | None = None
    best_key: tuple[float, float] | None = None
    for seg in segments:
        overlap = max(
            0.0, min(seg.end_ms, window_end_ms) - max(seg.start_ms, window_start_ms)
        )
        if overlap > 0:
            # 重叠场景按重叠时长降序、段长升序（优先短句）排序。
            key = (1.0, -overlap, (seg.end_ms - seg.start_ms))
        else:
            # 无重叠场景按到时间窗的距离升序。
            distance = max(
                window_start_ms - seg.end_ms,
                seg.start_ms - window_end_ms,
                0.0,
            )
            key = (0.0, -distance, 0.0)
        if best_key is None or key > best_key:
            best_key = key
            best = seg
    return best
