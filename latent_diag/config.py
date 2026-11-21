"""工作台配置：门限策略、资源上限、目录布局。全部为显式声明的确定性参数。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

STRATEGY_STRICT = "strict"
STRATEGY_TOLERANCE_BAND = "tolerance_band"
COMPARE_STRATEGIES = (STRATEGY_STRICT, STRATEGY_TOLERANCE_BAND)


@dataclass
class ThresholdPolicy:
    """边界判定与 top-k 候选的门限策略。"""
    boundary_residual_threshold: float = 1e-8
    decode_candidate_threshold: float = 0.05
    compare_strategy: str = STRATEGY_TOLERANCE_BAND
    tolerance_band: float = 1e-12
    version: int = 1

    def fingerprint(self):
        return {
            "boundary_residual_threshold": self.boundary_residual_threshold,
            "decode_candidate_threshold": self.decode_candidate_threshold,
            "compare_strategy": self.compare_strategy,
            "tolerance_band": self.tolerance_band,
            "version": self.version,
        }


@dataclass
class ResourceLimits:
    """资源上限：潜帧驻留内存、单次分块数、并行任务数。"""
    max_latent_bytes: int = 64 * 1024 * 1024
    max_blocks_per_task: int = 64
    max_parallel_tasks: int = 8


@dataclass
class WorkbenchConfig:
    cond_dim: int = 16
    top_k: int = 5
    quantiles: tuple = (0.5, 0.9, 0.99, 1.0)
    chunk_tolerance: float = 0.0  # float64 下要求逐元素一致，默认容差 0
    threshold_policy: ThresholdPolicy = field(default_factory=ThresholdPolicy)
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)
    sample_dir: str = "samples"
    snapshot_dir: str = "snapshots"
    cache_dir: str = "cache"
    report_dir: str = "reports"

    def to_dict(self):
        data = asdict(self)
        data["quantiles"] = list(self.quantiles)
        return data

    @staticmethod
    def from_dict(data):
        data = dict(data)
        tp = data.pop("threshold_policy", None) or {}
        rl = data.pop("resource_limits", None) or {}
        cfg = WorkbenchConfig(**data)
        cfg.threshold_policy = ThresholdPolicy(**tp)
        cfg.resource_limits = ResourceLimits(**rl)
        if isinstance(cfg.quantiles, list):
            cfg.quantiles = tuple(cfg.quantiles)
        return cfg

    @staticmethod
    def load(path):
        with open(path, "r", encoding="utf-8") as fh:
            return WorkbenchConfig.from_dict(json.load(fh))

    def save(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, sort_keys=True)


def ensure_dirs(cfg, clear_snapshots=False):
    """预留样本/快照/缓存/报告目录；可选清空快照目录使版本号从初始值开始。"""
    import shutil
    for d in (cfg.sample_dir, cfg.cache_dir, cfg.report_dir):
        Path(d).mkdir(parents=True, exist_ok=True)
    snap = Path(cfg.snapshot_dir)
    if clear_snapshots and snap.exists():
        shutil.rmtree(snap)
    snap.mkdir(parents=True, exist_ok=True)
