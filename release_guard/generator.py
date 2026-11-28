from __future__ import annotations

import random
from decimal import Decimal
from pathlib import Path
from typing import Any

from .common import write_json


VERSION = "monitor-2026.11.3"
SESSION_CAPACITY = 12000
SCHEMA_NAMES = ("v1", "v2", "v3")


def _d(value: str) -> Decimal:
    return Decimal(value)


STEPS = [
    {
        "step_id": "canary",
        "traffic_percent": _d("5"),
        "observation_seconds": 240,
        "thresholds": (_d("0.010"), _d("320"), _d("0.008")),
    },
    {
        "step_id": "early-cohort",
        "traffic_percent": _d("15"),
        "observation_seconds": 330,
        "thresholds": (_d("0.012"), _d("330"), _d("0.009")),
    },
    {
        "step_id": "regional-expand",
        "traffic_percent": _d("40"),
        "observation_seconds": 240,
        "thresholds": (_d("0.011"), _d("340"), _d("0.0075")),
    },
    {
        "step_id": "exam-week-surge",
        "traffic_percent": _d("70"),
        "observation_seconds": 240,
        "thresholds": (_d("0.010"), _d("320"), _d("0.008")),
    },
    {
        "step_id": "full-traffic",
        "traffic_percent": _d("100"),
        "observation_seconds": 180,
        "thresholds": (_d("0.012"), _d("330"), _d("0.009")),
    },
]

SLIDING_SECONDS = 60
SAMPLE_INTERVAL_SECONDS = 30
MAX_MISSING_RATIO = _d("0.34")
REQUIRED_CONSECUTIVE = 3


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _percent_text(value: Decimal) -> str:
    return _decimal_text(value)


def _threshold_percent(value: Decimal) -> str:
    return format(value * 100, ".2f")


