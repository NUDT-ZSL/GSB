#!/usr/bin/env python3
"""Generate the deterministic local sample set used by tests and verification."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import yaml


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def _candidate(request_id: str, item_id: str, values: list[float]) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "item_id": item_id,
        "recall_scores": [round(values[0], 6), round(values[1], 6)],
        "dense_features": [round(values[2], 6), round(values[3], 6)],
        "sparse_counts": [int(values[4])],
    }


def build_samples(sample_dir: Path = SAMPLES) -> dict[str, Path]:
    sample_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(20261117)
    lines: list[str] = []

    for request_number in range(1, 15):
        request_id = f"req-{request_number:03d}"
        count = 9 if request_number == 3 else 4
        if request_number == 12:
            vectors = [
                [0.9, 0.1, 5.0, 2.0, 1.0],
                [0.89998, 0.10001, 5.001, 2.0, 1.0],
                [0.1, 0.9, 0.0, 8.0, 3.0],
                [0.5, 0.5, 10.0, 0.0, 0.0],
            ]
        else:
            vectors = []
            for _ in range(count):
                vectors.append(
                    [
                        float(rng.uniform(0.0, 1.0)),
                        float(rng.uniform(0.0, 1.0)),
                        float(rng.uniform(-1.0, 10.0)),
                        float(rng.uniform(0.0, 8.0)),
                        float(rng.integers(0, 6)),
                    ]
                )
        for candidate_number, values in enumerate(vectors):
            item_id = f"item-{request_number:03d}-{candidate_number:02d}"
            lines.append(
                json.dumps(
                    _candidate(request_id, item_id, values),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )

    # A structurally valid request with an empty candidate list.
    lines.append(
        json.dumps(
            {"request_id": "req-099", "candidates": []},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    # A deliberately malformed JSONL record.
    lines.append('{"request_id":"req-bad-json","item_id":"broken"')

    params: dict[str, Any] = {
        "schema_version": 1,
        "version": "params-20261117",
        "feature_names": [
            "recall_route_a",
            "recall_route_b",
            "dense_ctr",
            "dense_price",
            "sparse_category_count",
        ],
        "feature_weights": [0.8, 0.25, 0.55, -0.15, 0.1],
        "bias": -0.2,
        "feature_groups": {
            "recall": [0, 1],
            "dense": [2, 3],
            "sparse": [4],
        },
    }

    config: dict[str, Any] = {
        "schema_version": 1,
        "normalization": {
            "method": "minmax",
            "zero_denominator_policy": "zero",
        },
        "fixed_point": {
            "width": 3,
            "max_width": 12,
            "base": 10,
            "levels": 1000,
        },
        "fusion": {"temperature": 0.7},
        "score": {
            "clip_min": -1.5,
            "clip_max": 1.5,
            "decimals": 8,
        },
        "scheduler": {
            "concurrency": 4,
            "global_budget": 52,
            "per_request_quota": 7,
            "snapshot_commit_interval": 1,
        },
        "injection_list_path": "injections.json",
        "known_feature_groups": ["recall", "dense", "sparse"],
        "trace": {
            "high_width": 4,
            "tolerances": {
                "normalization": 1e-12,
                "quantization": 1e-6,
                "fusion": 1e-6,
                "truncation": 1e-6,
            },
        },
    }

    injections = {
        "schema_version": 1,
        "injections": [
            {
                "id": "inj-001",
                "request_id": "req-004",
                "stage": "normalize",
                "candidate_id": "item-004-02",
                "feature_index": 2,
                "fault_type": "non_finite_feature",
            },
            {
                "id": "inj-002",
                "request_id": "req-005",
                "stage": "fusion",
                "fault_type": "weight_length_mismatch",
            },
            {
                "id": "inj-003",
                "request_id": "req-006",
                "stage": "quantize",
                "fault_type": "width_invalid",
                "width": 99,
            },
        ],
    }

    paths = {
        "candidates": sample_dir / "candidates.jsonl",
        "params": sample_dir / "params.json",
        "config": sample_dir / "config.yaml",
        "injections": sample_dir / "injections.json",
    }
    paths["candidates"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    paths["params"].write_text(
        json.dumps(params, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    paths["config"].write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    paths["injections"].write_text(
        json.dumps(injections, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(SAMPLES))
    args = parser.parse_args()
    paths = build_samples(Path(args.output_dir))
    for name, path in paths.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
