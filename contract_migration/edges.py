"""Constructed boundary cases with directly observable conclusions."""

from __future__ import annotations

import json

from .errors import ERROR_CODES
from .model import (
    AccessRequest,
    ContractDocument,
    Dataset,
    RouteSegment,
    Shard,
    contract_id,
)
from .planner import _choose_target, build_migration_plan
from .router import RangeRouter, split_range_by_route
from .simulation import MigrationEngine


def _mini_dataset() -> Dataset:
    contracts = [
        ContractDocument(
            contract_id=contract_id(index),
            index=index,
            customer_batch="EDGE-BATCH",
            title=f"edge-contract-{index}",
            document_bytes=100,
            document_sha256=f"edge-sha-{index}",
            initial_logical_version=1,
        )
        for index in range(10)
    ]
    shards = [
        Shard("shard-a", contract_id(0), contract_id(4), 1_000, 500),
        Shard("shard-b", contract_id(5), contract_id(9), 1_000, 100),
    ]
    return Dataset(
        seed=1,
        shards=shards,
        contracts=contracts,
        seal_usages=[],
        signing_events=[],
        observation_requests=[],
    )


def _mini_batch(batch_id: str, ordinal: int, index: int, source: str, target: str) -> dict:
    key = contract_id(index)
    return {
        "batch_id": batch_id,
        "batch_ordinal": ordinal,
        "plan_index": 0,
        "start": key,
        "end": key,
        "contracts": [key],
        "contract_count": 1,
        "bytes": 100,
        "source_shard": source,
        "target_shard": target,
        "batch_capacity_bytes": 1_000,
        "within_batch_capacity": True,
        "note": "edge_constructed_batch",
    }


def _mini_plan(batches: list[dict]) -> dict:
    return {
        "batch_capacity_bytes": 1_000,
        "target_policy": "edge_constructed",
        "plans": [],
        "flattened_batches": batches,
        "summary": {},
    }


def _read_request(key_index: int, sequence: int, suffix: str) -> AccessRequest:
    return AccessRequest(
        request_id=f"EDGE-{suffix}",
        sequence=sequence,
        phase="edge",
        operation="read",
        contract_id=contract_id(key_index),
        client_id="EDGE-CLIENT",
        payload=f"edge-read-{suffix}",
    )


def _cross_boundary_case() -> dict:
    segments = [
        RouteSegment(contract_id(0), contract_id(9), "shard-a"),
        RouteSegment(contract_id(10), contract_id(19), "shard-b"),
    ]
    slices = split_range_by_route(contract_id(6), contract_id(14), segments)
    for item in slices:
        item["split_from_cross_boundary"] = True
    return {
        "case": "hot_range_exactly_crosses_route_boundary",
        "route_segments": [segment.as_dict() for segment in segments],
        "hot_range": {"start": contract_id(6), "end": contract_id(14)},
        "boundary": f"{contract_id(9)}|{contract_id(10)}",
        "resulting_slices": slices,
        "observable_conclusion": (
            "hot range is split into two ownership-marked slices; no contract "
            "is assigned to both slices"
        ),
        "passed": (
            len(slices) == 2
            and slices[0]["source_shard"] == "shard-a"
            and slices[1]["source_shard"] == "shard-b"
            and slices[0]["end"] == contract_id(9)
            and slices[1]["start"] == contract_id(10)
        ),
    }


def _concurrent_read_write_case() -> dict:
    dataset = _mini_dataset()
    batch = _mini_batch("batch-01", 0, 3, "shard-a", "shard-b")
    engine = MigrationEngine(dataset, _mini_plan([batch]))
    key = contract_id(3)
    engine.activate_batch(batch)
    before_source = engine.keys["shard-a"][key]["version"]
    before_target = engine.keys["shard-b"][key]["version"]
    first_read = engine.route_request(_read_request(3, 0, "READ-BEFORE"))
    engine.keys["shard-a"][key]["version"] += 1
    engine.keys["shard-a"][key]["last_write"] = "EDGE-SOURCE-WRITE"
    source_after_write = engine.keys["shard-a"][key]["version"]
    target_before_sync = engine.keys["shard-b"][key]["version"]
    second_read = engine.route_request(
        _read_request(3, 1, "READ-SOURCE-DURING")
    )
    engine.sync_contract(batch, key)
    target_after_sync = engine.keys["shard-b"][key]["version"]
    switch = engine.complete_batch(batch)
    post_switch_read = engine.route_request(
        _read_request(3, 2, "READ-AFTER-SWITCH")
    )
    return {
        "case": "concurrent_read_write_same_contract_during_migration",
        "contract_id": key,
        "versions": {
            "before_migration_source": before_source,
            "before_migration_target": before_target,
            "read_before_version": first_read["source_visible_version"],
            "source_after_committed_write": source_after_write,
            "target_before_sync_visible_version": target_before_sync,
            "read_during_migration_version": second_read["source_visible_version"],
            "target_after_sync": target_after_sync,
            "read_after_switch_version": post_switch_read["source_visible_version"],
        },
        "intermediate_target_lag_observed": target_before_sync == before_source,
        "source_and_target_equal_after_sync": source_after_write == target_after_sync,
        "route_after_switch": switch["route_change"]["after"],
        "post_switch_served_by": post_switch_read["routed_shard"],
        "observable_conclusion": (
            "writes are committed on the source first; a concurrent target read "
            "can see the old version only until replication completes, then both "
            "sides expose the same logical version and reads follow the new route"
        ),
        "passed": (
            target_before_sync == before_source
            and source_after_write == target_after_sync
            and post_switch_read["routed_shard"] == "shard-b"
            and post_switch_read["source_visible_version"] == source_after_write
        ),
    }


