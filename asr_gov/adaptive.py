"""基于数据规模的自适应算法取舍器。

精确路径：细帧移、逐帧边界精修、全量跨切片比对 O(n^2)。
逼近路径：粗帧移、分位边界估计、分块比对。
同一观测下决策逐字节一致；路径选择不得改变已定稿切片的毫秒时间轴语义。
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import RecognitionConfig, canonical_json, sha256_hex

PATH_EXACT = "exact"
PATH_APPROX = "approximate"


@dataclass
class ScaleObservation:
    num_slices: int
    num_frames: int
    num_speakers: int
    memory_headroom_bytes: int

    def to_dict(self):
        return {
            "num_slices": self.num_slices,
            "num_frames": self.num_frames,
            "num_speakers": self.num_speakers,
            "memory_headroom_bytes": self.memory_headroom_bytes,
        }


def estimate_comparisons(num_slices: int, path: str, block_size: int) -> int:
    if path == PATH_EXACT:
        return num_slices * (num_slices - 1) // 2
    blocks, rem = divmod(num_slices, block_size)
    full = blocks * (block_size * (block_size - 1) // 2)
    return full + rem * (rem - 1) // 2


@dataclass
class DecisionRecord:
    path: str
    observation: ScaleObservation
    frame_shift_ms: int
    boundary_method: str
    comparison_mode: str
    estimated_comparisons: int
    reasons: list

    def to_dict(self):
        return {
            "path": self.path,
            "observation": self.observation.to_dict(),
            "frame_shift_ms": self.frame_shift_ms,
            "boundary_method": self.boundary_method,
            "comparison_mode": self.comparison_mode,
            "estimated_comparisons": self.estimated_comparisons,
            "reasons": list(self.reasons),
        }

    def canonical(self) -> str:
        return canonical_json(self.to_dict())

    def digest(self) -> str:
        return sha256_hex(self.canonical().encode("utf-8"))


def decide(obs: ScaleObservation, config: RecognitionConfig) -> DecisionRecord:
    exact_pairwise = estimate_comparisons(obs.num_slices, PATH_EXACT, 0)
    reasons = []
    if obs.num_frames > config.exact_max_frames:
        reasons.append(f"frames {obs.num_frames} > exact_max_frames {config.exact_max_frames}")
    if obs.num_slices > config.exact_max_slices:
        reasons.append(f"slices {obs.num_slices} > exact_max_slices {config.exact_max_slices}")
    if exact_pairwise > config.exact_max_pairwise:
        reasons.append(f"pairwise {exact_pairwise} > exact_max_pairwise {config.exact_max_pairwise}")
    if obs.memory_headroom_bytes < config.min_memory_headroom_bytes:
        reasons.append(f"headroom {obs.memory_headroom_bytes} < min {config.min_memory_headroom_bytes}")
    if not reasons:
        return DecisionRecord(
            path=PATH_EXACT,
            observation=obs,
            frame_shift_ms=config.frame_shift_ms,
            boundary_method="per_frame_refine",
            comparison_mode="full_cross_slice",
            estimated_comparisons=exact_pairwise,
            reasons=["within_exact_budget"],
        )
    return DecisionRecord(
        path=PATH_APPROX,
        observation=obs,
        frame_shift_ms=config.frame_shift_ms * config.approx_shift_multiplier,
        boundary_method="quantile_boundary_estimate",
        comparison_mode="blocked",
        estimated_comparisons=estimate_comparisons(
            obs.num_slices, PATH_APPROX, config.approx_block_size),
        reasons=reasons,
    )
