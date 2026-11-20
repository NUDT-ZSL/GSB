"""Configuration model with local JSON loading and deterministic defaults."""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Config:
    precision: str = "float64"
    latency_budget_ms: float = 5000.0
    priority: tuple = ("latency", "precision", "memory")
    block_candidates: tuple = (64, 256, 1024, 4096)
    max_resident_bytes: int = 64 * 1024 * 1024
    max_cache_entries: int = 10000
    max_parallel_recompute: int = 4
    allow_degrade: bool = True
    latency_coeffs: dict = field(
        default_factory=lambda: {
            "element_ns": 0.02,
            "op_us": 5.0,
            "transpose_us": 20.0,
        }
    )
    cache_dir: str = "cache"
    results_dir: str = "results"

    def to_dict(self):
        data = asdict(self)
        data["priority"] = list(self.priority)
        data["block_candidates"] = list(self.block_candidates)
        return data

    @classmethod
    def from_dict(cls, data):
        data = dict(data)
        if "priority" in data:
            data["priority"] = tuple(data["priority"])
        if "block_candidates" in data:
            data["block_candidates"] = tuple(data["block_candidates"])
        return cls(**data)

    @classmethod
    def load(cls, path):
        path = Path(path)
        if not path.exists():
            return cls()
        with path.open("r", encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2, sort_keys=True)

    def with_overrides(self, overrides):
        """Return a copy with per-task policy overrides applied."""
        base = self.to_dict()
        for key, value in (overrides or {}).items():
            if key not in base:
                continue
            if key in ("priority", "block_candidates"):
                base[key] = tuple(value)
            else:
                base[key] = value
        return Config.from_dict(base)
