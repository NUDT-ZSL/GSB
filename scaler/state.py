from __future__ import annotations

from typing import Any

from .errors import BusinessError
from .models import ScalerSpec
from .packing import make_initial_ledger

SCHEMA_VERSION = 1
OCCUPYING_PHASES = {"COLD_STARTING", "READY", "DRAINING"}
PHASES = {"PENDING", "COLD_STARTING", "READY", "DRAINING", "RECYCLED"}


def initial_state(spec: ScalerSpec) -> dict[str, Any]:
    services = {}
    for name in sorted(spec.services):
        service = spec.services[name]
        services[name] = {
            "target_replicas": service.min_replicas,
            "available_replicas": 0,
            "cold_starting_replicas": 0,
            "pending_replicas": 0,
            "draining_replicas": 0,
            "launch_failure_streak": 0,
            "health_failure_streak": 0,
            "last_conversion": None,
            "last_round_outcome": None,
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "spec": spec.to_dict(),
        "config_version": spec.config.config_version,
        "spec_digest": spec.digest,
        "logical_clock_ms": 0,
        "round_watermark": 0,
        "state_revision": 0,
        "next_round_seq": 1,
        "next_replica_seq": {name: 1 for name in sorted(spec.services)},
        "services": services,
        "nodes": make_initial_ledger(spec.nodes),
        "replicas": {},
        "pending_rounds": {},
        "placement_decisions": {},
        "round_history": [],
        "requests": {},
    }


def get_spec(state: dict[str, Any]) -> ScalerSpec:
    return ScalerSpec.from_dict(state["spec"])


def active_pending_count(state: dict[str, Any]) -> int:
    return len(state.get("pending_rounds", {}))


def replica_workload(service_spec: Any) -> dict[str, Any]:
    return {
        "memory_mib": service_spec.replica_memory_mib,
        "compute_units": service_spec.replica_compute_units,
        "cold_start_budget_ms": service_spec.cold_start_ms,
    }


def set_replica_phase(
    replica: dict[str, Any],
    phase: str,
    *,
    round_no: int,
    clock_ms: int,
) -> None:
    if phase not in PHASES:
        raise BusinessError(
            "E_STORAGE_ERROR",
            f"invalid replica phase '{phase}'",
            {"replica_id": replica.get("replica_id")},
        )
    replica["phase"] = phase
    replica["phase_round"] = round_no
    replica["phase_clock_ms"] = clock_ms


def release_replica_allocation(state: dict[str, Any], replica: dict[str, Any]) -> None:
    node_name = replica.get("node")
    gpu_index = replica.get("gpu_index")
    memory = replica.get("memory_mib", 0)
    if node_name is None:
        return
    node = state["nodes"].get(node_name)
    if node is None:
        raise BusinessError(
            "E_LEDGER_MISMATCH",
            "replica references a node missing from the ledger",
            {"replica_id": replica["replica_id"], "node": node_name},
        )
    if not isinstance(gpu_index, int) or not (0 <= gpu_index < len(node["gpu_remaining_mib"])):
        raise BusinessError(
            "E_LEDGER_MISMATCH",
            "replica references an invalid GPU index",
            {"replica_id": replica["replica_id"], "gpu_index": gpu_index},
        )
    node["gpu_remaining_mib"][gpu_index] += memory
    node["used_instances"] -= 1
    if node["used_instances"] < 0:
        raise BusinessError(
            "E_LEDGER_MISMATCH",
            "node concurrency ledger would become negative",
            {"node": node_name, "replica_id": replica["replica_id"]},
        )
    spec = get_spec(state)
    capacity = spec.nodes[node_name].gpu_memory_mib
    if node["gpu_remaining_mib"][gpu_index] > capacity:
        raise BusinessError(
            "E_LEDGER_MISMATCH",
            "GPU ledger exceeds physical capacity",
            {
                "node": node_name,
                "gpu_index": gpu_index,
                "remaining_mib": node["gpu_remaining_mib"][gpu_index],
                "capacity_mib": capacity,
            },
        )
    replica["node"] = None
    replica["gpu_index"] = None


def refresh_service_counters(state: dict[str, Any]) -> None:
    spec = get_spec(state)
    replicas = state["replicas"]
    for name in sorted(spec.services):
        service_state = state["services"].setdefault(name, {})
        service_replicas = [
            replica for replica in replicas.values() if replica["service"] == name
        ]
        service_state["available_replicas"] = sum(
            1 for replica in service_replicas if replica["phase"] == "READY"
        )
        service_state["cold_starting_replicas"] = sum(
            1 for replica in service_replicas if replica["phase"] == "COLD_STARTING"
        )
        service_state["pending_replicas"] = sum(
            1 for replica in service_replicas if replica["phase"] == "PENDING"
        )
        service_state["draining_replicas"] = sum(
            1 for replica in service_replicas if replica["phase"] == "DRAINING"
        )