def _generate_observations(seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    last_end = sum(step["observation_seconds"] for step in STEPS)
    observations: list[dict[str, Any]] = []
    for timestamp in range(SAMPLE_INTERVAL_SECONDS, last_end + 1, SAMPLE_INTERVAL_SECONDS):
        sample_count = rng.randint(900, 1600)
        error_rate = round(rng.uniform(0.0010, 0.0035), 9)
        latency = round(rng.uniform(170.0, 260.0), 3)
        drop_rate = round(rng.uniform(0.0005, 0.0030), 9)

        if timestamp == 120:
            error_rate = 0.060000000
        if timestamp in (270, 300):
            latency = 750.000 if timestamp == 270 else 760.000
        if 330 <= timestamp <= 420:
            latency = None
        if timestamp == 510:
            latency = 700.000
        if timestamp == 690:
            drop_rate = 0.040000000
        if timestamp in (870, 900, 930):
            error_rate = 0.065000000

        observations.append(
            {
                "logical_time_seconds": timestamp,
                "sample_count": sample_count,
                "error_rate": error_rate,
                "latency_p95_ms": latency,
                "session_drop_rate": drop_rate,
            }
        )
    return observations


def _step_common(step: dict[str, Any], index: int) -> dict[str, Any]:
    error_threshold, latency_threshold, drop_threshold = step["thresholds"]
    traffic_ppm = int(step["traffic_percent"] * 10000)
    return {
        "step_number": index + 1,
        "step_id": step["step_id"],
        "traffic_percent": _percent_text(step["traffic_percent"]),
        "traffic_ppm": traffic_ppm,
        "observation_seconds": step["observation_seconds"],
        "sliding_seconds": SLIDING_SECONDS,
        "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
        "max_missing_ratio": MAX_MISSING_RATIO,
        "required_consecutive": REQUIRED_CONSECUTIVE,
        "error_threshold": error_threshold,
        "latency_threshold": latency_threshold,
        "drop_threshold": drop_threshold,
    }


def _build_v1(observations: list[dict[str, Any]], seed: int) -> dict[str, Any]:
    steps = []
    for index, raw_step in enumerate(STEPS):
        step = _step_common(raw_step, index)
        steps.append(
            {
                "order": step["step_number"],
                "step_id": step["step_id"],
                "traffic_percent": float(step["traffic_percent"]),
                "observe_seconds": step["observation_seconds"],
                "rolling_seconds": step["sliding_seconds"],
                "probe_interval_seconds": step["sample_interval_seconds"],
                "max_missing_ratio": float(step["max_missing_ratio"]),
                "required_consecutive_breaches": step["required_consecutive"],
                "thresholds": {
                    "error_rate_percent": float(step["error_threshold"] * 100),
                    "latency_p95_ms": float(step["latency_threshold"]),
                    "session_drop_percent": float(step["drop_threshold"] * 100),
                },
            }
        )

    samples = [
        {
            "at_seconds": row["logical_time_seconds"],
            "requests": row["sample_count"],
            "error_rate": row["error_rate"],
            "latency_p95_ms": row["latency_p95_ms"],
            "session_drop_rate": row["session_drop_rate"],
        }
        for row in observations
    ]
    return {
        "schema": "gsb.release/v1",
        "sample_seed": seed,
        "service": {"name": "exam-proctor", "release": VERSION},
        "session_capacity": SESSION_CAPACITY,
        "rollout": {"strategy": "traffic", "steps": steps},
        "telemetry": {"clock": "relative_seconds", "samples": samples},
    }


def _build_v2(observations: list[dict[str, Any]], seed: int) -> dict[str, Any]:
    stages = []
    for index, raw_step in enumerate(STEPS):
        step = _step_common(raw_step, index)
        stages.append(
            {
                "sequence": step["step_number"],
                "id": step["step_id"],
                "traffic_fraction": float(
                    Decimal(step["traffic_ppm"]) / Decimal(1_000_000)
                ),
                "window": {
                    "observe_minutes": float(
                        Decimal(step["observation_seconds"]) / Decimal(60)
                    ),
                    "slide_minutes": float(
                        Decimal(step["sliding_seconds"]) / Decimal(60)
                    ),
                },
                "sampling": {
                    "cadence_s": step["sample_interval_seconds"],
                    "tolerance": {
                        "missing_fraction": float(step["max_missing_ratio"])
                    },
                },
                "health": {
                    "error_rate": float(step["error_threshold"]),
                    "latency_p95_ms": float(step["latency_threshold"]),
                    "disconnect_rate": float(step["drop_threshold"]),
                },
                "sustain": step["required_consecutive"],
            }
        )
    # The v2 producer is allowed to store stages out of order; sequence is canonical.
    reorder = [3, 0, 4, 1, 2]
    stages = [stages[index] for index in reorder]

    samples = [
        {
            "at_ms": row["logical_time_seconds"] * 1000,
            "n": row["sample_count"],
            "err": row["error_rate"],
            "lat_p95_ms": row["latency_p95_ms"],
            "drop": row["session_drop_rate"],
        }
        for row in observations
    ]
    return {
        "schema": "gsb.release/v2",
        "sample_seed": seed,
        "release": {"service": "exam-proctor", "artifact": VERSION},
        "capacity": {"sessions": SESSION_CAPACITY},
        "stages": stages,
        "observations": {"time_unit": "milliseconds", "samples": samples},
    }


def _build_v3(observations: list[dict[str, Any]], seed: int) -> dict[str, Any]:
    traffic_steps = []
    for index, raw_step in enumerate(STEPS):
        step = _step_common(raw_step, index)
        traffic_steps.append(
            {
                "phase": step["step_number"],
                "label": step["step_id"],
                "traffic_ppm": step["traffic_ppm"],
                "observe_ms": step["observation_seconds"] * 1000,
                "rolling_ms": step["sliding_seconds"] * 1000,
                "sample_ms": step["sample_interval_seconds"] * 1000,
                "drop_limit_bp": int(step["max_missing_ratio"] * 10000),
                "sustain_points": step["required_consecutive"],
                "threshold_expr": {
                    "error": f"<= {_threshold_percent(step['error_threshold'])}%",
                    "latency": f"p95 <= {_decimal_text(step['latency_threshold'])} ms",
                    "session_drop": (
                        f"<= {_threshold_percent(step['drop_threshold'])}%"
                    ),
                },
            }
        )

    metric_stream = [
        {
            "ts_ms": row["logical_time_seconds"] * 1000,
            "samples": row["sample_count"],
            "error_pct": (
                None
                if row["error_rate"] is None
                else float(Decimal(str(row["error_rate"])) * 100)
            ),
            "latency_p95": row["latency_p95_ms"],
            "drop_pct": (
                None
                if row["session_drop_rate"] is None
                else float(Decimal(str(row["session_drop_rate"])) * 100)
            ),
        }
        for row in observations
    ]
    return {
        "schema": "gsb.release/v3",
        "sample_seed": seed,
        "artifact_version": VERSION,
        "session_pool_size": SESSION_CAPACITY,
        "plan": {
            "traffic_steps": traffic_steps,
            "metric_stream": metric_stream,
        },
    }


def generate_schemas(seed: int) -> dict[str, dict[str, Any]]:
    observations = _generate_observations(seed)
    return {
        "v1": _build_v1(observations, seed),
        "v2": _build_v2(observations, seed),
        "v3": _build_v3(observations, seed),
    }


def write_samples(samples_dir: Path, seed: int) -> dict[str, Path]:
    samples = generate_schemas(seed)
    paths = {}
    for name in SCHEMA_NAMES:
        path = samples_dir / f"schema_{name}.json"
        write_json(path, samples[name])
        paths[name] = path
    return paths