def _disconnect_after_switch_case() -> dict:
    dataset = _mini_dataset()
    batches = [
        _mini_batch("batch-01", 0, 3, "shard-a", "shard-b"),
        _mini_batch("batch-02", 1, 4, "shard-a", "shard-b"),
    ]
    plan = _mini_plan(batches)
    engine = MigrationEngine(dataset, plan)
    engine.activate_batch(batches[0])
    engine.sync_remaining(batches[0])
    engine.complete_batch(batches[0])
    snapshot = engine.snapshot(
        request_cursor=0,
        next_batch_index=1,
        active_batch_id=None,
        parameters={"edge": "disconnect_after_switch"},
    )
    persisted = json.loads(json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
    restored = MigrationEngine.restore(dataset, plan, persisted)
    completed_seen = list(restored.completed_batches)
    restored.activate_batch(batches[1])
    restored.sync_remaining(batches[1])
    restored.complete_batch(batches[1])
    return {
        "case": "disconnect_immediately_after_batch_switch",
        "snapshot_completed_batches": completed_seen,
        "replayed_completed_batches": 0,
        "resumed_pending_batch": batches[1]["batch_id"],
        "route_after_resume": [
            segment.as_dict() for segment in restored.route.segments
        ],
        "contract_routes": {
            contract_id(3): restored.route.route(contract_id(3)),
            contract_id(4): restored.route.route(contract_id(4)),
        },
        "source_key_states": {
            contract_id(3): restored.keys["shard-a"][contract_id(3)]["state"],
            contract_id(4): restored.keys["shard-a"][contract_id(4)]["state"],
        },
        "observable_conclusion": (
            "the completed batch is loaded as a checkpoint and is not migrated "
            "again; only the pending batch is resumed"
        ),
        "passed": (
            completed_seen == ["batch-01"]
            and restored.route.route(contract_id(3)) == "shard-b"
            and restored.route.route(contract_id(4)) == "shard-b"
            and restored.keys["shard-a"][contract_id(3)]["state"]
            == "pending_reclaim"
        ),
    }


def _exact_capacity_case() -> dict:
    contracts = [
        ContractDocument(
            contract_id=contract_id(index),
            index=index,
            customer_batch="EXACT-CAPACITY",
            title=f"exact-capacity-{index}",
            document_bytes=100,
            document_sha256=f"exact-sha-{index}",
            initial_logical_version=1,
        )
        for index in range(10)
    ]
    dataset = Dataset(
        seed=2,
        shards=[
            Shard("shard-a", contract_id(0), contract_id(3), 500, 400),
            Shard("shard-b", contract_id(4), contract_id(9), 1_000, 600),
        ],
        contracts=contracts,
        seal_usages=[],
        signing_events=[],
        observation_requests=[],
    )
    usage = {"shard-a": 400, "shard-b": 600}
    target, max_free, candidates = _choose_target(
        dataset, "shard-a", required_bytes=400, current_usage=usage
    )
    projected = usage[target] + 400 if target is not None else None
    keys = [contract_id(index) for index in range(4)]
    batch = {
        "batch_id": "batch-01",
        "batch_ordinal": 0,
        "plan_index": 0,
        "start": keys[0],
        "end": keys[-1],
        "contracts": keys,
        "contract_count": 4,
        "bytes": 400,
        "source_shard": "shard-a",
        "target_shard": "shard-b",
        "batch_capacity_bytes": 1_000,
        "within_batch_capacity": True,
        "note": "exact_capacity_boundary_batch",
    }
    engine = MigrationEngine(dataset, _mini_plan([batch]))
    engine.activate_batch(batch)
    engine.sync_remaining(batch)
    switch = engine.complete_batch(batch)
    final_target = next(
        row
        for row in engine.load_distribution()
        if row["shard"] == "shard-b"
    )
    return {
        "case": "target_reaches_capacity_exactly",
        "required_bytes": 400,
        "initial_target_used_bytes": usage["shard-b"],
        "target_capacity_bytes": 1_000,
        "selected_target": target,
        "projected_used_bytes": projected,
        "free_bytes_after": (
            1_000 - projected if projected is not None else None
        ),
        "at_capacity_but_not_over": projected == 1_000,
        "candidates": candidates,
        "executed_route": [
            segment.as_dict() for segment in engine.route.segments
        ],
        "executed_contract_routes": {
            key: engine.route.route(key) for key in keys
        },
        "target_owned_bytes_after": final_target["owned_bytes"],
        "target_owned_watermark_bp_after": final_target[
            "owned_watermark_bp"
        ],
        "source_key_states_after": {
            key: engine.keys["shard-a"][key]["state"] for key in keys
        },
        "target_key_states_after": {
            key: engine.keys["shard-b"][key]["state"] for key in keys
        },
        "route_switch_epoch": switch["route_epoch"],
        "observable_conclusion": (
            "a target whose remaining free bytes equal the required bytes is "
            "accepted; after the atomic switch it owns exactly 1000/1000 bytes "
            "while every source copy is pending_reclaim"
        ),
        "passed": (
            target == "shard-b"
            and projected == 1_000
            and final_target["owned_bytes"] == 1_000
            and final_target["owned_watermark_bp"] == 10_000
            and all(engine.route.route(key) == "shard-b" for key in keys)
            and all(
                engine.keys["shard-a"][key]["state"] == "pending_reclaim"
                for key in keys
            )
            and all(
                engine.keys["shard-b"][key]["state"] == "active"
                for key in keys
            )
        ),
    }


def _capacity_rejection_case() -> dict:
    contracts = [
        ContractDocument(
            contract_id=contract_id(index),
            index=index,
            customer_batch="REJECT-CAPACITY",
            title=f"reject-capacity-{index}",
            document_bytes=100,
            document_sha256=f"reject-sha-{index}",
            initial_logical_version=1,
        )
        for index in range(10)
    ]
    dataset = Dataset(
        seed=3,
        shards=[
            Shard("shard-a", contract_id(0), contract_id(3), 500, 400),
            Shard("shard-b", contract_id(4), contract_id(9), 800, 600),
        ],
        contracts=contracts,
        seal_usages=[],
        signing_events=[],
        observation_requests=[],
    )
    hotspot = {
        "hot_range_slices": [
            {
                "start": contract_id(0),
                "end": contract_id(3),
                "source_shard": "shard-a",
                "split_from_cross_boundary": False,
            }
        ]
    }
    plan = build_migration_plan(dataset, hotspot, batch_capacity=1_000)
    plan_row = plan["plans"][0]
    return {
        "case": "target_capacity_insufficient",
        "required_bytes": 400,
        "largest_free_bytes": plan_row["rejection"][
            "largest_single_target_free_bytes"
        ],
        "gap_bytes": plan_row["rejection"]["gap_bytes"],
        "selected_target": plan_row["target_shard"],
        "conclusion": plan_row["rejection"]["conclusion"],
        "error_code": plan_row["rejection"]["error_code"],
        "batches_created": len(plan["flattened_batches"]),
        "candidates": plan_row["target_selection"]["candidates"],
        "observable_conclusion": (
            "the planner emits no batch for the rejected slice and reports "
            "the exact capacity gap"
        ),
        "passed": (
            plan_row["status"] == "rejected"
            and plan_row["target_shard"] is None
            and plan_row["rejection"]["largest_single_target_free_bytes"] == 200
            and plan_row["rejection"]["gap_bytes"] == 200
            and not plan["flattened_batches"]
            and plan_row["rejection"]["error_code"]
            == ERROR_CODES["TARGET_CAPACITY_INSUFFICIENT"]
        ),
    }


def build_edge_cases() -> dict:
    cases = [
        _cross_boundary_case(),
        _concurrent_read_write_case(),
        _disconnect_after_switch_case(),
        _exact_capacity_case(),
        _capacity_rejection_case(),
    ]
    return {
        "cases": cases,
        "all_passed": all(case["passed"] for case in cases),
        "error_codes": dict(ERROR_CODES),
        "error_injection": {
            "cli_option": "--inject-error INTEGER_CODE",
            "example": "python3 -m contract_migration --inject-error 1005",
            "output_contract": {
                "ok": False,
                "error_code": "stable integer",
                "symbol": "stable symbolic name",
                "message": "human-readable detail",
            },
        },
    }
