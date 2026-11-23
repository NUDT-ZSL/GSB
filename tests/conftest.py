from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from metric_engine.engine import Engine


DEFAULT_STREAMS: list[dict[str, Any]] = [
    {
        "tenant": "tenant-a",
        "metric": "metric.one",
        "tags": {"host": "h1"},
        "sample_period_ms": 500,
        "sample_bytes": 4,
    }
]
DEFAULT_PARTITIONS: dict[str, Any] = {
    "partition_count": 4,
    "buffer_per_partition": 3,
    "flush_round_limit": 10,
    "round_time_budget_ms": 10000,
    "hash_seed": "test-seed-v1",
    "token_cost_per_sample": 1,
}
DEFAULT_QUOTAS: dict[str, Any] = {
    "window_ms": 5000,
    "fixed_scale": 1000,
    "comparison_tolerance_bytes": "0",
    "tenants": {
        "tenant-a": {
            "window_limit_bytes": "100",
            "borrow_limit_bytes": "0",
            "allow_borrow": False,
        }
    },
}
DEFAULT_TOKENS: dict[str, Any] = {
    "capacity_tokens": 100,
    "refill_rate_per_second": 1,
    "clock_step_ms": 100,
    "fixed_scale": 1000,
}


class WorkspaceBuilder:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.runtime = root / "runtime"
        self._config_index = 0

    def write_config(
        self,
        *,
        streams: list[dict[str, Any]] | None = None,
        partitions: dict[str, Any] | None = None,
        quotas: dict[str, Any] | None = None,
        tokens: dict[str, Any] | None = None,
    ) -> dict[str, Path]:
        self._config_index += 1
        directory = self.root / f"config-{self._config_index}"
        directory.mkdir(parents=True, exist_ok=True)
        documents = {
            "streams": deepcopy(streams if streams is not None else DEFAULT_STREAMS),
            "partitions": deepcopy(
                partitions if partitions is not None else DEFAULT_PARTITIONS
            ),
            "quotas": deepcopy(quotas if quotas is not None else DEFAULT_QUOTAS),
            "tokens": deepcopy(tokens if tokens is not None else DEFAULT_TOKENS),
        }
        paths: dict[str, Path] = {}
        payloads = {
            "streams": {"streams": documents["streams"]},
            "partitions": documents["partitions"],
            "quotas": documents["quotas"],
            "tokens": documents["tokens"],
        }
        for key, payload in payloads.items():
            path = directory / f"{key}.json"
            path.write_text(
                json.dumps(payload, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            paths[key] = path
        return paths

    def create(
        self,
        *,
        streams: list[dict[str, Any]] | None = None,
        partitions: dict[str, Any] | None = None,
        quotas: dict[str, Any] | None = None,
        tokens: dict[str, Any] | None = None,
    ) -> tuple[Engine, dict[str, Path]]:
        paths = self.write_config(
            streams=streams,
            partitions=partitions,
            quotas=quotas,
            tokens=tokens,
        )
        engine, _report = Engine.load(
            self.runtime,
            streams_path=paths["streams"],
            partitions_path=paths["partitions"],
            quotas_path=paths["quotas"],
            tokens_path=paths["tokens"],
        )
        return engine, paths

    def write_events(self, name: str, events: list[dict[str, Any]]) -> Path:
        path = self.root / name
        path.write_text(
            json.dumps({"events": events}, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return path


@pytest.fixture
def workspace(tmp_path: Path) -> WorkspaceBuilder:
    return WorkspaceBuilder(tmp_path)


def event(
    *,
    tenant: str = "tenant-a",
    metric: str = "metric.one",
    logical_time_ms: int = 1000,
    sample_seq: int = 1,
    tags: dict[str, str] | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tenant": tenant,
        "metric": metric,
        "logical_time_ms": logical_time_ms,
        "sample_seq": sample_seq,
    }
    if tags is not None:
        payload["tags"] = tags
    if event_id is not None:
        payload["event_id"] = event_id
    return payload

