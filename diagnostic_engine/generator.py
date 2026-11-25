"""Deterministic local fixture generator.

The generated JSONL intentionally has the schema described in the task.  The
generator uses a fixed ``numpy.random.Generator`` seed and no network access.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from .config import default_config
from .hashing import canonical_json, sha256_json


SAMPLE_RATE_MS = [200, 250, 400, 500, 320, 280, 450, 600, 350, 500]
NOISE_SCALE = [0.10, 0.16, 0.22, 0.13, 0.28, 0.18, 0.24, 0.12, 0.31, 0.20]


def _is_missing_time(var_index: int, time_ms: int) -> bool:
    first_start = 4200 + (var_index % 4) * 2600
    first = first_start <= time_ms < first_start + 650 + 90 * (var_index % 3)
    second_start = 14500 + (var_index % 5) * 1700
    second = second_start <= time_ms < second_start + 520 + 110 * (var_index % 4)
    return first or second


def generate_records(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    cfg = config or default_config()
    seed = int(cfg.get("seed", 440))
    rng = np.random.default_rng(seed)
    variables = cfg["variables"]
    time_cfg = cfg["time"]
    total_ms = int(time_cfg["windows"] * time_cfg["window_ms"])
    width = int(time_cfg["values_width"])
    subsample_ms = int(time_cfg["subsample_ms"])

    raw_records: list[dict[str, Any]] = []
    for var_index, name in enumerate(variables):
        interval = SAMPLE_RATE_MS[var_index % len(SAMPLE_RATE_MS)]
        phase = 37 * var_index
        record_number = 0
        for obs_time in range(interval + phase, total_ms, interval):
            if _is_missing_time(var_index, obs_time):
                record_number += 1
                continue

            values: list[float] = []
            for sub_index in range(width):
                sample_time = obs_time + sub_index * subsample_ms
                cycle = 1.35 * np.sin(sample_time / 430.0 + var_index * 0.73)
                cycle += 0.45 * np.cos(sample_time / 970.0 + var_index * 0.31)
                trend_start = 8000 + 900 * (var_index % 5)
                local_trend = 0.0
                if sample_time > trend_start:
                    local_trend = 0.00042 * (sample_time - trend_start) * (1 if var_index % 3 else -0.7)
                noise = float(rng.normal(0.0, NOISE_SCALE[var_index]))
                if var_index in (4, 8) and record_number % 7 == 3:
                    noise += float(rng.laplace(0.0, 0.35))
                value = cycle + local_trend + noise
                if record_number > 0 and record_number % 13 == var_index % 13:
                    direction = 1.0 if (var_index + record_number) % 2 == 0 else -1.0
                    value += direction * (5.5 + 0.35 * (var_index % 4))
                values.append(round(float(value), 9))

            q_param = round(
                float(np.clip(0.92 - 0.035 * (var_index % 5) + rng.normal(0.0, 0.012), 0.55, 0.99)),
                6,
            )
            raw_records.append(
                {
                    "sort_time": obs_time,
                    "sort_name": name,
                    "sort_sub": record_number,
                    "src_name": name,
                    "obs_time_ms": int(obs_time),
                    "values": values,
                    "q_param": q_param,
                }
            )
            record_number += 1

    raw_records.sort(key=lambda r: (r["sort_time"], r["sort_name"], r["sort_sub"]))
    records: list[dict[str, Any]] = []
    for v_idx, record in enumerate(raw_records, start=1):
        records.append(
            {
                "v_idx": v_idx,
                "src_name": record["src_name"],
                "obs_time_ms": record["obs_time_ms"],
                "values": record["values"],
                "q_param": record["q_param"],
            }
        )
    return records


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
            handle.write("\n")


def generate_fixture(
    output_dir: str | Path = "samples",
    seed: int = 440,
    config: dict[str, Any] | None = None,
) -> dict[str, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    cfg = config or default_config(seed)
    records = generate_records(cfg)
    observations = out / "observations.jsonl"
    scenario = out / "scenario.json"
    write_jsonl(observations, records)
    scenario.write_text(canonical_json(cfg) + "\n", encoding="utf-8")
    manifest = {
        "seed": seed,
        "record_count": len(records),
        "observations_sha256": sha256_json(records),
        "config_sha256": sha256_json(cfg),
        "observations_path": str(observations),
        "scenario_path": str(scenario),
    }
    manifest_path = out / "manifest.json"
    manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8")
    return {
        "observations": observations,
        "scenario": scenario,
        "manifest": manifest_path,
    }


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                from .errors import bad_input

                raise bad_input(f"invalid JSON on line {line_number}", line=line_number) from exc
            records.append(record)
    return records
