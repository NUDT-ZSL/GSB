from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def sample_bundle() -> dict[str, Any]:
    return json.loads((ROOT / "sample" / "bundle.json").read_text(encoding="utf-8"))


@pytest.fixture
def tiny_bundle() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "model": {
            "model_id": "tiny",
            "layers": [{"name": "layer", "parameters": 2_000_000}],
            "input_tokens": 4,
            "output_tokens": 4,
            "bytes_per_parameter": 2,
            "activation_bytes_per_token": 8192,
            "latency_ms_per_compute_unit": 10.0,
        },
        "replica_versions": [
            {
                "version": 1,
                "effective_at_ms": 0,
                "replicas": [
                    {
                        "replica_id": "r0",
                        "compute_units": 1.0,
                        "memory_mib": 64,
                        "batch_window_ms": 20,
                        "max_batch_size": 3,
                        "max_sequence_length": 32,
                        "max_concurrent_batches": 2,
                    }
                ],
            }
        ],
        "requests": [
            {
                "seq": 1,
                "arrival_ms": 0,
                "priority": 0,
                "input_tokens": 4,
                "max_output_tokens": 4,
                "deadline_ms": 100,
            },
            {
                "seq": 2,
                "arrival_ms": 3,
                "priority": 2,
                "input_tokens": 4,
                "max_output_tokens": 4,
                "deadline_ms": 100,
            },
            {
                "seq": 3,
                "arrival_ms": 5,
                "priority": 1,
                "input_tokens": 6,
                "max_output_tokens": 4,
                "deadline_ms": 100,
            },
        ],
        "precision": {
            "compute_scale": 1_000_000,
            "memory_scale": 1_024,
            "latency_scale": 1_000,
            "tolerance": 1.0,
            "rounding": "half_up",
        },
        "scheduler": {
            "logical_time_budget_ms": 100,
            "max_concurrent_batches": 2,
        },
        "execution": {
            "mode": "deterministic",
            "fail_batches": [],
            "latency_overhead_ms": 0,
        },
    }


@pytest.fixture
def clone_bundle():
    def clone(value: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(value)

    return clone
