"""Deterministic sample generation for the three supported schemas."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

MIB = 1024 * 1024

# Two replicas per rack, 16 MiB each.  The fourth event grows r0, which keeps
# the final state feasible while the temporary shrink creates observable
# routing pressure.
REPLICA_DEFS = (
    ("r0", "rack-a", ("bf16", "fp8"), 16 * MIB),
    ("r1", "rack-a", ("bf16",), 16 * MIB),
    ("r2", "rack-b", ("bf16",), 16 * MIB),
    ("r3", "rack-b", ("bf16", "fp8"), 16 * MIB),
)

EXPERT_DEFS = (
    ("e0", "r0", ("general",), 47_000, 6 * MIB, "bf16"),
    ("e1", "r3", ("general",), 61_000, 96 * MIB, "fp8"),
    ("e2", "r1", ("code",), 98_000, 2 * MIB, "bf16"),
    ("e3", "r2", ("code",), 132_000, MIB // 4, "bf16"),
    ("e4", "r0", ("math",), 34_000, 18 * MIB, "fp8"),
    ("e5", "r2", ("math",), 79_000, 64 * MIB, "bf16"),
    ("e6", "r1", ("vision",), 18_000, 36 * MIB, "bf16"),
    ("e7", "r3", ("vision",), 26_000, 3 * MIB // 4, "bf16"),
)

# Entropy from the seed is confined to sample generation; these values make
# the example useful for capacity planning and exercise all normalization
# paths in a small data set.
DOMAINS = ("general", "math", "code", "vision")
DOMAIN_WEIGHTS = (50, 25, 15, 10)
SEQUENCE_CHOICES = (64, 96, 128, 192, 256)
SEQUENCE_WEIGHTS = (15, 20, 25, 20, 20)


def _domain_costs() -> dict[str, float]:
    costs: dict[str, list[int]] = {domain: [] for domain in DOMAINS}
    for _, _, domains, cost, _, _ in EXPERT_DEFS:
        costs[domains[0]].append(cost)
    return {
        domain: sum(values) / len(values) for domain, values in costs.items()
    }


def _calibrated_requests(seed: int, request_count: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    average_cost = _domain_costs()
    initial_capacity = sum(item[3] for item in REPLICA_DEFS)
    target_work = int(initial_capacity * 0.82)

    provisional: list[dict[str, Any]] = []
    estimated_work = 0.0
    for index in range(request_count):
        domain = rng.choices(DOMAINS, weights=DOMAIN_WEIGHTS, k=1)[0]
        sequence_length = rng.choices(
            SEQUENCE_CHOICES, weights=SEQUENCE_WEIGHTS, k=1
        )[0]
        blocks = max(1, (sequence_length + 127) // 128)
        sample_units = rng.randint(8, 40)
        provisional.append(
            {
                "request_id": f"R{index + 1:04d}",
                "arrival_time": (index + 1) * 10,
                "domain": domain,
                "sample_units": sample_units,
                "sequence_length": sequence_length,
                "blocks": blocks,
            }
        )
        estimated_work += sample_units * blocks * average_cost[domain]

    if estimated_work <= 0:
        raise ValueError("request_count must be positive")
    # Small request counts cannot absorb the full calibration target without
    # creating individual requests larger than a replica.  A bounded scale
    # keeps every request routable while still exercising near-saturation
    # behavior for the default 24-request demonstration.
    scale = min(1.5, target_work / estimated_work)
    for item in provisional:
        item["sample_units"] = max(
            4, min(120, int(round(item["sample_units"] * scale)))
        )
        del item["blocks"]
    return provisional


def _events(requests: list[dict[str, Any]], seed: int) -> list[dict[str, Any]]:
    count = len(requests)
    if not requests:
        raise ValueError("request_count must be positive")

    def at(fraction: float, offset: int) -> int:
        index = min(count - 1, max(0, int(count * fraction)))
        return int(requests[index]["arrival_time"]) + offset

    # A hot math expert moves between replicas, then r0 is temporarily shrunk
    # and finally expanded.  The event times are derived only from request
    # count and arrival order, so they are reproducible.
    migration = {
        "event_id": "E001",
        "at": at(0.50, -5),
        "kind": "migrate_expert",
        "expert_id": "e5",
        "from_replica": "r2",
        "to_replica": "r3",
    }
    shrink = {
        "event_id": "E002",
        "at": at(2.0 / 3.0, 5),
        "kind": "scale_replica",
        "replica_id": "r0",
        "new_capacity_bytes": 11 * MIB,
    }
    grow = {
        "event_id": "E003",
        "at": at(4.0 / 5.0, 5),
        "kind": "scale_replica",
        "replica_id": "r0",
        "new_capacity_bytes": 17 * MIB,
    }
    events = [migration, shrink, grow]
    events.sort(key=lambda event: (event["at"], event["event_id"]))
    return events


def _base_dataset(seed: int, request_count: int) -> dict[str, Any]:
    if request_count <= 0:
        raise ValueError("request_count must be positive")
    sample_equivalent_bytes = 4096
    reference_sequence_length = 128
    requests = _calibrated_requests(seed, request_count)
    for request in requests:
        request["equivalent_bytes"] = (
            request["sample_units"] * sample_equivalent_bytes
        )
    return {
        "project": f"moe-capacity-seed-{seed}",
        "seed": seed,
        "request_count": request_count,
        "sample_equivalent_bytes": sample_equivalent_bytes,
        "reference_sequence_length": reference_sequence_length,
        "replicas": [
            {
                "replica_id": replica_id,
                "rack": rack,
                "supported_precisions": list(precisions),
                "capacity_bytes": capacity,
            }
            for replica_id, rack, precisions, capacity in REPLICA_DEFS
        ],
        "experts": [
            {
                "expert_id": expert_id,
                "home_replica": home,
                "domains": list(domains),
                "per_sample_cost_bytes": cost,
                "cross_rack_comm_bytes": comm,
                "precision": precision,
            }
            for expert_id, home, domains, cost, comm, precision in EXPERT_DEFS
        ],
        "requests": requests,
        "events": _events(requests, seed),
    }


def _metadata(base: dict[str, Any]) -> dict[str, Any]:
    return {
        "generator": "moe_capacity.generator",
        "request_count": base["request_count"],
        "seed": base["seed"],
    }


def _render_v1(base: dict[str, Any]) -> dict[str, Any]:
    return {
        "events": [
            (
                {
                    "event_id": event["event_id"],
                    "expert_id": event["expert_id"],
                    "from_replica_id": event["from_replica"],
                    "logical_time": event["at"],
                    "to_replica_id": event["to_replica"],
                    "type": "migrate_expert",
                }
                if event["kind"] == "migrate_expert"
                else {
                    "capacity_bytes": event["new_capacity_bytes"],
                    "event_id": event["event_id"],
                    "logical_time": event["at"],
                    "replica_id": event["replica_id"],
                    "type": "scale_replica",
                }
            )
            for event in base["events"]
        ],
        "experts": [
            {
                "cost_per_sample": expert["per_sample_cost_bytes"],
                "cross_replica_comm_mb": round(
                    expert["cross_rack_comm_bytes"] / 1_000_000, 6
                ),
                "domain_tags": list(expert["domains"]),
                "id": expert["expert_id"],
                "precision": expert["precision"],
                "replica_id": expert["home_replica"],
            }
            for expert in base["experts"]
        ],
        "metadata": _metadata(base),
        "project": base["project"],
        "replicas": [
            {
                "capacity_bytes": replica["capacity_bytes"],
                "id": replica["replica_id"],
                "precision": "/".join(replica["supported_precisions"]),
                "rack": replica["rack"],
            }
            for replica in base["replicas"]
        ],
        "requests": [
            {
                "domain": request["domain"],
                "logical_time": request["arrival_time"],
                "request_id": request["request_id"],
                "sample_count": request["sample_units"],
                "sequence_length": request["sequence_length"],
            }
            for request in base["requests"]
        ],
        "schema_version": "1.0",
        "units": {
            "expert_comm": "MB",
            "reference_sequence_length": base["reference_sequence_length"],
            "request_load": "samples",
            "sample_equiv_bytes": base["sample_equivalent_bytes"],
        },
    }


def _render_v2(base: dict[str, Any]) -> dict[str, Any]:
    experts_by_replica: dict[str, list[dict[str, Any]]] = {
        replica["replica_id"]: [] for replica in base["replicas"]
    }
    for expert in base["experts"]:
        experts_by_replica[expert["home_replica"]].append(
            {
                "cross_rack_comm": {
                    "unit": "MiB",
                    "value": expert["cross_rack_comm_bytes"] / MIB,
                },
                "domains": list(expert["domains"]),
                "expert_id": expert["expert_id"],
                "precision": expert["precision"],
                "sample_cost": {
                    "unit": "equiv_byte",
                    "value": expert["per_sample_cost_bytes"],
                },
            }
        )
    return {
        "defaults": {
            "comm_reference_sequence_length": base["reference_sequence_length"],
            "sample_equivalent_bytes": base["sample_equivalent_bytes"],
        },
        "metadata": _metadata(base),
        "operations": [
            (
                {
                    "at": event["at"],
                    "expert_id": event["expert_id"],
                    "op": "move_expert",
                    "operation_id": event["event_id"],
                    "source_replica_id": event["from_replica"],
                    "target_replica_id": event["to_replica"],
                }
                if event["kind"] == "migrate_expert"
                else {
                    "at": event["at"],
                    "capacity": {
                        "unit": "equiv_byte",
                        "value": event["new_capacity_bytes"],
                    },
                    "op": "resize_replica",
                    "operation_id": event["event_id"],
                    "replica_id": event["replica_id"],
                }
            )
            for event in base["events"]
        ],
        "project": base["project"],
        "replicas": [
            {
                "capacity": {
                    "unit": "equiv_byte",
                    "value": replica["capacity_bytes"],
                },
                "experts": experts_by_replica[replica["replica_id"]],
                "rack_id": replica["rack"],
                "replica_id": replica["replica_id"],
                "supported_precisions": list(replica["supported_precisions"]),
            }
            for replica in base["replicas"]
        ],
        "requests": [
            {
                "at": request["arrival_time"],
                "field": request["domain"],
                "id": request["request_id"],
                "payload_bytes": request["equivalent_bytes"],
                "sequence_length": request["sequence_length"],
            }
            for request in base["requests"]
        ],
        "schema": "moe.service/v2",
    }


def _render_v3(base: dict[str, Any]) -> dict[str, Any]:
    branches_by_replica: dict[str, list[dict[str, Any]]] = {
        replica["replica_id"]: [] for replica in base["replicas"]
    }
    for expert in base["experts"]:
        branches_by_replica[expert["home_replica"]].append(
            {
                "cost": {"bytes_per_sample": expert["per_sample_cost_bytes"]},
                "domains": list(expert["domains"]),
                "egress": {
                    "mebibytes_per_round": (
                        expert["cross_rack_comm_bytes"] / MIB
                    )
                },
                "numeric_mode": expert["precision"],
                "shard": expert["expert_id"],
            }
        )
    return {
        "changes": [
            (
                {
                    "change_id": event["event_id"],
                    "destination": event["to_replica"],
                    "op": "relocate_shard",
                    "origin": event["from_replica"],
                    "shard": event["expert_id"],
                    "tick": event["at"],
                }
                if event["kind"] == "migrate_expert"
                else {
                    "capacity": {
                        "weighted_load_bytes": event["new_capacity_bytes"]
                    },
                    "change_id": event["event_id"],
                    "op": "resize_worker",
                    "tick": event["at"],
                    "worker": event["replica_id"],
                }
            )
            for event in base["events"]
        ],
        "format": "moe-capacity/v3",
        "inference": [
            {
                "context_tokens": request["sequence_length"],
                "rid": request["request_id"],
                "tick": request["arrival_time"],
                "topic": request["domain"],
                "work": {"samples": request["sample_units"]},
            }
            for request in base["requests"]
        ],
        "metadata": _metadata(base),
        "normalization": {
            "communication": "cross_rack_only",
            "sample_equivalent_bytes": base["sample_equivalent_bytes"],
            "sequence_quantum": base["reference_sequence_length"],
        },
        "project": base["project"],
        "topology": {
            "nodes": [
                {
                    "branches": branches_by_replica[replica["replica_id"]],
                    "limits": {
                        "weighted_load_bytes": replica["capacity_bytes"]
                    },
                    "precision_profiles": list(replica["supported_precisions"]),
                    "rack": {"id": replica["rack"]},
                    "worker": replica["replica_id"],
                }
                for replica in base["replicas"]
            ]
        },
    }


def generate_project_set(
    seed: int = 42,
    request_count: int = 24,
) -> dict[str, dict[str, Any]]:
    """Return three semantically identical schema documents.

    The same seed and request count always produce byte-identical documents.
    """

    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    if isinstance(request_count, bool) or not isinstance(request_count, int):
        raise TypeError("request_count must be an integer")
    base = _base_dataset(seed, request_count)
    return {
        "v1": _render_v1(base),
        "v2": _render_v2(base),
        "v3": _render_v3(base),
    }


def write_project_set(
    documents: dict[str, dict[str, Any]],
    output_dir: str | Path,
) -> dict[str, str]:
    """Write ``project_v1.json`` ... and return the written paths."""

    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    for version in ("v1", "v2", "v3"):
        path = target / f"project_{version}.json"
        path.write_text(
            json.dumps(documents[version], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        paths[version] = str(path)
    return paths

