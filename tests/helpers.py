from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from sre_burnrate.config import EvaluationConfig


def config_dict(
    *,
    long_minutes: int = 5,
    short_minutes: int = 2,
    step_minutes: int = 2,
    target_ppm: int = 999000,
    threshold: int = 10,
    adaptive_threshold: int = 50_000,
    slo_version: str = "slo-test-v1",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "sli": {
            "id": "test-availability",
            "service": "test-service",
            "timestamp_field": "minute",
            "total_requests_field": "requests",
            "failed_requests_field": "errors",
            "timestamp_unit": "minute",
        },
        "slo": {
            "id": "test-slo",
            "version": slo_version,
            "target_ppm": target_ppm,
        },
        "windows": {
            "long_minutes": long_minutes,
            "short_minutes": short_minutes,
            "step_minutes": step_minutes,
        },
        "alert_policy": {
            "warning_long_ppm": 2_000_000,
            "warning_short_ppm": 1_500_000,
            "critical_long_ppm": threshold * 1_000_000,
            "critical_short_ppm": max(1, threshold // 2) * 1_000_000,
        },
        "precision": {
            "ratio_scale": 1_000_000_000,
            "burn_scale": 1_000_000,
            "request_scale": 1_000_000,
        },
        "adaptive": {"row_threshold": adaptive_threshold},
    }


def make_config(**kwargs: Any) -> EvaluationConfig:
    return EvaluationConfig.from_dict(config_dict(**kwargs))


def minute_iso(index: int, start: str = "2026-01-01T00:00:00Z") -> str:
    from datetime import datetime, timedelta, timezone

    base = datetime.fromisoformat(start.replace("Z", "+00:00"))
    return (base + timedelta(minutes=index)).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_ndjson(path: Path, records: Iterable[dict[str, Any]]) -> Path:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    return path


def simple_records(count: int, *, failure_burst: bool = False) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index in range(count):
        requests = 100 + index % 11
        errors = 20 if failure_burst and count - 8 <= index < count - 1 else 0
        records.append(
            {
                "minute": minute_iso(index),
                "requests": requests,
                "errors": errors,
            }
        )
    return records


def assert_no_floats(testcase: Any, value: Any) -> None:
    if isinstance(value, dict):
        for item in value.values():
            assert_no_floats(testcase, item)
    elif isinstance(value, list):
        for item in value:
            assert_no_floats(testcase, item)
    else:
        testcase.assertNotIsInstance(value, float)

