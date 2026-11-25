from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any

import numpy as np

from .config import FeatureConfig
from .errors import bad_input
from .util import atomic_write_bytes, atomic_write_json, canonical_json, sha256_bytes, sha256_json


REGIONS = ["north", "south", "east", "west"]
SEGMENTS = ["basic", "plus", "pro"]
CHANNELS = ["web", "app", "store"]


def build_sample_config_v1() -> dict[str, Any]:
    return {
        "schema": "feature_config/v1",
        "snapshot_id": "sample-config-v1",
        "snapshot_version": 1,
        "effective_at_sec": 12.345,
        "epoch_ms": 1704067200000,
        "seed": 20261126,
        "fit_rows": 20,
        "row_id_column": "row_id",
        "label_column": "label",
        "numeric": [
            {"name": "age", "method": "standardize"},
            {"name": "income", "method": "minmax"},
            {"name": "score", "method": "standardize"},
        ],
        "categorical": [
            {"name": "region", "codec": "one_hot", "categories": REGIONS, "prior_weight": 5.0},
            {"name": "segment", "codec": "target", "categories": SEGMENTS, "prior_weight": 4.0},
            {"name": "channel", "codec": "one_hot", "categories": CHANNELS, "prior_weight": 5.0},
        ],
        "cross": [
            {"slot": "region__segment", "columns": ["region", "segment"], "codec": "one_hot", "prior_weight": 5.0},
            {"slot": "segment__channel", "columns": ["segment", "channel"], "codec": "target", "prior_weight": 6.0},
            {"slot": "income__score", "columns": ["income", "score"], "codec": "product", "prior_weight": 5.0},
        ],
        "selector": {
            "method": "variance",
            "target_k": 12,
        },
    }


def generate_rows(seed: int = 20261126, count: int = 32) -> list[dict[str, Any]]:
    rng = np.random.Generator(np.random.PCG64(seed))
    rows: list[dict[str, Any]] = []
    for index in range(count):
        age = int(rng.integers(19, 67))
        income = float(round(float(rng.normal(7100.0, 1850.0)), 6))
        score = float(round(float(rng.normal(52.0, 13.0)), 6))
        region = REGIONS[int(rng.integers(0, len(REGIONS)))]
        segment = SEGMENTS[int(rng.integers(0, len(SEGMENTS)))]
        channel = CHANNELS[int(rng.integers(0, len(CHANNELS)))]
        logit = -1.05 + 0.035 * (age - 42.0)
        logit += {"north": 0.20, "south": -0.35, "east": 0.55, "west": 0.10}[region]
        logit += {"basic": -0.65, "plus": 0.25, "pro": 0.80}[segment]
        logit += {"web": 0.05, "app": 0.34, "store": -0.20}[channel]
        noise = float(rng.normal(0.0, 0.85))
        probability = 1.0 / (1.0 + math.exp(-(logit + noise)))
        label = 1 if float(rng.random()) < probability else 0
        rows.append(
            {
                "row_id": f"r{index + 1:04d}",
                "numerics": {"age": float(age), "income": income, "score": score},
                "categoricals": {"region": region, "segment": segment, "channel": channel},
                "label": float(label),
            }
        )
    return rows


def write_rows_csv(path: Path, rows: list[dict[str, Any]], config: FeatureConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    numeric_names = [item["name"] for item in config.numeric]
    categorical_names = [item["name"] for item in config.categorical]
    columns = [config.row_id_column, *numeric_names, *categorical_names, config.label_column]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            record: dict[str, Any] = {config.row_id_column: row["row_id"]}
            for name in numeric_names:
                record[name] = format(float(row["numerics"][name]), ".9f")
            for name in categorical_names:
                record[name] = row["categoricals"][name]
            record[config.label_column] = format(float(row["label"]), ".1f")
            writer.writerow(record)


def read_rows_csv(path: Path, config: FeatureConfig) -> list[dict[str, Any]]:
    numeric_names = [item["name"] for item in config.numeric]
    categorical_names = [item["name"] for item in config.categorical]
    expected = [config.row_id_column, *numeric_names, *categorical_names, config.label_column]
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        missing = [name for name in expected if name not in fieldnames]
        if missing:
            raise bad_input("input is missing required columns", field="columns", missing=sorted(missing))
        for line_number, record in enumerate(reader, start=2):
            if None in record:
                raise bad_input("input contains extra unnamed columns", row=line_number, field="csv")
            row_id = str(record[config.row_id_column])
            if not row_id:
                raise bad_input("row_id must not be empty", row=line_number, field=config.row_id_column)
            numerics: dict[str, float] = {}
            for name in numeric_names:
                raw = record[name]
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    raise bad_input("numeric field is not parseable", row=row_id, row_number=line_number, field=name) from None
                if not math.isfinite(value):
                    raise bad_input(
                        "numeric field contains a non-finite value",
                        row=row_id,
                        row_number=line_number,
                        field=name,
                    )
                numerics[name] = value
            categoricals: dict[str, str] = {}
            for name in categorical_names:
                categoricals[name] = str(record[name])
            label_raw = record[config.label_column]
            try:
                label = float(label_raw)
            except (TypeError, ValueError):
                raise bad_input("label field is not parseable", row=row_id, row_number=line_number, field=config.label_column) from None
            if not math.isfinite(label):
                raise bad_input(
                    "label contains a non-finite value",
                    row=row_id,
                    row_number=line_number,
                    field=config.label_column,
                )
            rows.append(
                {
                    "row_id": row_id,
                    "numerics": numerics,
                    "categoricals": categoricals,
                    "label": label,
                    "input_row_number": line_number,
                }
            )
    return rows


def rows_to_canonical(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "row_id": row["row_id"],
            "numerics": {name: round(float(value), 12) for name, value in sorted(row["numerics"].items())},
            "categoricals": dict(sorted(row["categoricals"].items())),
            "label": round(float(row["label"]), 12),
        }
        for row in sorted(rows, key=lambda item: item["row_id"])
    ]


def rows_digest(rows: list[dict[str, Any]]) -> str:
    return sha256_json(rows_to_canonical(rows))


def write_samples(
    directory: Path,
    *,
    seed: int = 20261126,
    count: int = 32,
    config_path: Path | None = None,
) -> tuple[Path, Path]:
    from .config import normalize_config

    directory.mkdir(parents=True, exist_ok=True)
    config = normalize_config(build_sample_config_v1())
    rows = generate_rows(seed=seed, count=count)
    data_path = directory / "sample.csv"
    if config_path is None:
        config_path = directory / "config-v1.json"
    config_path = Path(config_path)
    write_rows_csv(data_path, rows, config)
    raw_config = build_sample_config_v1()
    config_bytes = canonical_json(raw_config, newline=True).encode("utf-8")
    atomic_write_bytes(config_path, config_bytes)
    atomic_write_bytes(
        config_path.with_name(config_path.name + ".sha256"),
        (sha256_bytes(config_bytes) + "\n").encode("utf-8"),
    )
    (directory / "sample.meta.json").write_text(
        canonical_json(
            {
                "seed": seed,
                "row_count": count,
                "data_sha256": sha256_json(rows_to_canonical(rows)),
                "generator": "feature_diag.sample.generate_rows",
            },
            newline=True,
        ),
        encoding="utf-8",
    )
    return data_path, config_path
