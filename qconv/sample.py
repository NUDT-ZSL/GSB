"""Generate the self-contained deterministic sample workspace."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import numpy as np

from .packs import write_pack
from .util import atomic_write_json


DEFAULT_SEED = 20261117


def _random_ints(seed: int, shape: tuple[int, ...], low: int, high: int) -> np.ndarray:
    rng = random.Random(seed)
    count = int(np.prod(shape))
    values = [rng.randint(low, high) for _ in range(count)]
    return np.asarray(values, dtype=np.int64).reshape(shape)


def generate_samples(destination: str | Path, *, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    packs_dir = root / "packs"
    packs_dir.mkdir(parents=True, exist_ok=True)

    input_shape = (1, 4, 6, 6)
    activation_scale = 0.03125
    input_values = _random_ints(seed ^ 0x11, input_shape, -10, 10)
    write_pack(packs_dir / "input.qpak", input_values, "int8")

    node_specs: list[dict[str, Any]] = [
        {
            "id": "branch_l",
            "input": "input",
            "output": "branch_l",
            "in_channels": 4,
            "out_channels": 3,
            "kernel": [3, 3],
            "stride": [2, 2],
            "padding": [1, 1],
            "weight_pack": "branch_l",
            "weight_shape": [3, 4, 3, 3],
            "weight_scale": 0.015625,
            "seed": seed ^ 0x21,
        },
        {
            "id": "branch_r",
            "input": "input",
            "output": "branch_r",
            "in_channels": 4,
            "out_channels": 3,
            "kernel": [1, 1],
            "stride": [1, 1],
            "padding": [0, 0],
            "weight_pack": "branch_r",
            "weight_shape": [3, 4, 1, 1],
            "weight_scale": 0.01,
            "seed": seed ^ 0x31,
        },
        {
            "id": "stem",
            "input": "input",
            "output": "stem",
            "in_channels": 4,
            "out_channels": 4,
            "kernel": [3, 3],
            "stride": [1, 1],
            "padding": [1, 1],
            "weight_pack": "stem",
            "weight_shape": [4, 4, 3, 3],
            "weight_scale": 0.02,
            "seed": seed ^ 0x41,
        },
        {
            "id": "tail",
            "input": "stem",
            "output": "tail",
            "in_channels": 4,
            "out_channels": 2,
            "kernel": [3, 3],
            "stride": [1, 1],
            "padding": [1, 1],
            "weight_pack": "tail",
            "weight_shape": [2, 4, 3, 3],
            "weight_scale": 0.02,
            "seed": seed ^ 0x51,
        },
    ]

    weights_manifest: dict[str, Any] = {}
    graph_nodes: list[dict[str, Any]] = []
    for spec in node_specs:
        shape = tuple(spec["weight_shape"])
        values = _random_ints(int(spec["seed"]), shape, -7, 7)
        pack_name = f"{spec['weight_pack']}.weight.qpak"
        write_pack(packs_dir / pack_name, values, "int8")
        weights_manifest[spec["weight_pack"]] = {
            "path": f"packs/{pack_name}",
            "shape": list(shape),
            "dtype": "int8",
            "bit_width": 8,
            "scale": spec["weight_scale"],
        }
        graph_nodes.append(
            {
                "id": spec["id"],
                "input": spec["input"],
                "output": spec["output"],
                "in_channels": spec["in_channels"],
                "out_channels": spec["out_channels"],
                "kernel": spec["kernel"],
                "stride": spec["stride"],
                "padding": spec["padding"],
                "weight_pack": spec["weight_pack"],
                "weight_shape": spec["weight_shape"],
                "weight_bit_width": 8,
                "weight_scale": spec["weight_scale"],
                "activation_bit_width": 8,
                "activation_scale": activation_scale,
                "accumulator_bits": 32,
                "overflow_policy": "error",
            }
        )

    edges = [
        {"from": "input", "to": "branch_l"},
        {"from": "input", "to": "branch_r"},
        {"from": "input", "to": "stem"},
        {"from": "stem", "to": "tail"},
    ]
    graph = {
        "schema_version": 1,
        "graph_version": 1,
        "input": {
            "name": "input",
            "shape": list(input_shape),
            "pack": "input",
            "bit_width": 8,
            "scale": activation_scale,
            "dtype": "int8",
        },
        "nodes": graph_nodes,
        "edges": edges,
        "outputs": ["branch_l", "branch_r", "tail"],
    }
    pack_manifest = {
        "schema_version": 1,
        "input": {
            "path": "packs/input.qpak",
            "shape": list(input_shape),
            "dtype": "int8",
            "bit_width": 8,
            "scale": activation_scale,
            "seed": seed ^ 0x11,
        },
        "weights": weights_manifest,
    }
    schedule = {
        "max_concurrency": 3,
        "global_mac_budget": 100000,
        "per_node_mac_quota": 20000,
        "cache_residency_bytes": 1048576,
        "snapshot_commit_interval": 1,
        "first_frame_response_budget_ms": 2000,
        "rounding_mode": "half_even",
        "dequant_decimals": 12,
        "quantization_enabled": True,
        "debug_node_delay_ms": 0,
    }
    compact_budget = dict(schedule)
    compact_budget.update(
        {
            "global_mac_budget": 6000,
            "per_node_mac_quota": 3000,
        }
    )

    injections: dict[str, Any] = {
        "schema_version": 1,
        "items": [],
    }
    injection_nan = {
        "schema_version": 1,
        "items": [
            {
                "id": "INJ-NAN-01",
                "type": "non_finite_scale",
                "node": "branch_l",
                "target": "weight",
            }
        ],
    }
    injection_overflow = {
        "schema_version": 1,
        "items": [
            {
                "id": "INJ-OVF-01",
                "type": "accumulator_overflow",
                "node": "branch_r",
                "value": 2,
            }
        ],
    }
    injection_cache_zero = {
        "schema_version": 1,
        "items": [
            {
                "id": "INJ-CACHE-01",
                "type": "cache_budget_zero",
            }
        ],
    }
    injection_combined = {
        "schema_version": 1,
        "items": [
            {
                "id": "INJ-NAN-01",
                "type": "non_finite_scale",
                "node": "branch_l",
                "target": "weight",
            },
            {
                "id": "INJ-BW-01",
                "type": "bit_width_out_of_range",
                "node": "branch_r",
                "target": "activation",
                "value": 17,
            },
            {
                "id": "INJ-OVF-01",
                "type": "accumulator_overflow",
                "node": "stem",
                "value": 2,
            },
            {"id": "INJ-CACHE-01", "type": "cache_budget_zero"},
        ],
    }

    atomic_write_json(root / "graph.json", graph)
    atomic_write_json(root / "packs.json", pack_manifest)
    atomic_write_json(root / "schedule.json", schedule)
    atomic_write_json(root / "schedule-compact-budget.json", compact_budget)
    atomic_write_json(root / "injection.json", injections)
    atomic_write_json(root / "injection-nan.json", injection_nan)
    atomic_write_json(root / "injection-overflow.json", injection_overflow)
    atomic_write_json(root / "injection-cache-zero.json", injection_cache_zero)
    atomic_write_json(root / "injection-combined.json", injection_combined)

    readme = """QConv deterministic sample
==========================

Files:
- graph.json: four-node DAG with independent branches and one downstream node
- packs.json: shape/dtype/scale descriptors for QPAK files
- packs/*.qpak: QPK1 little-endian signed integer containers
- schedule.json: normal scheduling configuration
- schedule-compact-budget.json: intentionally tight budget sample
- injection*.json: isolated failure injection examples

Run:
  python -m qconv run --samples . --session session --report report.json
"""
    (root / "README.txt").write_text(readme, encoding="utf-8")
    return {
        "destination": str(root),
        "seed": seed,
        "graph": str(root / "graph.json"),
        "packs": str(root / "packs.json"),
        "schedule": str(root / "schedule.json"),
    }


def load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))

