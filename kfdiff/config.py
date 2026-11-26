"""Configuration loading and workspace path layout."""
from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


DEFAULT_CONFIG: Dict[str, Any] = {
    "schema_version": 1,
    "master_seed": 20261127,
    "embedding_dim": 24,
    "vocab_seed": 7717,
    "diffusion": {
        "beta_start": 0.00085,
        "beta_end": 0.012,
        "condition_scale": 1.15,
        "coupling_weight": 0.35,
        "normalization_eps": 1.0e-5,
        "quant_low_pct": 2.0,
        "quant_high_pct": 98.0,
    },
    "block_grid_candidates": [1, 2, 3, 4, 6],
    "first_frame_budget_ms": 18.0,
    "consistency_risk_limit": 0.30,
    "boundary_risk_weight": 0.085,
    "coupling_width": 1,
    "priority_order": ["block_grid", "first_frame_budget", "diffusion_strength"],
    "approximation_levels": [1.0, 0.75, 0.5],
    "latency_model": {
        "base_ms": 1.5,
        "coef_keyframe_ms": 1.8,
        "coef_pixel_step_ms": 0.00012,
        "coef_coupling_link_ms": 0.15,
    },
    "memory": {
        "max_clip_resident_bytes": 256 * 1024 * 1024,
        "max_parallel_jobs": 2,
        "on_parallel_limit": "serialize",
    },
    "generation": {
        "parallel_blocks": True,
        "store_full_trajectory": True,
    },
}


@dataclass
class Workspace:
    root: Path

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def records_dir(self) -> Path:
        return self.root / "records"

    @property
    def web_dir(self) -> Path:
        return self.root / "web"

    @property
    def tasks_path(self) -> Path:
        return self.data_dir / "tasks.jsonl"

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.json"

    @property
    def revision_events_path(self) -> Path:
        return self.data_dir / "revision_events.json"

    @property
    def applied_revisions_path(self) -> Path:
        return self.data_dir / "applied_revisions.json"

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    @property
    def object_dir(self) -> Path:
        return self.cache_dir / "objects"

    @property
    def snapshot_dir(self) -> Path:
        return self.cache_dir / "snapshots"

    @property
    def db_path(self) -> Path:
        return self.cache_dir / "index.sqlite3"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.records_dir,
            self.cache_dir,
            self.object_dir,
            self.snapshot_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def load_config(workspace: Workspace, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Load config.json merged over :data:`DEFAULT_CONFIG`.

    The merge is recursive for nested dicts.  ``overrides`` are applied last
    and are used by tests / CLI flags.
    """
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if workspace.config_path.exists():
        on_disk = json.loads(workspace.config_path.read_text(encoding="utf-8"))
        _deep_update(cfg, on_disk)
    if overrides:
        _deep_update(cfg, overrides)
    return cfg


def _deep_update(base: Dict[str, Any], extra: Dict[str, Any]) -> None:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = copy.deepcopy(value)


def find_workspace(start: Optional[os.PathLike] = None) -> Workspace:
    """Resolve the workspace root (defaults to current working directory)."""
    return Workspace(Path(start or Path.cwd()).resolve())
