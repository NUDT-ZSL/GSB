from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from shardpipe.storage import StateStore
from shardpipe.workload import parse_workload


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def sample_document() -> dict[str, Any]:
    return json.loads((ROOT / "data" / "sample.json").read_text(encoding="utf-8"))


@pytest.fixture()
def sample_path() -> Path:
    return ROOT / "data" / "sample.json"


def write_document(path: Path, document: dict[str, Any]) -> Path:
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def store_with_document(state_dir: Path, document: dict[str, Any]) -> StateStore:
    workload = parse_workload(copy.deepcopy(document))
    store = StateStore(state_dir)
    store.load(workload)
    return store


def one_dataset_document(
    *,
    sample_count: int = 4,
    sample_bytes: int = 64,
    shard_size: int = 2,
    batch_size: int = 1,
    epochs: int = 1,
    data_parallelism: int = 1,
    prefetch_depth: int = 1,
    allow_shard_reuse: bool = True,
    memory_mib: int = 1,
    time_budget_ms: int = 20,
    step_ms: int = 1,
    base_ms: int = 1,
    bytes_per_ms: int = 4096,
    cache_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "global_seed": 7,
        "datasets": [
            {
                "name": "d",
                "sample_count": sample_count,
                "feature_dim": 4,
                "sample_bytes": sample_bytes,
                "cacheable": True,
                "shard_size": shard_size,
                "version": "v1",
                "generation_params": {"salt": "test"},
            }
        ],
        "cache": {
            "generation": 1,
            "version": "test-cache",
            "entries": cache_entries or [],
        },
        "jobs": [
            {
                "name": "j",
                "dataset": "d",
                "batch_size": batch_size,
                "epochs": epochs,
                "data_parallelism": data_parallelism,
                "prefetch_depth": prefetch_depth,
                "allow_shard_reuse": allow_shard_reuse,
            }
        ],
        "scheduling": {
            "logical_time_budget_ms": time_budget_ms,
            "per_dataset_prefetch_limit": 1,
            "global_memory_budget_mib": memory_mib,
            "cache_capacity_entries": 32,
            "tolerance_ms": 0,
            "logical_clock_step_ms": step_ms,
            "generation_cost": {
                "base_ms": base_ms,
                "bytes_per_ms": bytes_per_ms,
                "hit_ms": 0,
            },
        },
    }
