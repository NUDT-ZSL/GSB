#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from metric_engine.config import parse_configuration, stable_hash  # noqa: E402


def dump(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    streams = {
        "streams": [
            {
                "tenant": "tenant-a",
                "metric": "api.latency",
                "tags": {"host": "a1", "region": "east"},
                "sample_period_ms": 500,
                "sample_bytes": 6,
            },
            {
                "tenant": "tenant-a",
                "metric": "cpu.util",
                "tags": {"host": "a2", "region": "east"},
                "sample_period_ms": 500,
                "sample_bytes": 7,
            },
            {
                "tenant": "tenant-a",
                "metric": "net.bytes",
                "tags": {"host": "a3", "region": "west"},
                "sample_period_ms": 500,
                "sample_bytes": 8,
            },
            {
                "tenant": "tenant-b",
                "metric": "queue.depth",
                "tags": {"host": "b1", "region": "east"},
                "sample_period_ms": 1000,
                "sample_bytes": 10,
            },
            {
                "tenant": "tenant-b",
                "metric": "errors.total",
                "tags": {"host": "b2", "region": "west"},
                "sample_period_ms": 500,
                "sample_bytes": 4,
            },
            {
                "tenant": "tenant-c",
                "metric": "disk.io",
                "tags": {"host": "c1", "region": "east"},
                "sample_period_ms": 1000,
                "sample_bytes": 10,
            },
        ]
    }
    partitions = {
        "partition_count": 4,
        "buffer_per_partition": 3,
        "flush_round_limit": 4,
        "round_time_budget_ms": 4000,
        "hash_seed": "metric-engine-demo-v1",
        "token_cost_per_sample": 1,
    }
    quotas = {
        "window_ms": 5000,
        "fixed_scale": 1000,
        "comparison_tolerance_bytes": "0.2",
        "tenants": {
            "tenant-a": {
                "window_limit_bytes": "20.0",
                "borrow_limit_bytes": "4.0",
                "allow_borrow": True,
            },
            "tenant-b": {
                "window_limit_bytes": "9.95",
                "borrow_limit_bytes": "0",
                "allow_borrow": False,
            },
            "tenant-c": {
                "window_limit_bytes": "10.05",
                "borrow_limit_bytes": "0",
                "allow_borrow": False,
            },
        },
    }
    tokens = {
        "capacity_tokens": 3,
        "refill_rate_per_second": 1,
        "clock_step_ms": 250,
        "fixed_scale": 1000,
    }

    phase1_events = [
            {
                "event_id": "evt-a-lat-1000-1",
                "tenant": "tenant-a",
                "metric": "api.latency",
                "tags": {"host": "a1", "region": "east"},
                "logical_time_ms": 1000,
                "sample_seq": 1,
            },
            {
                "event_id": "evt-a-cpu-1000-1",
                "tenant": "tenant-a",
                "metric": "cpu.util",
                "tags": {"host": "a2", "region": "east"},
                "logical_time_ms": 1000,
                "sample_seq": 1,
            },
            {
                "event_id": "evt-b-queue-1000-1",
                "tenant": "tenant-b",
                "metric": "queue.depth",
                "tags": {"host": "b1", "region": "east"},
                "logical_time_ms": 1000,
                "sample_seq": 1,
            },
            {
                "event_id": "evt-c-disk-1000-1",
                "tenant": "tenant-c",
                "metric": "disk.io",
                "tags": {"host": "c1", "region": "east"},
                "logical_time_ms": 1000,
                "sample_seq": 1,
            },
    ]
    phase2_events = [
            {
                "event_id": "evt-a-net-6000-1",
                "tenant": "tenant-a",
                "metric": "net.bytes",
                "tags": {"host": "a3", "region": "west"},
                "logical_time_ms": 6000,
                "sample_seq": 1,
            },
            {
                "event_id": "evt-b-errors-6000-1",
                "tenant": "tenant-b",
                "metric": "errors.total",
                "tags": {"host": "b2", "region": "west"},
                "logical_time_ms": 6000,
                "sample_seq": 1,
            },
            {
                "event_id": "evt-c-disk-6000-1",
                "tenant": "tenant-c",
                "metric": "disk.io",
                "tags": {"host": "c1", "region": "east"},
                "logical_time_ms": 6000,
                "sample_seq": 2,
            },
            {
                "event_id": "evt-a-lat-6000-2",
                "tenant": "tenant-a",
                "metric": "api.latency",
                "tags": {"host": "a1", "region": "east"},
                "logical_time_ms": 6000,
                "sample_seq": 2,
            },
            {
                "event_id": "evt-a-cpu-6000-2",
                "tenant": "tenant-a",
                "metric": "cpu.util",
                "tags": {"host": "a2", "region": "east"},
                "logical_time_ms": 6000,
                "sample_seq": 2,
            },
    ]
    events = {"events": phase1_events}
    recovery_events = {
        "events": [
            {
                "event_id": "rec-a-lat-1000-1",
                "tenant": "tenant-a",
                "metric": "api.latency",
                "logical_time_ms": 1000,
                "sample_seq": 1,
            },
            {
                "event_id": "rec-a-cpu-1000-1",
                "tenant": "tenant-a",
                "metric": "cpu.util",
                "logical_time_ms": 1000,
                "sample_seq": 1,
            },
            {
                "event_id": "rec-b-errors-1000-1",
                "tenant": "tenant-b",
                "metric": "errors.total",
                "logical_time_ms": 1000,
                "sample_seq": 1,
            },
            {
                "event_id": "rec-c-disk-1000-1",
                "tenant": "tenant-c",
                "metric": "disk.io",
                "logical_time_ms": 1000,
                "sample_seq": 1,
            },
            {
                "event_id": "rec-a-net-1000-1",
                "tenant": "tenant-a",
                "metric": "net.bytes",
                "logical_time_ms": 1000,
                "sample_seq": 1,
            },
        ]
    }
    config = parse_configuration(streams, partitions, quotas, tokens)
    routing_map = {
        "hash_seed": partitions["hash_seed"],
        "partition_count": partitions["partition_count"],
        "routes": [
            {
                "tenant": stream.tenant,
                "metric": stream.metric,
                "tags": dict(stream.tags),
                "partition": stream.partition,
                "route_hash": stream.route_hash,
            }
            for stream in sorted(config.streams, key=lambda item: (item.tenant, item.metric))
        ],
        "config_hash": stable_hash(
            {
                "streams": streams,
                "partitions": partitions,
                "quotas": quotas,
                "tokens": tokens,
            }
        ),
    }
    recovery_plan = {
        "failpoint": "after_data_append_before_commit",
        "setup": [
            "metric-engine load",
            "metric-engine ingest --events samples/events_recovery.json",
            "METRIC_ENGINE_FAILPOINT=after_data_append_before_commit metric-engine flush --clock-ms 2500",
            "metric-engine resume",
            "metric-engine flush --clock-ms 6500",
            "metric-engine verify",
        ],
        "expected": {
            "crash_exit_code": 86,
            "redo_samples_min": 1,
            "duplicate_ledger_charges": 0,
            "verify_ok": True,
        },
    }
    scenario = {
        "name": "partition-quota-token-recovery-demo",
        "steps": [
            "metric-engine load",
            "metric-engine ingest --events samples/events.json",
            "metric-engine flush --clock-ms 2500",
            "metric-engine ingest --events samples/events_phase2.json",
            "metric-engine flush --clock-ms 6500",
            "metric-engine status",
            "metric-engine verify",
        ],
        "expected": {
            "phase1_ingested": len(phase1_events),
            "phase2_ingested": len(phase2_events),
            "precision_sensitive_kind": "TENANT",
            "verify_ok": True,
        },
    }

    samples = ROOT / "samples"
    dump(samples / "streams.json", streams)
    dump(samples / "partitions.json", partitions)
    dump(samples / "quotas.json", quotas)
    dump(samples / "tokens.json", tokens)
    dump(samples / "events.json", events)
    dump(samples / "events_phase2.json", {"events": phase2_events})
    dump(samples / "events_recovery.json", recovery_events)
    dump(samples / "routing_map.json", routing_map)
    dump(samples / "recovery_plan.json", recovery_plan)
    dump(samples / "scenario.json", scenario)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