def validate_state(state: dict[str, Any]) -> None:
    if state.get("schema_version") != SCHEMA_VERSION:
        raise BusinessError(
            "E_STORAGE_ERROR",
            "unsupported state schema version",
            {"schema_version": state.get("schema_version")},
        )
    spec = get_spec(state)
    nodes = state.get("nodes", {})
    replicas = state.get("replicas", {})
    expected_remaining = {
        name: [node.gpu_memory_mib for _ in range(node.gpu_count)]
        for name, node in spec.nodes.items()
    }
    expected_used = {name: 0 for name in spec.nodes}
    seen_service_idx: set[tuple[str, int]] = set()

    for node_name in sorted(nodes):
        if node_name not in spec.nodes:
            raise BusinessError(
                "E_LEDGER_MISMATCH",
                "ledger contains an undeclared node",
                {"node": node_name},
            )
        node_state = nodes[node_name]
        if len(node_state.get("gpu_remaining_mib", [])) != spec.nodes[node_name].gpu_count:
            raise BusinessError(
                "E_LEDGER_MISMATCH",
                "GPU ledger length does not match node GPU count",
                {"node": node_name},
            )
    for node_name in sorted(spec.nodes):
        if node_name not in nodes:
            raise BusinessError(
                "E_LEDGER_MISMATCH",
                "declared node is missing from the ledger",
                {"node": node_name},
            )

    for replica_id in sorted(replicas):
        replica = replicas[replica_id]
        service_name = replica.get("service")
        if service_name not in spec.services:
            raise BusinessError(
                "E_LEDGER_MISMATCH",
                "replica references an undeclared service",
                {"replica_id": replica_id, "service": service_name},
            )
        phase = replica.get("phase")
        if phase not in PHASES:
            raise BusinessError(
                "E_STORAGE_ERROR",
                "replica has an invalid phase",
                {"replica_id": replica_id, "phase": phase},
            )
        key = (service_name, int(replica.get("ordinal", 0)))
        if key in seen_service_idx:
            raise BusinessError(
                "E_STORAGE_ERROR",
                "replica ordinal is duplicated within a service",
                {"service": service_name, "ordinal": key[1]},
            )
        seen_service_idx.add(key)
        service = spec.services[service_name]
        if replica.get("memory_mib") != service.replica_memory_mib:
            raise BusinessError(
                "E_LEDGER_MISMATCH",
                "replica memory does not match the current service specification",
                {
                    "replica_id": replica_id,
                    "ledger_memory_mib": replica.get("memory_mib"),
                    "spec_memory_mib": service.replica_memory_mib,
                },
            )
        if phase not in OCCUPYING_PHASES:
            if replica.get("node") is not None or replica.get("gpu_index") is not None:
                raise BusinessError(
                    "E_LEDGER_MISMATCH",
                    "non-occupying replica still owns a placement",
                    {"replica_id": replica_id, "phase": phase},
                )
            continue
        node_name = replica.get("node")
        gpu_index = replica.get("gpu_index")
        if node_name not in spec.nodes:
            raise BusinessError(
                "E_LEDGER_MISMATCH",
                "occupying replica references an invalid node",
                {"replica_id": replica_id, "node": node_name},
            )
        if not isinstance(gpu_index, int) or not (
            0 <= gpu_index < spec.nodes[node_name].gpu_count
        ):
            raise BusinessError(
                "E_LEDGER_MISMATCH",
                "occupying replica references an invalid GPU",
                {"replica_id": replica_id, "gpu_index": gpu_index},
            )
        if spec.nodes[node_name].gpu_memory_mib - (
            expected_remaining[node_name][gpu_index]
        ) + replica["memory_mib"] > spec.nodes[node_name].gpu_memory_mib:
            raise BusinessError(
                "E_LEDGER_MISMATCH",
                "replica allocation exceeds GPU capacity",
                {"replica_id": replica_id, "node": node_name, "gpu_index": gpu_index},
            )
        expected_remaining[node_name][gpu_index] -= replica["memory_mib"]
        expected_used[node_name] += 1
        if expected_remaining[node_name][gpu_index] < 0:
            raise BusinessError(
                "E_LEDGER_MISMATCH",
                "GPU memory ledger is oversold",
                {
                    "node": node_name,
                    "gpu_index": gpu_index,
                    "replica_id": replica_id,
                },
            )

    for node_name in sorted(spec.nodes):
        actual_remaining = nodes[node_name]["gpu_remaining_mib"]
        actual_used = nodes[node_name]["used_instances"]
        if actual_remaining != expected_remaining[node_name]:
            raise BusinessError(
                "E_LEDGER_MISMATCH",
                "GPU memory ledger does not equal the sum of replica allocations",
                {
                    "node": node_name,
                    "actual_remaining_mib": actual_remaining,
                    "expected_remaining_mib": expected_remaining[node_name],
                },
            )
        if actual_used != expected_used[node_name]:
            raise BusinessError(
                "E_LEDGER_MISMATCH",
                "node concurrency ledger does not equal the replica count",
                {
                    "node": node_name,
                    "actual_used_instances": actual_used,
                    "expected_used_instances": expected_used[node_name],
                },
            )
        if actual_used > spec.nodes[node_name].max_instances:
            raise BusinessError(
                "E_LEDGER_MISMATCH",
                "node concurrency ledger exceeds its limit",
                {
                    "node": node_name,
                    "used_instances": actual_used,
                    "max_instances": spec.nodes[node_name].max_instances,
                },
            )
    refresh_service_counters(state)


def sorted_replica_list(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        state["replicas"][replica_id]
        for replica_id in sorted(state["replicas"], key=_replica_sort_key)
    ]


def _replica_sort_key(replica_id: str) -> tuple[str, int, str]:
    text = replica_id
    service, _, suffix = text.rpartition("-")
    try:
        ordinal = int(suffix)
    except ValueError:
        ordinal = 0
    return (service, ordinal, text)
