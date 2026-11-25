#!/usr/bin/env python3
"""Generate the deterministic local validation fixture.

The fixture is intentionally data-only. It never imports torch, never reads
the wall clock, and never contacts a network service.
"""

import argparse
import json
from pathlib import Path

DEFAULT_SEED = 20261126


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def generate(output: Path, seed: int = DEFAULT_SEED) -> None:
    strategy = {
        "schema": "ckpt-strategy/v1",
        "shard_count": 4,
        "max_shard_bytes": 65536,
        "max_concurrency": 3,
        "round_budget_ms": 120,
        "clock_tolerance_ms": 250,
        "shard_base_cost_ms": 5,
        "bytes_per_ms": 4096,
    }
    save_manifest = {
        "schema": "ckpt-save-plan/v1",
        "versions": [
            {"version": 1, "changed_layers": ["*"]},
            {
                "version": 2,
                "changed_layers": ["encoder.1.weight", "decoder.bias"],
            },
            {"version": 3, "changed_layers": ["embedding.weight"]},
        ],
    }
    plan = {
        "schema": "ckpt-plan/v1",
        "checkpoint": "demo",
        "global_seed": seed,
        "dtype": "float32",
        "layers": [
            {"name": "embedding.weight", "shape": [64, 32]},
            {"name": "encoder.0.weight", "shape": [32, 32]},
            {"name": "encoder.0.bias", "shape": [32]},
            {"name": "encoder.1.weight", "shape": [32, 32]},
            {"name": "encoder.1.bias", "shape": [32]},
            {"name": "decoder.weight", "shape": [32, 64]},
            {"name": "decoder.bias", "shape": [64]},
            {"name": "norm.weight", "shape": [64], "dtype": "float32"},
            {"name": "norm.bias", "shape": [64], "dtype": "float32"},
        ],
        "strategy": strategy,
        "checkpoint_manifest": save_manifest,
    }
    write_manifest = {
        "schema": "ckpt-crash/v1",
        "injections": [
            {
                "stage": "write",
                "after_committed_shards": 2,
                "signal": "SIGKILL",
            }
        ],
    }
    commit_manifest = {
        "schema": "ckpt-crash/v1",
        "injections": [{"stage": "commit", "signal": "SIGKILL"}],
    }

    output.mkdir(parents=True, exist_ok=True)
    _write(output / "plan.json", plan)
    _write(output / "strategy.json", strategy)
    _write(output / "checkpoint-manifest.json", save_manifest)
    _write(output / "crash-write.json", write_manifest)
    _write(output / "crash-commit.json", commit_manifest)
    (output / "seed.txt").write_text(
        "global_seed={0}\n".format(seed), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        default="examples/data",
        help="output directory (default: examples/data)",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    generate(Path(args.out), args.seed)


if __name__ == "__main__":
    main()
