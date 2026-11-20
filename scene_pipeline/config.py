"""Pipeline configuration objects and sample configuration file support."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict


@dataclass
class ICPConfig:
    max_iterations: int = 20
    tolerance: float = 1e-8
    # Hard upper bound on nearest-neighbour comparisons for one scene.
    max_comparisons: int = 2_000_000
    # Per-iteration cap applied once the comparison budget is exhausted.
    min_iterations: int = 3


@dataclass
class GeometryConfig:
    default_face_budget: int = 128
    grid_resolution: int = 8


@dataclass
class TextureConfig:
    resolution: int = 16
    tolerance: float = 0.25


@dataclass
class MemoryConfig:
    # Shared resident-memory budget expressed in abstract "memory units".
    resident_budget_units: int = 4_096
    max_registration_comparisons: int = 2_000_000
    # Ordered downgrade levers; earlier levers are applied first.
    downgrade_order: tuple = (
        "texture_resolution",
        "face_budget",
        "icp_iterations",
    )


@dataclass
class PriorityRule:
    # Higher priority value wins; ties broken by scene_id ascending.
    order: str = "priority_desc"
    tie_breaker: str = "scene_id_asc"


@dataclass
class PipelineConfig:
    workers: int = 2
    scenes_per_batch: int = 4
    target_face_budget: int = 128
    icp: ICPConfig = field(default_factory=ICPConfig)
    geometry: GeometryConfig = field(default_factory=GeometryConfig)
    texture: TextureConfig = field(default_factory=TextureConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    priority: PriorityRule = field(default_factory=PriorityRule)
    # Deterministic PRNG seed source; no other randomness source is permitted.
    master_seed: int = 20261121

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["memory"]["downgrade_order"] = list(self.memory.downgrade_order)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PipelineConfig":
        data = dict(data)
        data["icp"] = ICPConfig(**data.get("icp", {}))
        data["geometry"] = GeometryConfig(**data.get("geometry", {}))
        data["texture"] = TextureConfig(**data.get("texture", {}))
        memory_data = dict(data.get("memory", {}))
        if "downgrade_order" in memory_data:
            memory_data["downgrade_order"] = tuple(memory_data["downgrade_order"])
        data["memory"] = MemoryConfig(**memory_data)
        data["priority"] = PriorityRule(**data.get("priority", {}))
        return cls(**data)

    @classmethod
    def load(cls, path: str) -> "PipelineConfig":
        with open(path, "r", encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")
