"""切片的音频一致性校验与资源上限。

校验：越出音频长度 E_AUDIO_RANGE、零长度 E_EMPTY_SLICE、
区间内无可观测语音活动 E_NO_ACTIVITY。
上限：单样本切片数、驻留特征内存、并行重算分块数；
超限返回 E_RESOURCE_LIMIT 或降级并标记降级类别，
降级不得改变已通过的校验结论。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ResourceLimit, AudioRange, EmptySlice, NoActivity
from .framing import FrameFeatures, NOISE_BASELINE_PERCENTILE

DEGRADE_FEATURE_DOWNSAMPLE = "resident_feature_downsample"
DEGRADE_CHUNK_MERGE = "chunk_merge"


@dataclass
class ValidationReport:
    checked: int = 0
    verdicts: dict = field(default_factory=dict)
    degradations: list = field(default_factory=list)
    feature_stride: int = 1
    recompute_chunks: int = 1

    def to_dict(self):
        return {
            "checked": self.checked,
            "verdicts": self.verdicts,
            "degradations": self.degradations,
            "feature_stride": self.feature_stride,
            "recompute_chunks": self.recompute_chunks,
        }


def _slice_has_activity(features: FrameFeatures, low_threshold: float,
                        start_sample: int, end_sample: int, stride: int) -> bool:
    starts = features.starts
    energy = features.energy
    frame_end = starts + features.frame_length
    overlap = (starts < end_sample) & (frame_end > start_sample)
    if stride > 1:
        idx = np_nonzero_stride(overlap, stride)
    else:
        idx = overlap
    if not idx.any():
        return False
    return bool((energy[idx] >= low_threshold).any())


def np_nonzero_stride(mask, stride):
    import numpy as np
    idx = np.flatnonzero(mask)
    kept = idx[::stride]
    out = np.zeros_like(mask)
    out[kept] = True
    return out


def validate_slices(slices, features: FrameFeatures, n_samples: int,
                    low_mult: float, limits, requested_chunks: int = 1,
                    allow_degrade: bool = True, clip_id: str = None) -> ValidationReport:
    """limits 为 RecognitionConfig（含 max_* 字段）。逐切片校验，首个错误即抛出。"""
    if len(slices) > limits.max_slices_per_clip:
        raise ResourceLimit(
            f"切片数 {len(slices)} 超过上限 {limits.max_slices_per_clip}",
            clip_id=clip_id,
            details={"limit": "max_slices_per_clip", "observed": len(slices)},
        )
    report = ValidationReport()
    resident = features.resident_bytes()
    stride = 1
    if resident > limits.max_resident_feature_bytes:
        if not allow_degrade:
            raise ResourceLimit(
                f"驻留特征内存 {resident} 超过上限 {limits.max_resident_feature_bytes}",
                details={"limit": "max_resident_feature_bytes", "observed": resident},
            )
        stride = max(2, resident // limits.max_resident_feature_bytes + 1)
        report.degradations.append({
            "category": DEGRADE_FEATURE_DOWNSAMPLE,
            "detail": f"特征驻留 {resident}B 超限，活动性检测按 stride={stride} 抽样；"
                      f"已通过的 range/empty 结论不变",
        })
    chunks = requested_chunks
    if chunks > limits.max_recompute_chunks:
        if not allow_degrade:
            raise ResourceLimit(
                f"重算分块数 {chunks} 超过上限 {limits.max_recompute_chunks}",
                details={"limit": "max_recompute_chunks", "observed": chunks},
            )
        chunks = limits.max_recompute_chunks
        report.degradations.append({
            "category": DEGRADE_CHUNK_MERGE,
            "detail": f"重算分块 {requested_chunks} 合并为 {chunks}",
        })
    report.feature_stride = stride
    report.recompute_chunks = chunks
    import numpy as np
    baseline = float(np.percentile(features.energy, NOISE_BASELINE_PERCENTILE)) if features.n_frames else 0.0
    low_threshold = baseline * low_mult
    sr = features.sample_rate
    for index, s in enumerate(slices):
        location = f"slice[{index}] utterance_id={s.utterance_id}"
        start_sample = s.start_us * sr // 1_000_000
        end_sample = (s.end_us * sr + 1_000_000 - 1) // 1_000_000
        if s.end_us <= s.start_us:
            raise EmptySlice("零长度切片", clip_id=clip_id, location=location)
        if end_sample > n_samples or start_sample < 0:
            raise AudioRange(
                f"切片区间 [{start_sample},{end_sample}) 越出音频长度 {n_samples}",
                clip_id=clip_id, location=location,
            )
        if not _slice_has_activity(features, low_threshold, start_sample, end_sample, stride):
            raise NoActivity("区间内无可观测语音活动", clip_id=clip_id, location=location)
        report.verdicts[s.utterance_id] = "ok"
        report.checked += 1
    return report
