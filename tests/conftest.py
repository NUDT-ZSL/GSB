from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def read_sample(name: str) -> Any:
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


def load_sample_docs() -> tuple[Any, Any, Any, Any]:
    return (
        read_sample("topics.json"),
        read_sample("events.json"),
        read_sample("subscriptions.json"),
        read_sample("config.json"),
    )


@pytest.fixture
def sample_docs() -> tuple[Any, Any, Any, Any]:
    return load_sample_docs()


def one_partition_docs(
    *,
    event_count: int = 1,
    backlog_limit: int = 8,
    retry_budget: int = 2,
    failure_rate: float = 0.0,
    budget_ms: int = 200,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    topics = {
        "topics": [
            {
                "name": "only",
                "partition_count": 1,
                "partition_key_rule": "sha256_mod_v1",
                "backlog_limit": backlog_limit,
                "out_of_order_tolerance_window": 1,
            }
        ]
    }
    events = {
        "events": [
            {
                "event_seq": index,
                "event_id": f"only-{index}",
                "topic": "only",
                "partition_key": "same-key",
                "payload_digest": f"sha256:{index:064x}",
                "injected_clock_ms": index * 10,
                "expected_delivery_generation": 1,
            }
            for index in range(event_count)
        ]
    }
    subscriptions = {
        "subscriptions": [
            {
                "subscriber_id": "sub-only",
                "topic": "only",
                "pull_batch_size": 10,
                "consume_rate_per_sec": 100,
                "failure_rate": failure_rate,
                "retry_budget": retry_budget,
            }
        ]
    }
    config = {
        "partition_lock_granularity": "partition",
        "catchup_logical_time_budget_ms": budget_ms,
        "global_concurrency_limit": 2,
        "retry_backoff_base_ms": 5,
        "retry_backoff_max_ms": 20,
        "fixed_scale": 1000,
        "clock_tolerance_ms": 5,
        "deterministic_seed": 7,
        "adaptive_lock_enabled": True,
        "lock_switch_backlog_threshold": 2,
        "lock_switch_inflight_threshold": 2,
        "segment_size": 2,
        "lock_cost_ms": 1,
    }
    return topics, events, subscriptions, config
