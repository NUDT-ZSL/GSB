from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from coldstart_ledger.engine import ColdStartEngine  # noqa: E402
from coldstart_ledger.loader import InputBundle, load_inputs  # noqa: E402


BASE_CONFIG: dict[str, Any] = {
    "temp_storage_quota_limit_bytes": 1_000_000,
    "layer_read_bandwidth_bytes_per_second": 1_000_000,
    "round_logical_time_budget_ms": 100,
    "fixed_scale": 1000,
    "error_upper_bound_budget": 8,
    "logical_clock_step_ms": 5,
    "comparison_tolerance_ticks": 2,
    "clock_rollback_threshold_ms": 0,
}


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def write_bundle(
    directory: Path,
    *,
    instances: list[dict[str, Any]] | None = None,
    layers: list[dict[str, Any]] | None = None,
    nodes: list[dict[str, Any]] | None = None,
    config: dict[str, Any] | None = None,
) -> InputBundle:
    directory.mkdir(parents=True, exist_ok=True)
    instances = instances or [
        {
            "instance_id": "i1",
            "service": "svc",
            "priority": 10,
            "requested_ephemeral_bytes": 1000,
            "required_layers": ["sha256:l1"],
            "deadline_ms": 1000,
        }
    ]
    layers = layers or [
        {
            "digest": "sha256:l1",
            "bytes": 1000,
            "node": "node-a",
            "ref_count": 2,
        }
    ]
    nodes = nodes or [
        {
            "node": "node-a",
            "available_ephemeral_bytes": 100_000,
            "image_layer_bytes": 1000,
            "max_concurrent_cold_starts": 2,
        }
    ]
    config_value = dict(BASE_CONFIG)
    if config:
        config_value.update(config)
    write_json(directory / "instances.json", {"instances": instances})
    write_json(directory / "layers.json", {"layers": layers})
    write_json(directory / "nodes.json", {"nodes": nodes})
    write_json(directory / "config.json", {"ledger_config": config_value})
    return load_inputs(directory)


def make_engine(
    directory: Path,
    *,
    instances: list[dict[str, Any]] | None = None,
    layers: list[dict[str, Any]] | None = None,
    nodes: list[dict[str, Any]] | None = None,
    config: dict[str, Any] | None = None,
    db_name: str = "ledger.db",
    crash_hook=None,
) -> ColdStartEngine:
    bundle = write_bundle(
        directory,
        instances=instances,
        layers=layers,
        nodes=nodes,
        config=config,
    )
    return ColdStartEngine(directory / db_name, bundle, crash_hook=crash_hook)


@pytest.fixture
def sample_input_dir() -> Path:
    return ROOT


@pytest.fixture
def sample_bundle(sample_input_dir: Path) -> InputBundle:
    return load_inputs(sample_input_dir)

