"""测试公共工具: 小型确定性配置与临时快照存储."""

from __future__ import annotations

import tempfile

from genapp.config import GenerationConfig, ResourceLimits
from genapp.snapshot import SnapshotStore

TOKENS = ["amber", "dune", "dune", "reef", "tide"]


def small_cfg(**overrides) -> GenerationConfig:
    cfg = GenerationConfig(
        width=48,
        height=48,
        channels=3,
        steps=5,
        precision="float32",
        seed=7,
        latency_budget_ms=1000.0,
        grid_candidates=("1x1", "2x2", "4x4"),
        limits=ResourceLimits(
            max_total_pixels=1 << 20,
            max_block_bytes=1 << 22,
            max_concurrent_tasks=4,
        ),
    )
    if overrides:
        cfg = GenerationConfig.from_dict({**cfg.to_dict(), **overrides})
    return cfg


def temp_store():
    tmp = tempfile.TemporaryDirectory()
    return tmp, SnapshotStore(tmp.name)
