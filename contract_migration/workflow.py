"""End-to-end scenario orchestration and deterministic report assembly."""

from __future__ import annotations

import json
import os
import tempfile
from collections import defaultdict
from pathlib import Path

from .edges import build_edge_cases
from .errors import ERROR_CODES, ContractMigrationError
from .generator import (
    DEFAULT_BATCH_CAPACITY,
    DEFAULT_MIGRATION_THRESHOLD_BP,
    DEFAULT_SEED,
    DEFAULT_WINDOW_SIZE,
    generate_dataset,
    validate_parameters,
)
from .hotspot import ACCESS_SHARE_WEIGHT_BP, CAPACITY_WEIGHT_BP, detect_hotspots
from .model import RouteSegment, contract_id
from .planner import build_migration_plan
from .router import LinearReferenceRouter, RangeRouter, expand_route_table
from .simulation import MigrationEngine, build_migration_workload


def _bp(numerator: int, denominator: int) -> int:
    return (numerator * 10_000) // denominator if denominator else 0


def _json_bytes(value: dict, *, sort_keys: bool = False) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=sort_keys,
        )
        + "\n"
    ).encode("utf-8")


def write_json_atomic(path: str | os.PathLike[str], value: dict) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = _json_bytes(value)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def write_snapshot_atomic(path: str | os.PathLike[str], snapshot: dict) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = _json_bytes(snapshot, sort_keys=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _parameters(
    seed: int,
    window_size: int,
    batch_capacity: int,
    migration_threshold_bp: int,
) -> dict:
    return {
        "seed": seed,
        "window_size": window_size,
        "batch_capacity_bytes": batch_capacity,
        "migration_threshold_bp": migration_threshold_bp,
        "access_window_percent_threshold": 20,
        "score_formula": (
            "(65*access_share_bp + 35*min(capacity_watermark_bp,10000))/100"
        ),
    }


def _source_streams(dataset, workload: dict) -> dict:
    return {
        "topology": {
            "path": "generator://topology",
            "record_count": len(dataset.shards),
            "field_sources": {
                "shard_id": "topology[].shard_id",
                "start_key": "topology[].start_key",
                "end_key": "topology[].end_key",
                "capacity_bytes": "topology[].capacity_bytes",
                "declared_used_bytes": "topology[].declared_used_bytes",
            },
        },
        "contract_documents": {
            "path": "generator://contract_documents",
            "record_count": len(dataset.contracts),
            "field_sources": {
                "contract_id": "contract_documents[].contract_id",
                "customer_batch": "contract_documents[].customer_batch",
                "title": "contract_documents[].title",
                "document_bytes": "contract_documents[].document_bytes",
                "document_sha256": "contract_documents[].document_sha256",
                "initial_logical_version": (
                    "contract_documents[].initial_logical_version"
                ),
            },
        },
        "seal_usage": {
            "path": "generator://seal_usage",
            "record_count": len(dataset.seal_usages),
            "join_key": "contract_id",
            "field_sources": {
                "usage_id": "seal_usage[].usage_id",
                "contract_id": "seal_usage[].contract_id",
                "seal_id": "seal_usage[].seal_id",
                "purpose": "seal_usage[].purpose",
                "used_at": "seal_usage[].used_at",
                "seal_version": "seal_usage[].seal_version",
            },
        },
        "signing_events": {
            "path": "generator://signing_events",
            "record_count": len(dataset.signing_events),
            "join_key": "contract_id",
            "field_sources": {
                "event_id": "signing_events[].event_id",
                "contract_id": "signing_events[].contract_id",
                "sequence": "signing_events[].sequence",
                "event_type": "signing_events[].event_type",
                "actor_id": "signing_events[].actor_id",
                "occurred_at": "signing_events[].occurred_at",
                "logical_version": "signing_events[].logical_version",
            },
        },
        "access_requests": {
            "path": "generator://access_requests.observation",
            "record_count": len(dataset.observation_requests),
            "field_sources": {
                "request_id": "access_requests.observation[].request_id",
                "sequence": "access_requests.observation[].sequence",
                "phase": "access_requests.observation[].phase",
                "operation": "access_requests.observation[].operation",
                "contract_id": "access_requests.observation[].contract_id",
                "client_id": "access_requests.observation[].client_id",
                "payload": "access_requests.observation[].payload",
            },
        },
        "migration_access_requests": {
            "path": "generator://access_requests.migration",
            "record_count": workload["request_count"],
            "derivation": (
                "built from each planned batch contract list and the fixed "
                "deterministic request template; post-switch requests reuse the "
                "accepted hotspot contract ranges"
            ),
            "field_sources": {
                "request_id": "access_requests.migration[].request_id",
                "sequence": "access_requests.migration[].sequence",
                "phase": "access_requests.migration[].phase",
                "operation": "access_requests.migration[].operation",
                "contract_id": (
                    "migration_plan.flattened_batches[].contracts[] or hotspot range"
                ),
                "client_id": "access_requests.migration[].client_id",
                "payload": "access_requests.migration[].payload",
            },
        },
        "fixed_seed": {
            "path": "generator://seed",
            "value": dataset.seed,
        },
    }


def _unified_contracts(dataset, initial_route: dict[str, str]) -> list[dict]:
    seals_by_contract: dict[str, list[tuple[int, object]]] = defaultdict(list)
    events_by_contract: dict[str, list[tuple[int, object]]] = defaultdict(list)
    for index, seal in enumerate(dataset.seal_usages):
        seals_by_contract[seal.contract_id].append((index, seal))
    for index, event in enumerate(dataset.signing_events):
        events_by_contract[event.contract_id].append((index, event))

    unified: list[dict] = []
    for index, contract in enumerate(dataset.contracts):
        seal_rows = sorted(
            seals_by_contract[contract.contract_id], key=lambda item: item[1].usage_id
        )
        event_rows = sorted(
            events_by_contract[contract.contract_id],
            key=lambda item: (item[1].sequence, item[1].event_id),
        )
        seal_paths = ",".join(str(item[0]) for item in seal_rows)
        event_paths = ",".join(str(item[0]) for item in event_rows)
        unified.append(
            {
                "contract_id": contract.contract_id,
                "customer_batch": contract.customer_batch,
                "initial_route_shard": initial_route[contract.contract_id],
                "document": {
                    "title": contract.title,
                    "document_bytes": contract.document_bytes,
                    "document_sha256": contract.document_sha256,
                    "initial_logical_version": contract.initial_logical_version,
                },
                "seal_usages": [item[1].__dict__ for item in seal_rows],
                "signing_events": [item[1].__dict__ for item in event_rows],
                "field_sources": {
                    "contract_id": f"contract_documents[{index}].contract_id",
                    "customer_batch": f"contract_documents[{index}].customer_batch",
                    "document.title": f"contract_documents[{index}].title",
                    "document.document_bytes": (
                        f"contract_documents[{index}].document_bytes"
                    ),
                    "document.document_sha256": (
                        f"contract_documents[{index}].document_sha256"
                    ),
                    "document.initial_logical_version": (
                        f"contract_documents[{index}].initial_logical_version"
                    ),
                    "seal_usages": (
                        f"seal_usage[{seal_paths}] joined by contract_id"
                        if seal_paths
                        else "seal_usage[].contract_id (no matching rows)"
                    ),
                    "signing_events": (
                        f"signing_events[{event_paths}] joined by contract_id"
                        if event_paths
                        else "signing_events[].contract_id (no matching rows)"
                    ),
                    "initial_route_shard": (
                        f"topology[] where {contract.contract_id} in "
                        "[start_key,end_key]"
                    ),
                },
            }
        )
    return unified


def _topology(dataset) -> list[dict]:
    return [
        {
            "shard": shard.shard_id,
            "start_key": shard.start_key,
            "end_key": shard.end_key,
            "capacity_bytes": shard.capacity_bytes,
            "declared_used_bytes": shard.declared_used_bytes,
            "source_path": f"topology[{index}]",
        }
        for index, shard in enumerate(dataset.shards)
    ]


def _reconciliation(dataset) -> dict:
    contracts = dataset.contracts_by_id
    rows: list[dict] = []
    for shard in dataset.shards:
        start = int(shard.start_key.split("-")[-1])
        end = int(shard.end_key.split("-")[-1])
        computed = sum(
            contracts[contract_id(index)].document_bytes
            for index in range(start, end + 1)
        )
        rows.append(
            {
                "shard": shard.shard_id,
                "contract_range": {
                    "start": shard.start_key,
                    "end": shard.end_key,
                },
                "document_bytes_sum": computed,
                "declared_used_bytes": shard.declared_used_bytes,
                "delta_bytes": computed - shard.declared_used_bytes,
                "balanced": computed == shard.declared_used_bytes,
            }
        )
    total_documents = sum(contract.document_bytes for contract in dataset.contracts)
    total_declared = sum(shard.declared_used_bytes for shard in dataset.shards)
    return {
        "per_shard": rows,
        "total_document_bytes": total_documents,
        "total_declared_used_bytes": total_declared,
        "delta_bytes": total_documents - total_declared,
        "balanced": total_documents == total_declared
        and all(row["balanced"] for row in rows),
    }


def _route_table(router: RangeRouter) -> list[dict]:
    return [segment.as_dict() for segment in router.segments]


def _reference_validation(dataset, initial_router: RangeRouter) -> dict:
    reference = LinearReferenceRouter(initial_router.segments)
    samples = []
    mismatches = []
    for contract in dataset.contracts:
        binary = initial_router.route(contract.contract_id)
        linear = reference.route(contract.contract_id)
        match = binary == linear
        samples.append(
            {
                "contract_id": contract.contract_id,
                "range_router_shard": binary,
                "linear_reference_shard": linear,
                "match": match,
            }
        )
        if not match:
            mismatches.append(samples[-1])
    return {
        "range_router": "binary_search_inclusive_ranges",
        "reference_router": "independent_linear_scan",
        "sample_count": len(samples),
        "samples": samples,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "passed": not mismatches,
    }


def _execute_full(dataset, plan: dict, workload: dict) -> MigrationEngine:
    engine = MigrationEngine(dataset, plan)
    for index, batch in enumerate(plan["flattened_batches"]):
        engine.activate_batch(batch)
        for request in workload["batch_workloads"][index]:
            engine.route_request(request)
        engine.complete_batch(batch)
    for request in workload["post_switch_workload"]:
        engine.route_request(request)
    return engine


def _snapshot_summary(snapshot: dict, path: str | None) -> dict:
    active = snapshot["active_batch_id"]
    active_state = snapshot["batch_states"].get(active) if active else None
    return {
        "snapshot_path": path,
        "request_cursor": snapshot["request_cursor"],
        "next_batch_index": snapshot["next_batch_index"],
        "active_batch_id": active,
        "completed_batches": snapshot["completed_batches"],
        "active_batch_synced_contracts": (
            list(active_state["synced_contracts"]) if active_state else []
        ),
        "route_epoch": snapshot["route_epoch"],
        "route_segments_at_interrupt": snapshot["route_segments"],
        "plan_fingerprint": snapshot["plan_fingerprint"],
    }


def _ownership_map(engine: MigrationEngine) -> dict[str, dict]:
    return {
        row["contract_id"]: {
            "shard": row["shard"],
            "logical_version": row["logical_version"],
            "physical_copies": row["physical_copies"],
        }
        for row in engine.contract_ownership()
    }


def _equivalence(full: MigrationEngine, resumed: MigrationEngine) -> dict:
    full_ownership = _ownership_map(full)
    resumed_ownership = _ownership_map(resumed)
    mismatches: list[dict] = []
    for contract in full.dataset.contracts:
        key = contract.contract_id
        if full_ownership[key] != resumed_ownership[key]:
            mismatches.append(
                {
                    "contract_id": key,
                    "full": full_ownership[key],
                    "resumed": resumed_ownership[key],
                }
            )
    full_keys = {
        shard: {
            key: [record["state"], record["version"]]
            for key, record in sorted(keys.items())
        }
        for shard, keys in sorted(full.keys.items())
    }
    resumed_keys = {
        shard: {
            key: [record["state"], record["version"]]
            for key, record in sorted(keys.items())
        }
        for shard, keys in sorted(resumed.keys.items())
    }
    route_equal = _route_table(full.route) == _route_table(resumed.route)
    key_state_equal = full_keys == resumed_keys
    signature_equal = full.state_signature() == resumed.state_signature()
    return {
        "contracts_compared": len(full.dataset.contracts),
        "route_table_equal": route_equal,
        "key_state_and_versions_equal": key_state_equal,
        "state_signature_equal": signature_equal,
        "full_state_signature": full.state_signature(),
        "resumed_state_signature": resumed.state_signature(),
        "per_contract_mismatch_count": len(mismatches),
        "per_contract_mismatches": mismatches,
        "equivalent": route_equal
        and key_state_equal
        and signature_equal
        and not mismatches,
    }


def _execute_interrupted(
    dataset,
    plan: dict,
    workload: dict,
    full_engine: MigrationEngine,
    interrupt_batch_index: int,
    interrupt_after_contracts: int,
    interrupt_after_requests: int,
    snapshot_path: str | None,
) -> dict:
    batches = plan["flattened_batches"]
    if not batches:
        return {
            "interruption": {
                "requested_batch_index": interrupt_batch_index,
                "effective_batch_index": None,
                "skipped": True,
                "reason": "no_accepted_migration_batches",
            },
            "resume": {
                "completed_batches_loaded": [],
                "completed_batches_replayed": 0,
                "contract_syncs_skipped_as_already_complete": 0,
                "route_table": _route_table(full_engine.route),
                "contract_ownership": full_engine.contract_ownership(),
                "load_distribution": full_engine.load_distribution(),
                "source_reclaim_rows": full_engine.reclaim_rows(),
            },
            "equivalence": _equivalence(full_engine, full_engine),
        }
    effective_batch_index = min(
        max(0, interrupt_batch_index), len(batches) - 1
    )
    engine = MigrationEngine(dataset, plan)
    for index in range(effective_batch_index):
        batch = batches[index]
        engine.activate_batch(batch)
        for request in workload["batch_workloads"][index]:
            engine.route_request(request)
        engine.complete_batch(batch)

    crash_batch = batches[effective_batch_index]
    engine.activate_batch(crash_batch)
    synced_before_crash = min(
        max(0, interrupt_after_contracts), len(crash_batch["contracts"])
    )
    for key in crash_batch["contracts"][:synced_before_crash]:
        engine.sync_contract(crash_batch, key)
    crash_batch_workload = workload["batch_workloads"][effective_batch_index]
    processed_before_crash = min(
        max(0, interrupt_after_requests), len(crash_batch_workload)
    )
    for request in crash_batch_workload[:processed_before_crash]:
        engine.route_request(request)
    cursor = (
        workload["batch_offsets"][effective_batch_index] + processed_before_crash
    )
    snapshot = engine.snapshot(
        request_cursor=cursor,
        next_batch_index=effective_batch_index,
        active_batch_id=crash_batch["batch_id"],
        parameters={
            "seed": dataset.seed,
            "window_size": workload["window_size"],
            "batch_capacity_bytes": plan["batch_capacity_bytes"],
            "migration_threshold_bp": workload["migration_threshold_bp"],
        },
    )
    persisted = _json_bytes(snapshot, sort_keys=True)
    if snapshot_path is not None:
        write_snapshot_atomic(snapshot_path, snapshot)
    snapshot_roundtrip = json.loads(persisted.decode("utf-8"))
    restored = MigrationEngine.restore(dataset, plan, snapshot_roundtrip)
    completed_loaded = list(restored.completed_batches)
    active_synced_loaded = list(
        restored.batch_states[crash_batch["batch_id"]]["synced_contracts"]
    )
    resume_cursor = snapshot_roundtrip["request_cursor"]
    current_index = snapshot_roundtrip["next_batch_index"]
    local_cursor = (
        resume_cursor - workload["batch_offsets"][current_index]
    )
    for request in workload["batch_workloads"][current_index][local_cursor:]:
        restored.route_request(request)
    restored.complete_batch(batches[current_index])

    for index in range(current_index + 1, len(batches)):
        batch = batches[index]
        restored.activate_batch(batch)
        for request in workload["batch_workloads"][index]:
            restored.route_request(request)
        restored.complete_batch(batch)
    for request in workload["post_switch_workload"]:
        restored.route_request(request)

    equivalence = _equivalence(full_engine, restored)
    return {
        "interruption": {
            "requested_batch_index": interrupt_batch_index,
            "interrupt_batch_index": effective_batch_index,
            "interrupt_batch_id": crash_batch["batch_id"],
            "interrupt_after_contracts_synced": synced_before_crash,
            "active_batch_contract_count": len(crash_batch["contracts"]),
            "active_batch_contracts_not_yet_synced": (
                len(crash_batch["contracts"]) - synced_before_crash
            ),
            "interrupt_after_requests_processed": processed_before_crash,
            "snapshot": _snapshot_summary(snapshot, snapshot_path),
        },
        "resume": {
            "completed_batches_loaded": completed_loaded,
            "completed_batches_replayed": 0,
            "active_batch_id": crash_batch["batch_id"],
            "contracts_already_synced_loaded": active_synced_loaded,
            "contract_syncs_skipped_as_already_complete": (
                restored.skipped_completed_contract_syncs
            ),
            "resumed_request_cursor": resume_cursor,
            "resumed_batches": [
                batch["batch_id"] for batch in batches[current_index:]
            ],
            "route_table": _route_table(restored.route),
            "contract_ownership": restored.contract_ownership(),
            "load_distribution": restored.load_distribution(),
            "source_reclaim_rows": restored.reclaim_rows(),
        },
        "equivalence": equivalence,
    }


def _post_migration_hotspot(
    dataset,
    engine: MigrationEngine,
    window_size: int,
    migration_threshold_bp: int,
    pre_hot_shards: list[str],
) -> dict:
    effective_window = min(window_size, len(dataset.observation_requests))
    window = dataset.observation_requests[-effective_window:]
    loads = engine.loads()
    counts = defaultdict(int)
    for request in window:
        counts[engine.route.route(request.contract_id)] += 1
    access_threshold = max(1, (effective_window * 20 + 99) // 100)
    rows = []
    post_hot = []
    for shard in dataset.shards:
        count = counts[shard.shard_id]
        access_share_bp = _bp(count, effective_window)
        watermark_bp = loads[shard.shard_id]["owned_watermark_bp"]
        score_bp = (
            ACCESS_SHARE_WEIGHT_BP * access_share_bp
            + CAPACITY_WEIGHT_BP * min(watermark_bp, 10_000)
        ) // 100
        is_hot = (
            count >= access_threshold
            and watermark_bp >= migration_threshold_bp
        )
        if is_hot:
            post_hot.append(shard.shard_id)
        rows.append(
            {
                "shard": shard.shard_id,
                "remapped_window_request_count": count,
                "access_share_bp": access_share_bp,
                "owned_watermark_bp": watermark_bp,
                "hot_score_bp": score_bp,
                "is_hot": is_hot,
                "evidence": [
                    f"access_count={count}, threshold={access_threshold}",
                    (
                        f"owned_watermark_bp={watermark_bp}, "
                        f"threshold_bp={migration_threshold_bp}"
                    ),
                ],
            }
        )
    return {
        "evaluation": "same observation window remapped through final route table",
        "per_shard": rows,
        "pre_hot_shards": pre_hot_shards,
        "post_hot_shards": post_hot,
        "hotspot_eliminated": not post_hot,
        "conclusion": (
            "hotspot_eliminated"
            if not post_hot
            else "hotspot_remains_after_migration"
        ),
    }


def _routing_section(engine: MigrationEngine, workload: dict) -> dict:
    migrating_writes = [
        record
        for record in engine.records
        if record["operation"] == "write"
        and "source_commit" in record["write_path"]
    ]
    post_switch_violations = [
        record
        for record in engine.records
        if record["misrouted_pending_reclaim"]
    ]
    return {
        "request_count": len(engine.records),
        "migration_request_count": len(workload["flattened"]),
        "records": engine.records,
        "write_version_checks": engine.write_checks,
        "migrating_write_count": len(migrating_writes),
        "all_migrating_writes_have_equal_versions": all(
            record["versions_match_after_write"] for record in migrating_writes
        ),
        "reference_router_mismatch_count": len(engine.reference_mismatches),
        "reference_router_mismatches": engine.reference_mismatches,
        "pending_reclaim_service_violation_count": len(post_switch_violations),
        "pending_reclaim_service_violations": post_switch_violations,
        "read_during_migration_count": sum(
            1
            for record in engine.records
            if record["operation"] == "read"
            and record["routed_key_state"] == "migrating"
        ),
        "post_switch_target_read_count": sum(
            1
            for record in engine.records
            if record["phase"] == "post_switch" and record["operation"] == "read"
        ),
    }


def _route_diff(
    dataset, initial_router: RangeRouter, final_engine: MigrationEngine
) -> dict:
    changed = []
    for contract in dataset.contracts:
        before = initial_router.route(contract.contract_id)
        after = final_engine.route.route(contract.contract_id)
        if before != after:
            changed.append(
                {
                    "contract_id": contract.contract_id,
                    "from_shard": before,
                    "to_shard": after,
                }
            )
    return {
        "initial_route_table": _route_table(initial_router),
        "final_route_table": _route_table(final_engine.route),
        "changed_contract_count": len(changed),
        "changed_contracts": changed,
        "atomic_replacement_count": final_engine.route.replacement_count,
    }


def _contract_lifecycle(
    dataset, initial_router: RangeRouter, engine: MigrationEngine
) -> list[dict]:
    rows: list[dict] = []
    for contract in dataset.contracts:
        key = contract.contract_id
        initial = initial_router.route(key)
        final = engine.route.route(key)
        copies = []
        for shard in dataset.shards:
            record = engine.keys[shard.shard_id].get(key)
            if record is not None:
                copies.append(
                    {
                        "shard": shard.shard_id,
                        "state": record["state"],
                        "logical_version": record["version"],
                    }
                )
        rows.append(
            {
                "contract_id": key,
                "initial_shard": initial,
                "final_shard": final,
                "moved": initial != final,
                "final_logical_version": engine.keys[final][key]["version"],
                "source_key_state_after_migration": (
                    engine.keys[initial][key]["state"]
                    if key in engine.keys[initial]
                    else None
                ),
                "physical_copies": copies,
            }
        )
    return rows


def _consistency_checks(
    dataset,
    reconciliation: dict,
    hotspot: dict,
    plan: dict,
    engine: MigrationEngine,
    resume_result: dict,
    edge_cases: dict,
    reference_validation: dict,
) -> dict:
    checks = {
        "capacity_reconciliation_balanced": reconciliation["balanced"],
        "hotspot_truth_range_matches": hotspot["truth_check"]["range_matches"],
        "hotspot_truth_shards_match": hotspot["truth_check"]["shards_match"],
        "hotspot_truth_customer_batches_match": hotspot["truth_check"][
            "customer_batches_match"
        ],
        "hotspot_truth_slices_match": hotspot["truth_check"]["slices_match"],
        "migration_plan_has_no_rejections": (
            plan["summary"]["rejected_range_count"] == 0
        ),
        "all_plan_batches_within_capacity": all(
            batch["within_batch_capacity"]
            for batch in plan["flattened_batches"]
        ),
        "all_request_routes_match_reference": (
            not engine.reference_mismatches
            and reference_validation["passed"]
        ),
        "all_migrating_write_versions_match": all(
            check["versions_match"] for check in engine.write_checks
        ),
        "no_pending_reclaim_key_served": not any(
            record["misrouted_pending_reclaim"] for record in engine.records
        ),
        "source_keys_pending_reclaim_after_switch": all(
            row["source_state"] == "pending_reclaim"
            and row["target_state"] == "active"
            and row["route_shard"] == row["target_shard"]
            for row in engine.reclaim_rows()
        ),
        "resume_equivalent_to_full_migration": resume_result["equivalence"][
            "equivalent"
        ],
        "edge_cases_all_passed": edge_cases["all_passed"],
        "logical_ownership_within_capacity": all(
            row["owned_bytes"] <= row["capacity_bytes"]
            for row in engine.load_distribution()
        ),
    }
    return {
        "checks": checks,
        "all_passed": all(checks.values()),
        "failed_checks": [name for name, passed in checks.items() if not passed],
    }


def _execution_summary(
    dataset,
    hotspot: dict,
    plan: dict,
    engine: MigrationEngine,
    resume_result: dict,
    edge_cases: dict,
    consistency: dict,
) -> dict:
    return {
        "contracts_loaded": len(dataset.contracts),
        "seal_usages_loaded": len(dataset.seal_usages),
        "signing_events_loaded": len(dataset.signing_events),
        "observation_requests_loaded": len(dataset.observation_requests),
        "migration_requests_executed": len(engine.records),
        "initial_hot_shards": hotspot["hot_shard_ids"],
        "hot_range": hotspot["hot_range"],
        "hot_range_slices": len(hotspot["hot_range_slices"]),
        "planned_batches": len(plan["flattened_batches"]),
        "completed_batches": len(engine.completed_batches),
        "route_switches": len(engine.switches),
        "contracts_moved": len(engine.reclaim_rows()),
        "resume_equivalent": resume_result["equivalence"]["equivalent"],
        "edge_cases_passed": edge_cases["all_passed"],
        "all_consistency_checks_passed": consistency["all_passed"],
    }


def run_scenario(
    seed: int = DEFAULT_SEED,
    window_size: int = DEFAULT_WINDOW_SIZE,
    batch_capacity: int = DEFAULT_BATCH_CAPACITY,
    migration_threshold_bp: int = DEFAULT_MIGRATION_THRESHOLD_BP,
    interrupt_batch_index: int = 1,
    interrupt_after_contracts: int = 3,
    interrupt_after_requests: int = 20,
    snapshot_path: str | None = None,
) -> dict:
    validate_parameters(
        seed, window_size, batch_capacity, migration_threshold_bp
    )
    dataset = generate_dataset(
        seed, window_size, batch_capacity, migration_threshold_bp
    )
    initial_router = RangeRouter.from_shards(dataset.shards)
    initial_route = expand_route_table(
        initial_router.segments,
        [contract.contract_id for contract in dataset.contracts],
    )
    reconciliation = _reconciliation(dataset)
    unified = _unified_contracts(dataset, initial_route)
    reference_validation = _reference_validation(dataset, initial_router)
    hotspot = detect_hotspots(
        dataset, window_size, migration_threshold_bp
    )
    plan = build_migration_plan(dataset, hotspot, batch_capacity)
    workload = build_migration_workload(dataset, plan)
    workload["window_size"] = window_size
    workload["migration_threshold_bp"] = migration_threshold_bp
    full_engine = _execute_full(dataset, plan, workload)
    resume_result = _execute_interrupted(
        dataset,
        plan,
        workload,
        full_engine,
        interrupt_batch_index,
        interrupt_after_contracts,
        interrupt_after_requests,
        snapshot_path,
    )
    edge_cases = build_edge_cases()
    routing = _routing_section(full_engine, workload)
    route_diff = _route_diff(dataset, initial_router, full_engine)
    lifecycle = _contract_lifecycle(dataset, initial_router, full_engine)
    final_loads = full_engine.load_distribution()
    reclaim_rows = full_engine.reclaim_rows()
    hotspot_elimination = _post_migration_hotspot(
        dataset,
        full_engine,
        window_size,
        migration_threshold_bp,
        hotspot["hot_shard_ids"],
    )
    consistency = _consistency_checks(
        dataset,
        reconciliation,
        hotspot,
        plan,
        full_engine,
        resume_result,
        edge_cases,
        reference_validation,
    )
    summary = _execution_summary(
        dataset,
        hotspot,
        plan,
        full_engine,
        resume_result,
        edge_cases,
        consistency,
    )
    return {
        "schema_version": "1.0",
        "report_kind": "deterministic_contract_migration",
        "run_parameters": _parameters(
            seed, window_size, batch_capacity, migration_threshold_bp
        ),
        "execution_summary": summary,
        "field_sources": _source_streams(dataset, workload),
        "topology": _topology(dataset),
        "capacity_reconciliation": reconciliation,
        "unified_contracts": unified,
        "initial_route_table": _route_table(initial_router),
        "reference_router_validation": reference_validation,
        "hotspot_analysis": hotspot,
        "migration_plan": plan,
        "access_routing": routing,
        "batch_switch_history": full_engine.switches,
        "route_diff": route_diff,
        "contract_shard_lifecycle": lifecycle,
        "load_distribution": {
            "shards": final_loads,
            "logical_ownership_capacity_violations": [
                row
                for row in final_loads
                if row["owned_bytes"] > row["capacity_bytes"]
            ],
        },
        "source_reclaim_state": {
            "rows": reclaim_rows,
            "row_count": len(reclaim_rows),
            "all_source_keys_pending_reclaim": all(
                row["source_state"] == "pending_reclaim" for row in reclaim_rows
            ),
            "all_routes_point_to_target": all(
                row["route_shard"] == row["target_shard"] for row in reclaim_rows
            ),
        },
        "hotspot_elimination": hotspot_elimination,
        "interruption_resume": resume_result,
        "edge_cases": edge_cases,
        "consistency_checks": consistency,
        "error_codes": dict(ERROR_CODES),
    }


def load_snapshot(path: str | os.PathLike[str]) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractMigrationError(
            "SNAPSHOT_CORRUPT", f"cannot load snapshot: {exc}"
        ) from exc


def resume_from_snapshot(path: str | os.PathLike[str]) -> dict:
    snapshot = load_snapshot(path)
    try:
        parameters = snapshot["parameters"]
        seed = parameters["seed"]
        window_size = parameters["window_size"]
        batch_capacity = parameters["batch_capacity_bytes"]
        threshold = parameters["migration_threshold_bp"]
    except (KeyError, TypeError) as exc:
        raise ContractMigrationError(
            "SNAPSHOT_CORRUPT", f"snapshot parameters missing: {exc}"
        ) from exc
    dataset = generate_dataset(seed, window_size, batch_capacity, threshold)
    hotspot = detect_hotspots(dataset, window_size, threshold)
    plan = build_migration_plan(dataset, hotspot, batch_capacity)
    workload = build_migration_workload(dataset, plan)
    workload["window_size"] = window_size
    workload["migration_threshold_bp"] = threshold
    restored = MigrationEngine.restore(dataset, plan, snapshot)
    completed_batches_loaded = list(snapshot["completed_batches"])
    resumed_batch_ids: list[str] = []
    current_index = snapshot["next_batch_index"]
    if current_index < 0 or current_index > len(plan["flattened_batches"]):
        raise ContractMigrationError(
            "SNAPSHOT_CORRUPT", "snapshot next_batch_index is outside plan"
        )
    if snapshot["active_batch_id"] is not None:
        active_id = snapshot["active_batch_id"]
        active_batch = restored.batch_by_id(active_id)
        resumed_batch_ids.append(active_id)
        if active_batch not in plan["flattened_batches"]:
            raise ContractMigrationError(
                "RESUME_PLAN_MISMATCH", "active batch not present in regenerated plan"
            )
        local_cursor = (
            snapshot["request_cursor"] - workload["batch_offsets"][current_index]
        )
        for request in workload["batch_workloads"][current_index][local_cursor:]:
            restored.route_request(request)
        restored.complete_batch(active_batch)
        current_index += 1
    for index in range(current_index, len(plan["flattened_batches"])):
        batch = plan["flattened_batches"][index]
        resumed_batch_ids.append(batch["batch_id"])
        restored.activate_batch(batch)
        for request in workload["batch_workloads"][index]:
            restored.route_request(request)
        restored.complete_batch(batch)
    for request in workload["post_switch_workload"]:
        restored.route_request(request)
    full_engine = _execute_full(dataset, plan, workload)
    equivalence = _equivalence(full_engine, restored)
    return {
        "schema_version": "1.0",
        "report_kind": "resumed_contract_migration",
        "snapshot_path": str(path),
        "run_parameters": _parameters(
            seed, window_size, batch_capacity, threshold
        ),
        "snapshot": _snapshot_summary(snapshot, str(path)),
        "resume": {
            "completed_batches_loaded": completed_batches_loaded,
            "completed_batches_replayed": 0,
            "resumed_batches": resumed_batch_ids,
            "route_table": _route_table(restored.route),
            "contract_ownership": restored.contract_ownership(),
            "load_distribution": restored.load_distribution(),
            "source_reclaim_rows": restored.reclaim_rows(),
            "contract_syncs_skipped_as_already_complete": (
                restored.skipped_completed_contract_syncs
            ),
            "completed_batches": restored.completed_batches,
        },
        "equivalence_to_full_migration": equivalence,
        "consistency_checks": {
            "resume_equivalent_to_full_migration": equivalence["equivalent"],
            "all_passed": equivalence["equivalent"],
        },
    }
