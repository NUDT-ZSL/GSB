"""Generate the checked-in deterministic local sample set.

The generated JSON files contain no wall-clock timestamps and no network data.
Run from the repository root with:

    python samples/generate_samples.py
"""

from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent
QUANTUM = Decimal("0.001")


def write_json(name: str, payload: object) -> None:
    (ROOT / name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def quantize(value: Decimal) -> str:
    return format(value.quantize(QUANTUM, rounding=ROUND_HALF_UP), "f")


def interpolate_keyframes(
    metric_id: str,
    keyframes: Iterable[tuple[int, str]],
) -> list[dict[str, object]]:
    points = [(timestamp, Decimal(value)) for timestamp, value in keyframes]
    samples: list[dict[str, object]] = []
    for index in range(0, 601_000, 15_000):
        for position in range(len(points) - 1):
            left_time, left_value = points[position]
            right_time, right_value = points[position + 1]
            if left_time <= index <= right_time:
                if right_time == left_time:
                    value = left_value
                else:
                    fraction = Decimal(index - left_time) / Decimal(
                        right_time - left_time
                    )
                    value = left_value + (right_value - left_value) * fraction
                break
        else:
            value = points[-1][1]
        samples.append(
            {
                "metricSourceId": metric_id,
                "timestampMs": index,
                "value": quantize(value),
            }
        )
    return samples


workloads = {
    "schemaVersion": 1,
    "workloads": [
        {
            "id": "wl-api",
            "name": "api",
            "minReplicas": 2,
            "maxReplicas": 12,
            "currentReplicas": 4,
            "cpuMillicoresPerReplica": 500,
            "memoryMiBPerReplica": 512,
            "topologyDomain": "zone-a",
        },
        {
            "id": "wl-queue",
            "name": "queue",
            "minReplicas": 1,
            "maxReplicas": 10,
            "currentReplicas": 3,
            "cpuMillicoresPerReplica": 300,
            "memoryMiBPerReplica": 256,
            "topologyDomain": "zone-a",
        },
        {
            "id": "wl-search",
            "name": "search",
            "minReplicas": 2,
            "maxReplicas": 15,
            "currentReplicas": 5,
            "cpuMillicoresPerReplica": 700,
            "memoryMiBPerReplica": 1024,
            "topologyDomain": "zone-b",
        },
        {
            "id": "wl-cache",
            "name": "cache",
            "minReplicas": 2,
            "maxReplicas": 8,
            "currentReplicas": 2,
            "cpuMillicoresPerReplica": 200,
            "memoryMiBPerReplica": 768,
            "topologyDomain": "zone-c",
        },
        {
            "id": "wl-batch",
            "name": "batch",
            "minReplicas": 1,
            "maxReplicas": 6,
            "currentReplicas": 1,
            "cpuMillicoresPerReplica": 900,
            "memoryMiBPerReplica": 2048,
            "topologyDomain": "zone-c",
        },
    ],
}

metric_sources = {
    "schemaVersion": 1,
    "metricSources": [
        {
            "id": "ms-api-cpu",
            "name": "api_cpu_utilization",
            "aggregation": "avg",
            "windowMs": 30000,
            "targetValue": "55",
            "alertThreshold": "70",
        },
        {
            "id": "ms-api-latency",
            "name": "api_latency_p95",
            "aggregation": "percentile",
            "percentile": "0.95",
            "windowMs": 60000,
            "targetValue": "250",
            "alertThreshold": "350",
        },
        {
            "id": "ms-queue-depth",
            "name": "queue_depth",
            "aggregation": "avg",
            "windowMs": 30000,
            "targetValue": "20",
            "alertThreshold": "45",
        },
        {
            "id": "ms-search-latency",
            "name": "search_latency_p90",
            "aggregation": "percentile",
            "percentile": "0.90",
            "windowMs": 60000,
            "targetValue": "180",
            "alertThreshold": "240",
        },
        {
            "id": "ms-cache-hit",
            "name": "cache_hit_rate",
            "aggregation": "avg",
            "windowMs": 30000,
            "targetValue": "88",
            "alertThreshold": "80",
        },
        {
            "id": "ms-batch-queue",
            "name": "batch_queue_depth",
            "aggregation": "avg",
            "windowMs": 60000,
            "targetValue": "10",
            "alertThreshold": "25",
        },
    ],
}

scaling_rules = {
    "schemaVersion": 1,
    "scalingRules": [
        {
            "id": "r-api-cpu-out",
            "name": "api-cpu-scale-out",
            "workloadId": "wl-api",
            "metricSourceId": "ms-api-cpu",
            "direction": "scale_out",
            "step": 2,
            "percent": "25",
            "priority": 1,
            "cooldownMs": 45000,
        },
        {
            "id": "r-queue-fast-out",
            "name": "queue-fast-scale-out",
            "workloadId": "wl-queue",
            "metricSourceId": "ms-queue-depth",
            "direction": "scale_out",
            "step": 1,
            "percent": "10",
            "priority": 2,
            "cooldownMs": 30000,
        },
        {
            "id": "r-api-latency-out",
            "name": "api-latency-scale-out",
            "workloadId": "wl-api",
            "metricSourceId": "ms-api-latency",
            "direction": "scale_out",
            "step": 1,
            "percent": "20",
            "priority": 3,
            "cooldownMs": 60000,
        },
        {
            "id": "r-queue-steady-out",
            "name": "queue-steady-scale-out",
            "workloadId": "wl-queue",
            "metricSourceId": "ms-queue-depth",
            "direction": "scale_out",
            "step": 2,
            "percent": "30",
            "priority": 4,
            "cooldownMs": 30000,
        },
        {
            "id": "r-api-cpu-in",
            "name": "api-cpu-scale-in",
            "workloadId": "wl-api",
            "metricSourceId": "ms-api-cpu",
            "direction": "scale_in",
            "step": 1,
            "percent": "10",
            "priority": 5,
            "cooldownMs": 60000,
        },
        {
            "id": "r-queue-in",
            "name": "queue-scale-in",
            "workloadId": "wl-queue",
            "metricSourceId": "ms-queue-depth",
            "direction": "scale_in",
            "step": 1,
            "percent": "10",
            "priority": 6,
            "cooldownMs": 45000,
        },
        {
            "id": "r-search-out",
            "name": "search-scale-out",
            "workloadId": "wl-search",
            "metricSourceId": "ms-search-latency",
            "direction": "scale_out",
            "step": 2,
            "percent": "25",
            "priority": 7,
            "cooldownMs": 45000,
        },
        {
            "id": "r-search-in",
            "name": "search-scale-in",
            "workloadId": "wl-search",
            "metricSourceId": "ms-search-latency",
            "direction": "scale_in",
            "step": 1,
            "percent": "10",
            "priority": 8,
            "cooldownMs": 60000,
        },
        {
            "id": "r-cache-in",
            "name": "cache-scale-in",
            "workloadId": "wl-cache",
            "metricSourceId": "ms-cache-hit",
            "direction": "scale_in",
            "step": 1,
            "percent": "15",
            "priority": 9,
            "cooldownMs": 45000,
        },
        {
            "id": "r-cache-out",
            "name": "cache-scale-out",
            "workloadId": "wl-cache",
            "metricSourceId": "ms-cache-hit",
            "direction": "scale_out",
            "step": 1,
            "percent": "10",
            "priority": 10,
            "cooldownMs": 30000,
        },
        {
            "id": "r-batch-out",
            "name": "batch-scale-out",
            "workloadId": "wl-batch",
            "metricSourceId": "ms-batch-queue",
            "direction": "scale_out",
            "step": 3,
            "percent": "50",
            "priority": 11,
            "cooldownMs": 90000,
        },
        {
            "id": "r-batch-in",
            "name": "batch-scale-in",
            "workloadId": "wl-batch",
            "metricSourceId": "ms-batch-queue",
            "direction": "scale_in",
            "step": 1,
            "percent": "20",
            "priority": 12,
            "cooldownMs": 90000,
        },
    ],
}

resource_budget = {
    "schemaVersion": 1,
    "resourceBudget": {
        "id": "budget-global",
        "name": "global-pool",
        "poolCoreHours": "0.001",
        "memoryLimitMiB": 12288,
        "roundTimeBudgetMs": 2200,
        "maxConcurrentActionsPerWorkload": 1,
        "actionBaseDurationMs": 200,
        "actionDurationPerReplicaMs": 100,
        "clockRollbackToleranceMs": 250,
        "failureBackoffBaseMs": 1000,
        "failureBackoffMaxMs": 16000,
        "failureBackoffJitterRatio": "0.2",
        "freezeWindows": [
            {
                "id": "freeze-maintenance",
                "startMs": 180000,
                "endMs": 210000,
            },
            {
                "id": "freeze-second",
                "startMs": 540000,
                "endMs": 555000,
            },
        ],
    },
}

keyframes = {
    "ms-api-cpu": [
        (0, "42"),
        (30000, "50"),
        (60000, "61"),
        (90000, "70"),
        (120000, "79"),
        (150000, "83"),
        (180000, "81"),
        (210000, "86"),
        (240000, "78"),
        (300000, "52"),
        (360000, "44"),
        (420000, "40"),
        (480000, "38"),
        (540000, "36"),
        (600000, "35"),
    ],
    "ms-api-latency": [
        (0, "170"),
        (30000, "210"),
        (60000, "260"),
        (90000, "330"),
        (120000, "390"),
        (150000, "430"),
        (180000, "440"),
        (210000, "420"),
        (240000, "360"),
        (300000, "260"),
        (360000, "210"),
        (420000, "180"),
        (480000, "170"),
        (540000, "160"),
        (600000, "150"),
    ],
    "ms-queue-depth": [
        (0, "15"),
        (30000, "22"),
        (60000, "31"),
        (90000, "44"),
        (120000, "53"),
        (150000, "58"),
        (180000, "49"),
        (210000, "42"),
        (240000, "36"),
        (300000, "21"),
        (360000, "12"),
        (420000, "8"),
        (480000, "6"),
        (540000, "5"),
        (600000, "4"),
    ],
    "ms-search-latency": [
        (0, "90"),
        (30000, "130"),
        (60000, "175"),
        (90000, "230"),
        (120000, "285"),
        (150000, "320"),
        (180000, "300"),
        (210000, "260"),
        (240000, "205"),
        (300000, "145"),
        (360000, "110"),
        (420000, "95"),
        (480000, "85"),
        (540000, "80"),
        (600000, "75"),
    ],
    "ms-cache-hit": [
        (0, "92"),
        (30000, "90"),
        (60000, "89"),
        (90000, "86"),
        (120000, "84"),
        (150000, "81"),
        (180000, "74"),
        (210000, "71"),
        (240000, "76"),
        (300000, "83"),
        (360000, "88"),
        (420000, "90"),
        (480000, "91"),
        (540000, "92"),
        (600000, "93"),
    ],
    "ms-batch-queue": [
        (0, "7"),
        (30000, "9"),
        (60000, "13"),
        (90000, "20"),
        (120000, "27"),
        (150000, "33"),
        (180000, "36"),
        (210000, "30"),
        (240000, "22"),
        (300000, "11"),
        (360000, "7"),
        (420000, "5"),
        (480000, "4"),
        (540000, "3"),
        (600000, "2"),
    ],
}

samples = []
for metric_id, frames in keyframes.items():
    samples.extend(interpolate_keyframes(metric_id, frames))
samples.sort(key=lambda item: (item["metricSourceId"], item["timestampMs"]))
metric_stream = {"schemaVersion": 1, "samples": samples}

rule_patches = {
    "schemaVersion": 1,
    "patchId": "patch-2026-queue-search-extension",
    "baseConfigVersion": 1,
    "patches": [
        {
            "operationId": "op-modify-queue-metric",
            "op": "modify",
            "objectType": "metric_source",
            "objectId": "ms-queue-depth",
            "expectedVersion": 1,
            "changes": {
                "windowMs": 60000,
                "targetValue": "18",
            },
        },
        {
            "operationId": "op-modify-queue-rule",
            "op": "modify",
            "objectType": "rule",
            "objectId": "r-queue-fast-out",
            "expectedVersion": 1,
            "changes": {
                "percent": "15",
            },
        },
        {
            "operationId": "op-modify-api-in-rule",
            "op": "modify",
            "objectType": "rule",
            "objectId": "r-api-cpu-in",
            "expectedVersion": 1,
            "changes": {
                "percent": "15",
            },
        },
        {
            "operationId": "op-delete-search-in",
            "op": "delete",
            "objectType": "rule",
            "objectId": "r-search-in",
            "expectedVersion": 1,
        },
        {
            "operationId": "op-add-realtime-workload",
            "op": "add",
            "objectType": "workload",
            "objectId": "wl-realtime",
            "object": {
                "id": "wl-realtime",
                "name": "realtime",
                "minReplicas": 2,
                "maxReplicas": 8,
                "currentReplicas": 2,
                "cpuMillicoresPerReplica": 300,
                "memoryMiBPerReplica": 384,
                "topologyDomain": "edge-a",
            },
        },
        {
            "operationId": "op-add-realtime-metric",
            "op": "add",
            "objectType": "metric_source",
            "objectId": "ms-realtime-queue",
            "object": {
                "id": "ms-realtime-queue",
                "name": "realtime_queue_depth",
                "aggregation": "avg",
                "windowMs": 30000,
                "targetValue": "12",
                "alertThreshold": "25",
            },
        },
        {
            "operationId": "op-add-realtime-rule",
            "op": "add",
            "objectType": "rule",
            "objectId": "r-realtime-out",
            "object": {
                "id": "r-realtime-out",
                "name": "realtime-scale-out",
                "workloadId": "wl-realtime",
                "metricSourceId": "ms-realtime-queue",
                "direction": "scale_out",
                "step": 1,
                "percent": "50",
                "priority": 13,
                "cooldownMs": 30000,
            },
        },
    ],
}

action_outcomes = {
    "schemaVersion": 1,
    "rules": [
        {
            "ruleId": "r-search-out",
            "logicalNowMs": 120000,
            "outcome": "failure",
            "reason": "simulated_replica_provisioning_failure",
        }
    ],
}

write_json("workloads.json", workloads)
write_json("metric_sources.json", metric_sources)
write_json("scaling_rules.json", scaling_rules)
write_json("resource_budget.json", resource_budget)
write_json("metric_stream.json", metric_stream)
write_json("rule_patches.json", rule_patches)
write_json("action_outcomes.json", action_outcomes)

