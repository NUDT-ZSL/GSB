"""Shared deterministic fixtures for unit and process tests."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from quota_ledger.utils import read_json

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def sample_quotas() -> dict:
    return read_json(PROJECT_ROOT / "samples" / "quotas.json")


def sample_limiter() -> dict:
    return read_json(PROJECT_ROOT / "samples" / "limiter.json")


def sample_events() -> list[dict]:
    return list(read_json(PROJECT_ROOT / "samples" / "events.json")["events"])


def sample_event(seq: int) -> dict:
    for event in sample_events():
        if event["seq"] == seq:
            return deepcopy(event)
    raise KeyError(seq)


def multiprocess_config() -> tuple[dict, dict]:
    quota = {
        "version": 1,
        "tenants": [
            {
                "name": "parallel",
                "max_bytes": 10000,
                "max_objects": 1000,
                "soft_pct": 90,
                "hard_pct": 100,
                "buckets": [
                    {
                        "name": "media",
                        "max_bytes": 10000,
                        "max_objects": 1000,
                        "soft_pct": 90,
                        "hard_pct": 100,
                        "prefixes": [
                            {
                                "key": f"p{index}",
                                "max_bytes": 10000,
                                "max_objects": 1000,
                                "soft_pct": 90,
                                "hard_pct": 100,
                            }
                            for index in range(4)
                        ],
                    }
                ],
            }
        ],
    }
    limiter = {
        "version": 1,
        "clock_rollback_tolerance_ms": 1000000,
        "tenants": [
            {
                "tenant": "parallel",
                "requests_per_second": 100000,
                "bucket_capacity": 1000,
                "refill_rate_per_second": 100000,
                "burst_depth": 0,
                "backoff_window_ms": 1,
            }
        ],
    }
    return quota, limiter

