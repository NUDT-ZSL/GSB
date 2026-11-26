"""Deterministic local telemetry and predictor snapshot generator."""

from __future__ import annotations

import math
import random
from pathlib import Path
from typing import Any

from .canonical import (
    atomic_write_json,
    atomic_write_text,
    canonical_json,
    sha256_file,
)
from .snapshots import (
    MetadataCompletion,
    PredictorModel,
    SnapshotBundle,
    Tombstone,
    model_identifier,
    write_v1,
    write_v2,
)


DEFAULT_SEED = 20261127
TICK_MS = 100
END_MS = 3000


def _value_record(
    variable: int, source: str, logical_ms: int, value: float | None
) -> dict[str, Any]:
    return {
        "variable": variable,
        "source": source,
        "logical_ms": logical_ms,
        "values": [value],
    }


def build_observations() -> list[dict[str, Any]]:
    rng = random.Random(DEFAULT_SEED)
    records: list[dict[str, Any]] = []
    for tick in range(0, END_MS + 1, TICK_MS):
        phase = tick / 1000.0
        x0 = (
            1.20 * math.sin(2.0 * math.pi * phase / 1.9)
            + 0.12 * math.sin(2.0 * math.pi * phase / 0.37)
            + rng.gauss(0.0, 0.018)
        )
        x1 = (
            0.80 * math.cos(2.0 * math.pi * phase / 2.3)
            + rng.gauss(0.0, 0.015)
        )
        if 1800 <= tick <= 2600:
            local = min(1.0, (tick - 1800) / 400.0)
            x0 += 0.38 * local
        if tick == 1500:
            x1 += 6.0
        if tick == 900:
            records.append(_value_record(0, "vehicle-can", tick, None))
        else:
            records.append(_value_record(0, "vehicle-can", tick, x0))
        records.append(_value_record(1, "vehicle-can", tick, x1))

    def latest_value(variable: int, tick: int) -> float:
        candidates = sorted(
            (
                record
                for record in records
                if record["variable"] == variable
                and record["logical_ms"] <= tick
                and record["values"][0] is not None
            ),
            key=lambda item: item["logical_ms"],
        )
        return float(candidates[-1]["values"][0])

    for tick in range(0, END_MS + 1, 2 * TICK_MS):
        phase = tick / 1000.0
        base_x0 = latest_value(0, tick)
        x1 = latest_value(1, tick)
        y = 0.62 * float(base_x0) - 0.34 * x1 + 0.45
        y += rng.gauss(0.0, 0.012)
        if 1800 <= tick <= 2600:
            local = min(1.0, (tick - 1800) / 400.0)
            y += 1.10 * local
        records.append(_value_record(2, "power-bus", tick, y))
        aux = 0.30 * math.cos(2.0 * math.pi * phase / 3.1)
        records.append(
            _value_record(
                3,
                "power-bus",
                tick,
                None if tick == 1200 else aux,
            )
        )

    records.sort(key=lambda item: (item["logical_ms"], item["source"], item["variable"]))
    return records


def build_config() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "seed": DEFAULT_SEED,
        "alignment": {
            "period_ms": TICK_MS,
            "stale_after_ms": 250,
            "missing_strategy": "forward_fill",
        },
        "residual_window": {
            "size": 8,
            "step": 2,
            "max_backtrack": 12,
        },
        "statistics": {
            "decay": 0.82,
            "skew_weight": 0.30,
            "trend_weight": 0.35,
            "upstream_weight": 0.25,
            "variance_floor": 1e-12,
            "residual_scale": 0.35,
            "input_scale": 1.0,
        },
        "thresholds": {
            "policy": "tolerance",
            "tolerance": 1e-9,
            "fixed_scale": 1_000_000,
            "subnormal_floor": 1e-12,
            "joint_rule": "both",
        },
        "retrain": {
            "learning_rate": 0.08,
            "epochs": 28,
            "l2": 1e-4,
            "closure_radius": 1,
        },
        "predictor": {
            "id": "predictor-main",
            "model_type": "linear",
            "input_variables": [0, 1],
            "output_variable": 2,
            "drift_threshold": 1.15,
            "input_amplitude_threshold": 0.12,
            "recycle_rate_threshold": 0.20,
        },
        "variables": [
            {
                "id": 0,
                "name": "traction_torque",
                "source": "vehicle-can",
                "role": "input",
                "sample_rate_hz": 10.0,
            },
            {
                "id": 1,
                "name": "road_slope",
                "source": "vehicle-can",
                "role": "input",
                "sample_rate_hz": 10.0,
            },
            {
                "id": 2,
                "name": "power_demand",
                "source": "power-bus",
                "role": "output",
                "sample_rate_hz": 5.0,
            },
            {
                "id": 3,
                "name": "bus_voltage",
                "source": "power-bus",
                "role": "aux",
                "sample_rate_hz": 5.0,
            },
        ],
    }


def build_initial_model() -> PredictorModel:
    return PredictorModel(
        predictor_id="predictor-main",
        model_type="linear",
        feature_names=("traction_torque", "road_slope"),
        coefficients=(0.62, -0.34),
        intercept=0.45,
        config_generation=1,
        retrain_generation=0,
        created_ms=0,
        trained_until_ms=0,
        origin="v1_initial",
    )


def generate_dataset(output_dir: str | Path) -> dict[str, Any]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    for pattern in (
        "predictor.v2.*.models.json",
        "predictor.v2.*.sqlite",
    ):
        for stale in output.glob(pattern):
            stale.unlink()
    records = build_observations()
    atomic_write_text(
        output / "observations.jsonl",
        "".join(canonical_json(record, newline=True) for record in records),
    )
    config = build_config()
    atomic_write_json(output / "predictor_config.json", config)

    base = build_initial_model()
    v1_bundle = SnapshotBundle(
        version="v1",
        current=base,
        models=(base,),
        tombstones=(),
        completions=(),
        created_ms=0,
    )
    write_v1(v1_bundle, output / "predictor_v1.json")

    calibration = PredictorModel(
        predictor_id=base.predictor_id,
        model_type=base.model_type,
        feature_names=base.feature_names,
        coefficients=(0.60, -0.31),
        intercept=0.41,
        config_generation=1,
        retrain_generation=1,
        created_ms=0,
        trained_until_ms=1200,
        origin="retired_calibration",
    )
    v2_bundle = SnapshotBundle(
        version="v2",
        current=base,
        models=(base, calibration),
        tombstones=(
            Tombstone(
                predictor_id=base.predictor_id,
                target_retrain_generation=1,
                reason="SUPERSEDED_BY_CONFIG_GENERATION",
                created_ms=1200,
            ),
        ),
        completions=(
            MetadataCompletion(
                "config_generation", "fixed_seed_generator", 1
            ),
            MetadataCompletion(
                "absolute_time_ms", "fixed_seed_generator", 0
            ),
        ),
        created_ms=0,
    )
    write_v2(v2_bundle, output / "predictor_v2.json")

    manifest = {
        "schema": "telemetry-drift-dataset-v1",
        "seed": DEFAULT_SEED,
        "tick_ms": TICK_MS,
        "record_count": len(records),
        "current_model": model_identifier(base),
        "files": {},
    }
    for name in (
        "observations.jsonl",
        "predictor_config.json",
        "predictor_v1.json",
        "predictor_v2.json",
    ):
        manifest["files"][name] = sha256_file(output / name)
    atomic_write_json(output / "dataset_manifest.json", manifest)
    return manifest

