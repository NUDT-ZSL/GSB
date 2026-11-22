"""Pipeline configuration: paths, tolerances and resource limits."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict


SUPPORTED_ACC_DTYPES = ("int32",)


@dataclass
class ResourceLimits:
    """Configurable resource ceilings for a single incremental commit."""
    max_recompute_jobs: int = 64
    max_block_resident_bytes: int = 8 * 1024 * 1024
    max_parallel_nodes: int = 4


@dataclass
class Tolerances:
    """Per-level max-abs tolerances for the deviation spectrum."""
    quant_input: float = 0.05
    fixed_point_mac: float = 1.0
    accumulator_reduce: float = 0.5
    dequant_output: float = 2.0


@dataclass
class Config:
    cache_dir: str = "run/cache"
    snapshot_dir: str = "run/snapshots"
    state_dir: str = "run/state"
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    tolerances: Tolerances = field(default_factory=Tolerances)

    @staticmethod
    def load(path):
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        cfg = Config()
        for key in ("cache_dir", "snapshot_dir", "state_dir"):
            if key in raw:
                setattr(cfg, key, raw[key])
        if "limits" in raw:
            cfg.limits = ResourceLimits(**raw["limits"])
        if "tolerances" in raw:
            cfg.tolerances = Tolerances(**raw["tolerances"])
        return cfg

    def save(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2, sort_keys=True)

    def ensure_dirs(self):
        for path in (self.cache_dir, self.snapshot_dir, self.state_dir):
            os.makedirs(path, exist_ok=True)
