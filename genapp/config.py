"""生成配置与资源上限.

配置全部来自本地确定性样本文件, 不依赖外部下载、系统时间或任何
未声明随机源. 同一配置对象在任何进程内产生逐字节相同的结果.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Tuple

import numpy as np


@dataclass(frozen=True)
class ResourceLimits:
    """资源上限: 单次生成像素总量 / 单块驻留内存 / 并发生成任务数."""

    max_total_pixels: int = 1 << 20
    max_block_bytes: int = 1 << 22
    max_concurrent_tasks: int = 4


@dataclass(frozen=True)
class GenerationConfig:
    """一次条件生成的全部输入配置."""

    width: int = 96
    height: int = 96
    channels: int = 3
    steps: int = 8
    precision: str = "float32"
    seed: int = 20231123
    cond_dim: int = 16
    dt: float = 0.15
    reaction: float = 0.05
    latency_budget_ms: float = 250.0
    # 估算校准常数: 单像素单步迭代的微秒级成本 (本地循环计时标定).
    cost_per_pixel_step_us: float = 0.40
    # 边界耦合态阈值: 超过则一致性校验档位必须提升到全量逐字节.
    coupling_threshold: int = 4096
    grid_candidates: Tuple[str, ...] = ("1x1", "2x2", "4x4")
    verify_min_tier: int = 1
    anchor_strength: float = 0.0
    limits: ResourceLimits = field(default_factory=ResourceLimits)

    @property
    def dtype(self) -> np.dtype:
        if self.precision == "float64":
            return np.dtype(np.float64)
        if self.precision == "float32":
            return np.dtype(np.float32)
        raise ValueError(f"unsupported precision: {self.precision}")

    @property
    def total_pixels(self) -> int:
        return self.width * self.height

    def to_dict(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "channels": self.channels,
            "steps": self.steps,
            "precision": self.precision,
            "seed": self.seed,
            "cond_dim": self.cond_dim,
            "dt": self.dt,
            "reaction": self.reaction,
            "latency_budget_ms": self.latency_budget_ms,
            "cost_per_pixel_step_us": self.cost_per_pixel_step_us,
            "coupling_threshold": self.coupling_threshold,
            "grid_candidates": list(self.grid_candidates),
            "verify_min_tier": self.verify_min_tier,
            "anchor_strength": self.anchor_strength,
            "limits": {
                "max_total_pixels": self.limits.max_total_pixels,
                "max_block_bytes": self.limits.max_block_bytes,
                "max_concurrent_tasks": self.limits.max_concurrent_tasks,
            },
        }

    @staticmethod
    def from_dict(data: dict) -> "GenerationConfig":
        raw_limits = data.get("limits", {})
        if isinstance(raw_limits, ResourceLimits):
            limits = raw_limits
        else:
            limits = ResourceLimits(**raw_limits)
        kwargs = {k: v for k, v in data.items() if k != "limits"}
        if "grid_candidates" in kwargs:
            kwargs["grid_candidates"] = tuple(kwargs["grid_candidates"])
        return GenerationConfig(limits=limits, **kwargs)

    @staticmethod
    def load(path: str) -> "GenerationConfig":
        with open(path, "r", encoding="utf-8") as fh:
            return GenerationConfig.from_dict(json.load(fh))
