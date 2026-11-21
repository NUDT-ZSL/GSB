"""Workbench configuration. A config is part of every deterministic identity:
the same input plus the same config must reproduce byte-identical results."""
from __future__ import annotations

import dataclasses
import hashlib
import json

IGNORE_CLASS = 255  # uint8 sentinel for the ignore (void) region; 0 = background


@dataclasses.dataclass(frozen=True)
class WorkbenchConfig:
    seed: int = 20241122
    width: int = 64
    height: int = 48
    num_categories: int = 4          # categories 0..num_categories (0=background)
    num_instances: int = 6           # instances painted by the generator
    latency_budget_ms: float = 5.0   # per interactive query
    iou_threshold: float = 0.5       # instance matching threshold for PQ
    precision: float = 1e-6          # declared tolerance for consistency checks
    mapping_version: int = 1         # category/instance mapping-table version
    snapshot_dir: str = "snapshots"
    cache_dir: str = "cache"

    def canonical_dict(self) -> dict:
        return dataclasses.asdict(self)

    def fingerprint(self) -> str:
        blob = json.dumps(self.canonical_dict(), sort_keys=True).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()
