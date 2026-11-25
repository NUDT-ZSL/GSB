#!/usr/bin/env python3
"""Generate deterministic local samples, invalid cases and config snapshots."""

from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from binning_engine.config_migration import (  # noqa: E402
    canonical_to_v1,
    write_v2_snapshot,
)
from binning_engine.config import normalize_config  # noqa: E402
from binning_engine.hashing import sha256_file, write_atomic_json  # noqa: E402


SEED = 20261126
ROW_COUNT = 180
ORIGIN_MS = 1700000000000
RELATIVE_SECONDS = 120


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


def build_records() -> list:
    rng = random.Random(SEED)
    cities = ["Beijing", "Shanghai", "Shenzhen", "Hangzhou", "Chengdu"]
    segments = ["A", "B", "C"]
    city_effect = {
        "Beijing": 0.18,
        "Shanghai": 0.08,
        "Shenzhen": 0.12,
        "Hangzhou": -0.05,
        "Chengdu": -0.16,
    }
    segment_effect = {"A": -0.22, "B": 0.02, "C": 0.20}
    records = []
    for row_id in range(1, ROW_COUNT + 1):
        age = rng.randint(19, 64)
        income = round(9000 + rng.random() * 48000 + max(0, 45 - age) * 180, 2)
        score = round(rng.gauss(58, 18), 3)
        score = max(0.0, min(100.0, score))
        city = rng.choice(cities)
        segment = rng.choices(segments, weights=[0.35, 0.4, 0.25], k=1)[0]
        risk = (
            -0.25
            + 0.025 * (age - 40)
            - 0.000025 * (income - 26000)
            + 0.018 * (score - 55)
            + city_effect[city]
            + segment_effect[segment]
            + rng.gauss(0.0, 0.12)
        )
        label = 1 if rng.random() < _sigmoid(risk) else 0
        records.append(
            {
                "row_id": row_id,
                "age": age,
                "income": income,
                "score": score,
                "city": city,
                "segment": segment,
                "label": label,
            }
        )
    rng.shuffle(records)
    return records


def write_jsonl(path: Path, records: list, *, allow_nan: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=allow_nan,
                )
                + "\n"
            )


def build_config() -> dict:
    return {
        "schema_version": "2.0",
        "config_name": "table_binning_diagnostic",
        "version": 3,
        "seed": SEED,
        "row_count": ROW_COUNT,
        "time_origin_absolute_ms": ORIGIN_MS,
        "recorded_at_relative_seconds": RELATIVE_SECONDS,
        "numeric_columns": ["score", "age", "income"],
        "categorical_columns": ["segment", "city"],
        "label_column": "label",
        "vocabulary": {
            "city": ["Beijing", "Shanghai", "Shenzhen", "Hangzhou", "Chengdu"],
            "segment": ["A", "B", "C"],
        },
        "bins": {
            "age": {"bin_count": 5, "bin_method": "equal_width"},
            "income": {"bin_count": 6, "bin_method": "equal_freq"},
            "score": {"bin_count": 4, "bin_method": "equal_width"},
        },
        "encoding": {
            "method": "one_hot",
            "smoothing": 8.0,
            "column_methods": {"income": "target_encoding"},
        },
        "quantization": {
            "default": {"quant_scale": 0.25, "zero_point": 0},
            "columns": {
                "income": {"quant_scale": 0.03125, "zero_point": 0},
            },
        },
        "precision_tier": "int8",
        "scheduler": {"workers": 2, "batch_size": 24},
        "resource_budget": {
            "max_memory_bytes": 256 * 1024 * 1024,
            "max_parallel_slots": 4,
            "max_comparisons": 5_000_000,
            "max_encoding_length": 2_000_000,
        },
        "priority": ["quality", "memory", "latency"],
        "degradation_policy": {
            "on_limit": "degrade",
            "allow_worker_reduction": True,
            "allow_batch_reduction": True,
            "allow_bin_reduction": True,
            "allow_quant_coarsening": True,
        },
    }


def main() -> int:
    fixtures = ROOT / "fixtures"
    config_dir = fixtures / "config"
    invalid_dir = fixtures / "invalid"
    fixtures.mkdir(parents=True, exist_ok=True)

    records = build_records()
    samples_path = fixtures / "samples.jsonl"
    write_jsonl(samples_path, records)

    duplicate = [dict(records[0]), dict(records[0])]
    duplicate[1]["income"] = round(float(duplicate[1]["income"]) + 1.0, 2)
    write_jsonl(invalid_dir / "duplicate_row_id.jsonl", duplicate)

    nan_record = dict(records[1])
    nan_record["score"] = float("nan")
    write_jsonl(invalid_dir / "nan_value.jsonl", [records[0], nan_record], allow_nan=True)

    category_record = dict(records[2])
    category_record["city"] = "ControlledVocabularyOutside"
    write_jsonl(invalid_dir / "bad_category.jsonl", [records[0], category_record])

    raw_config = build_config()
    config = normalize_config(raw_config)
    write_atomic_json(config_dir / "engine_config.json", config)
    v1 = canonical_to_v1(config)
    write_atomic_json(config_dir / "binning_config.v1.json", v1)
    v2_metadata = write_v2_snapshot(config_dir / "binning_config.v2", config)

    invalid_encoding = dict(raw_config)
    invalid_encoding["encoding"] = {"method": "binary_weird_encoding"}
    write_atomic_json(config_dir / "invalid_encoding_method.json", invalid_encoding)

    invalid_quant = dict(raw_config)
    invalid_quant["quantization"] = {
        "default": {"quant_scale": 0.0, "zero_point": 0}
    }
    write_atomic_json(config_dir / "invalid_quant_params.json", invalid_quant)

    old_version = dict(raw_config)
    old_version["version"] = 1
    write_atomic_json(config_dir / "old_version_config.json", old_version)

    files = [
        samples_path,
        invalid_dir / "duplicate_row_id.jsonl",
        invalid_dir / "nan_value.jsonl",
        invalid_dir / "bad_category.jsonl",
        config_dir / "engine_config.json",
        config_dir / "binning_config.v1.json",
        config_dir / "invalid_encoding_method.json",
        config_dir / "invalid_quant_params.json",
        config_dir / "old_version_config.json",
        config_dir / "binning_config.v2" / "metadata.sqlite3",
        *sorted((config_dir / "binning_config.v2" / "shards").glob("*.json")),
    ]
    manifest = {
        "schema": "deterministic-fixture-manifest/v1",
        "seed": SEED,
        "row_count": ROW_COUNT,
        "time_origin_absolute_ms": ORIGIN_MS,
        "relative_seconds": RELATIVE_SECONDS,
        "expected_absolute_ms": ORIGIN_MS + RELATIVE_SECONDS * 1000,
        "files": [
            {
                "path": str(path.relative_to(ROOT)),
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
            for path in files
        ],
        "invalid_cases": {
            "duplicate_row_id": "E_DUP_INPUT",
            "nan_value": "E_NAN_VALUE",
            "bad_category": "E_CATEGORY_VIOLATION",
            "unknown_encoding": "E_CODE_METHOD",
            "invalid_quant": "E_QUANT_PARAM",
            "rollback": "E_SNAPSHOT_ROLLBACK",
            "corrupt_cache": "E_CACHE_CORRUPT",
            "resource_limit": "E_RESOURCE_LIMIT",
        },
        "v2_metadata": v2_metadata,
    }
    write_atomic_json(fixtures / "input_manifest.json", manifest)
    print(f"generated {ROW_COUNT} rows and {len(files)} fixture files under {fixtures}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
