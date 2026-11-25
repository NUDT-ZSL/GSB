#!/usr/bin/env python3
"""Generate deterministic local fixtures without external dependencies."""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any


def _dump(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_fixture(seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    layer_names = (
        "embedding",
        "attention_0",
        "mlp_0",
        "attention_1",
        "mlp_1",
        "attention_2",
        "mlp_2",
        "output_head",
    )
    layers = [
        {
            "name": name,
            "params": (
                131_072
                if name == "embedding"
                else 96_000_000 + rng.randrange(0, 48_000_001, 1_000)
            ),
        }
        for name in layer_names
    ]
    model = {
        "model_id": "ledger-demo-1.8b",
        "layers": layers,
        "flops_per_parameter_per_token": 2.0,
        "memory_base_mib": 24.0,
        "kv_mib_per_token": 0.0625,
        "activation_mib_per_token": 0.00048828125,
        "batch_overhead_ms": 1.5,
    }
    replicas = [
        {
            "replica_id": "replica-alpha",
            "compute_capacity": 3000.0,
            "memory_limit_mib": 160.0,
            "window_ms": 100,
            "max_batch_size": 4,
            "max_sequence_length": 2048,
        },
        {
            "replica_id": "replica-beta",
            "compute_capacity": 3000.0,
            "memory_limit_mib": 192.0,
            "window_ms": 100,
            "max_batch_size": 3,
            "max_sequence_length": 1536,
        },
        {
            "replica_id": "replica-gamma",
            "compute_capacity": 3400.0,
            "memory_limit_mib": 256.0,
            "window_ms": 80,
            "max_batch_size": 5,
            "max_sequence_length": 3072,
        },
    ]
    requests = []
    request_id = 1
    for burst_index in range(4):
        burst_arrival = burst_index * 400
        for offset in range(12):
            arrival = burst_arrival + offset * 3
            input_tokens = rng.randrange(48, 257, 8)
            output_tokens = rng.randrange(24, 129, 8)
            deadline = arrival + rng.randrange(900, 1801, 25)
            if request_id == 48:
                deadline = arrival + 1
            requests.append(
                {
                    "request_id": request_id,
                    "arrival_ms": arrival,
                    "priority": rng.randrange(0, 4),
                    "input_tokens": input_tokens,
                    "max_output_tokens": output_tokens,
                    "deadline_ms": deadline,
                }
            )
            request_id += 1
    accuracy = {
        "compute_scale": 4096,
        "memory_scale": 1024,
        "latency_scale": 1000,
        "float_tolerance": 6.0,
        "error_budget": 0.02,
        "rounding_mode": "half_up",
    }
    return {
        "schema_version": 1,
        "model": model,
        "replicas": replicas,
        "requests": requests,
        "accuracy": accuracy,
        "injections": [],
    }


def generate(output: Path, seed: int) -> dict[str, Any]:
    fixture = build_fixture(seed)
    output.mkdir(parents=True, exist_ok=True)
    for key in ("model", "replicas", "requests", "accuracy", "injections"):
        _dump(output / f"{key}.json", fixture[key])
    manifest = {
        "schema_version": 1,
        "seed": seed,
        "generator": "tools/generate_samples.py",
        "model": "model.json",
        "replicas": "replicas.json",
        "requests": "requests.json",
        "accuracy": "accuracy.json",
        "injections": "injections.json",
    }
    _dump(output / "manifest.json", manifest)
    return {
        "ok": True,
        "seed": seed,
        "output": str(output),
        "counts": {
            "layers": len(fixture["model"]["layers"]),
            "replicas": len(fixture["replicas"]),
            "requests": len(fixture["requests"]),
            "injections": len(fixture["injections"]),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="samples/default", help="output directory")
    parser.add_argument("--seed", type=int, default=20261126, help="fixed random seed")
    args = parser.parse_args()
    result = generate(Path(args.output), args.seed)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
