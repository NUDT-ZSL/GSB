"""Migration state machine, request routing, and durable resume support."""

from __future__ import annotations

import hashlib
import json
from collections import Counter

from .errors import ContractMigrationError
from .model import AccessRequest, Dataset, RouteSegment, contract_id, contract_index
from .router import LinearReferenceRouter, RangeRouter


def _bp(numerator: int, denominator: int) -> int:
    return (numerator * 10_000) // denominator if denominator else 0


def _write_workload_for_batch(
    batch: dict, start_sequence: int, length: int = 80
) -> list[AccessRequest]:
    contracts = batch["contracts"]
    requests: list[AccessRequest] = []
    operations = (
        ("read", contracts[0]),
        ("write", contracts[0]),
        ("read", contracts[0]),
    )
    for operation, key in operations:
        sequence = start_sequence + len(requests)
        requests.append(
            AccessRequest(
                request_id=f"R{sequence:06d}",
                sequence=sequence,
                phase="migration",
                operation=operation,
                contract_id=key,
                client_id=f"MIG-CLIENT-{batch['batch_ordinal'] % 5:02d}",
                payload=f"migration-write-{sequence}",
            )
        )
    cursor = 0
    while len(requests) < length:
        key = contracts[cursor % len(contracts)]
        operation = (
            "write"
            if (cursor + batch["batch_ordinal"]) % 3 == 0
            else "read"
        )
        sequence = start_sequence + len(requests)
        requests.append(
            AccessRequest(
                request_id=f"R{sequence:06d}",
                sequence=sequence,
                phase="migration",
                operation=operation,
                contract_id=key,
                client_id=f"MIG-CLIENT-{(cursor + batch['batch_ordinal']) % 5:02d}",
                payload=f"migration-write-{sequence}",
            )
        )
        cursor += 1
    return requests


def _post_switch_workload(
    hot_contract_ids: list[str], start_sequence: int, length: int = 100
) -> list[AccessRequest]:
    requests: list[AccessRequest] = []
    for offset in range(length):
        sequence = start_sequence + offset
        requests.append(
            AccessRequest(
                request_id=f"R{sequence:06d}",
                sequence=sequence,
                phase="post_switch",
                operation="write" if offset % 4 == 0 else "read",
                contract_id=hot_contract_ids[offset % len(hot_contract_ids)],
                client_id=f"POST-CLIENT-{offset % 4:02d}",
                payload=f"post-switch-write-{sequence}",
            )
        )
    return requests


def build_migration_workload(
    dataset: Dataset, plan: dict
) -> dict:
    sequence = len(dataset.observation_requests)
    batch_workloads: list[list[AccessRequest]] = []
    for batch in plan["flattened_batches"]:
        workload = _write_workload_for_batch(batch, sequence)
        batch_workloads.append(workload)
        sequence += len(workload)
    hot_ids: list[str] = []
    for item in plan["plans"]:
        if item["status"] == "accepted":
            hot_ids.extend(item["contract_ids"])
    if not hot_ids:
        hot_ids = [contract.contract_id for contract in dataset.contracts[:1]]
    post_workload = _post_switch_workload(hot_ids, sequence)
    sequence += len(post_workload)
    flattened = [request for workload in batch_workloads for request in workload]
    flattened.extend(post_workload)
    return {
        "batch_workloads": batch_workloads,
        "post_switch_workload": post_workload,
        "flattened": flattened,
        "batch_offsets": [
            sum(len(workload) for workload in batch_workloads[:index])
            for index in range(len(batch_workloads))
        ],
        "post_switch_offset": len(flattened) - len(post_workload),
        "request_count": len(flattened),
    }


class MigrationEngine:
    """Single-threaded model with synchronous source-to-target write replication."""

    def __init__(self, dataset: Dataset, plan: dict):
        self.dataset = dataset
        self.plan = plan
        self.batches = plan["flattened_batches"]
        self.contracts_by_id = dataset.contracts_by_id
        self.route = RangeRouter.from_shards(dataset.shards)
        self.keys: dict[str, dict[str, dict]] = {
            shard.shard_id: {} for shard in dataset.shards
        }
        initial = {
            shard.shard_id: (shard.start_key, shard.end_key)
            for shard in dataset.shards
        }
        for shard_id, (start, end) in initial.items():
            for index in range(contract_index(start), contract_index(end) + 1):
                key = contract_id(index)
                self.keys[shard_id][key] = {
                    "state": "active",
                    "version": self.contracts_by_id[key].initial_logical_version,
                    "last_write": None,
                }
        self.batch_states = {
            batch["batch_id"]: {
                "status": "pending",
                "synced_contracts": [],
            }
            for batch in self.batches
        }
        self.completed_batches: list[str] = []
        self.switches: list[dict] = []
        self.records: list[dict] = []
        self.route_epoch = 0
        self.reference_mismatches: list[dict] = []
        self.write_checks: list[dict] = []
        self.skipped_completed_contract_syncs = 0
        self.skipped_completed_batches = 0
        self.plan_fingerprint = _plan_fingerprint(plan)

    def batch_by_id(self, batch_id: str) -> dict:
        for batch in self.batches:
            if batch["batch_id"] == batch_id:
                return batch
        raise ContractMigrationError(
            "SNAPSHOT_CORRUPT", f"unknown batch id in state: {batch_id}"
        )

    def activate_batch(self, batch: dict) -> None:
        state = self.batch_states[batch["batch_id"]]
        if state["status"] != "pending":
            raise ContractMigrationError(
                "SNAPSHOT_CORRUPT",
                f"batch {batch['batch_id']} cannot activate from {state['status']}",
            )
        source = batch["source_shard"]
        target = batch["target_shard"]
        target_shard = next(
            shard
            for shard in self.dataset.shards
            if shard.shard_id == target
        )
        projected_target_bytes = sum(
            self.contracts_by_id[key].document_bytes
            for key in self.keys[target]
        ) + sum(
            self.contracts_by_id[key].document_bytes
            for key in batch["contracts"]
            if key not in self.keys[target]
        )
        if projected_target_bytes > target_shard.capacity_bytes:
            raise ContractMigrationError(
                "CAPACITY_EXCEEDED",
                (
                    f"target {target} would host {projected_target_bytes} bytes "
                    f"> capacity {target_shard.capacity_bytes}"
                ),
            )
        for key in batch["contracts"]:
            source_record = self.keys[source].get(key)
            if source_record is None:
                raise ContractMigrationError(
                    "SNAPSHOT_CORRUPT", f"source key missing: {source}/{key}"
                )
            if source_record["state"] != "active":
                raise ContractMigrationError(
                    "SNAPSHOT_CORRUPT",
                    f"source key not active: {source}/{key}/{source_record['state']}",
                )
            source_record["state"] = "migrating"
            target_record = self.keys[target].get(key)
            if target_record is None:
                self.keys[target][key] = {
                    "state": "staging",
                    "version": source_record["version"],
                    "last_write": source_record["last_write"],
                }
            elif target_record["state"] != "staging":
                raise ContractMigrationError(
                    "SNAPSHOT_CORRUPT",
                    f"target key not staging: {target}/{key}/{target_record['state']}",
                )
        state["status"] = "active"

    def sync_contract(self, batch: dict, key: str) -> bool:
        state = self.batch_states[batch["batch_id"]]
        if key in state["synced_contracts"]:
            self.skipped_completed_contract_syncs += 1
            return False
        source = batch["source_shard"]
        target = batch["target_shard"]
        source_record = self.keys[source][key]
        target_record = self.keys[target][key]
        target_record["version"] = source_record["version"]
        target_record["last_write"] = source_record["last_write"]
        target_record["state"] = "staging"
        state["synced_contracts"].append(key)
        return True

    def sync_remaining(self, batch: dict) -> dict:
        state = self.batch_states[batch["batch_id"]]
        already = set(state["synced_contracts"])
        newly_synced = 0
        for key in batch["contracts"]:
            if key in already:
                self.skipped_completed_contract_syncs += 1
                continue
            if self.sync_contract(batch, key):
                newly_synced += 1
        return {
            "batch_id": batch["batch_id"],
            "already_synced_skipped": len(batch["contracts"]) - newly_synced,
            "newly_synced": newly_synced,
        }

    def _verify_batch_versions(self, batch: dict) -> list[dict]:
        source = batch["source_shard"]
        target = batch["target_shard"]
        checks: list[dict] = []
        for key in batch["contracts"]:
            source_version = self.keys[source][key]["version"]
            target_version = self.keys[target][key]["version"]
            checks.append(
                {
                    "contract_id": key,
                    "source_version": source_version,
                    "target_version": target_version,
                    "match": source_version == target_version,
                }
            )
        return checks

    def complete_batch(self, batch: dict) -> dict:
        state = self.batch_states[batch["batch_id"]]
        if state["status"] != "active":
            raise ContractMigrationError(
                "SNAPSHOT_CORRUPT",
                f"batch {batch['batch_id']} is not active",
            )
        sync_result = self.sync_remaining(batch)
        version_checks = self._verify_batch_versions(batch)
        if not all(row["match"] for row in version_checks):
            raise ContractMigrationError(
                "SNAPSHOT_CORRUPT",
                f"batch {batch['batch_id']} version mismatch before switch",
            )
        before_loads = self.load_distribution()
        source = batch["source_shard"]
        target = batch["target_shard"]
        for key in batch["contracts"]:
            self.keys[target][key]["state"] = "active"
            self.keys[source][key]["state"] = "pending_reclaim"
        route_change = self.route.replace_range(batch["start"], batch["end"], target)
        state["status"] = "completed"
        self.completed_batches.append(batch["batch_id"])
        self.route_epoch += 1
        after_loads = self.load_distribution()
        switch = {
            "batch_id": batch["batch_id"],
            "route_epoch": self.route_epoch,
            "range": {"start": batch["start"], "end": batch["end"]},
            "source_shard": source,
            "target_shard": target,
            "sync_result": sync_result,
            "version_checks": version_checks,
            "route_change": route_change,
            "loads_before": before_loads,
            "loads_after": after_loads,
            "atomic_route_replacement": True,
        }
        self.switches.append(switch)
        return switch

    def _reference_route(self, key: str) -> str:
        reference = LinearReferenceRouter(self.route.segments)
        return reference.route(key)

    def route_request(self, request: AccessRequest) -> dict:
        if request.operation not in {"read", "write"}:
            raise ContractMigrationError(
                "INVALID_REQUEST_OP",
                f"request {request.request_id} has operation {request.operation}",
            )
        if request.contract_id not in self.contracts_by_id:
            raise ContractMigrationError(
                "UNKNOWN_CONTRACT", f"unknown contract: {request.contract_id}"
            )
        key = request.contract_id
        routed_shard = self.route.route(key)
        reference_shard = self._reference_route(key)
        reference_match = routed_shard == reference_shard
        if not reference_match:
            self.reference_mismatches.append(
                {
                    "request_id": request.request_id,
                    "contract_id": key,
                    "routed_shard": routed_shard,
                    "reference_shard": reference_shard,
                }
            )
        key_record = self.keys[routed_shard][key]
        migration_target = None
        migration_target_record = None
        if key_record["state"] == "migrating":
            for shard_id, shard_keys in self.keys.items():
                if shard_id == routed_shard:
                    continue
                candidate = shard_keys.get(key)
                if candidate is not None and candidate["state"] == "staging":
                    migration_target = shard_id
                    migration_target_record = candidate
                    break

        record = {
            "request_id": request.request_id,
            "sequence": request.sequence,
            "phase": request.phase,
            "operation": request.operation,
            "contract_id": key,
            "client_id": request.client_id,
            "route_epoch": self.route_epoch,
            "routed_shard": routed_shard,
            "reference_shard": reference_shard,
            "reference_match": reference_match,
            "routed_key_state": key_record["state"],
            "migration_target": migration_target,
            "source_visible_version": None,
            "target_visible_version": (
                migration_target_record["version"]
                if migration_target_record is not None
                else None
            ),
            "write_path": [],
            "versions_match_after_write": None,
            "misrouted_pending_reclaim": False,
        }
        if request.operation == "read":
            if key_record["state"] == "pending_reclaim":
                record["misrouted_pending_reclaim"] = True
            record["source_visible_version"] = key_record["version"]
        else:
            if key_record["state"] == "migrating":
                if migration_target is None or migration_target_record is None:
                    raise ContractMigrationError(
                        "SNAPSHOT_CORRUPT",
                        f"migrating key lacks staging target: {key}",
                    )
                source_before = key_record["version"]
                target_before = migration_target_record["version"]
                key_record["version"] += 1
                key_record["last_write"] = request.request_id
                migration_target_record["version"] = key_record["version"]
                migration_target_record["last_write"] = request.request_id
                record["write_path"] = ["source_commit", "target_sync"]
                record["source_visible_version"] = key_record["version"]
                record["target_visible_version"] = migration_target_record["version"]
                record["versions_match_after_write"] = (
                    key_record["version"] == migration_target_record["version"]
                )
                check = {
                    "request_id": request.request_id,
                    "contract_id": key,
                    "source_shard": routed_shard,
                    "target_shard": migration_target,
                    "source_version_before": source_before,
                    "target_version_before": target_before,
                    "source_version_after": key_record["version"],
                    "target_version_after": migration_target_record["version"],
                    "versions_match": record["versions_match_after_write"],
                }
                self.write_checks.append(check)
            else:
                if key_record["state"] == "pending_reclaim":
                    record["misrouted_pending_reclaim"] = True
                key_record["version"] += 1
                key_record["last_write"] = request.request_id
                record["write_path"] = ["single_shard_commit"]
                record["source_visible_version"] = key_record["version"]
        record["content_token"] = f"{key}:v{key_record['version']}"
        self.records.append(record)
        return record

    def loads(self) -> dict[str, dict]:
        contracts = self.contracts_by_id
        owned_bytes = {shard.shard_id: 0 for shard in self.dataset.shards}
        owned_count = {shard.shard_id: 0 for shard in self.dataset.shards}
        for key in contracts:
            owner = self.route.route(key)
            owned_bytes[owner] += contracts[key].document_bytes
            owned_count[owner] += 1
        result: dict[str, dict] = {}
        for shard in self.dataset.shards:
            resident_bytes = sum(
                contracts[key].document_bytes for key in self.keys[shard.shard_id]
            )
            states = Counter(
                record["state"] for record in self.keys[shard.shard_id].values()
            )
            result[shard.shard_id] = {
                "owned_contract_count": owned_count[shard.shard_id],
                "owned_bytes": owned_bytes[shard.shard_id],
                "capacity_bytes": shard.capacity_bytes,
                "owned_watermark_bp": _bp(
                    owned_bytes[shard.shard_id], shard.capacity_bytes
                ),
                "resident_contract_count": len(self.keys[shard.shard_id]),
                "resident_bytes": resident_bytes,
                "resident_watermark_bp": _bp(
                    resident_bytes, shard.capacity_bytes
                ),
                "key_states": {
                    "active": states.get("active", 0),
                    "migrating": states.get("migrating", 0),
                    "staging": states.get("staging", 0),
                    "pending_reclaim": states.get("pending_reclaim", 0),
                },
            }
        return result

    def load_distribution(self) -> list[dict]:
        return [
            {"shard": shard_id, **values}
            for shard_id, values in self.loads().items()
        ]

    def contract_ownership(self) -> list[dict]:
        rows: list[dict] = []
        for contract in self.dataset.contracts:
            key = contract.contract_id
            owner = self.route.route(key)
            physical = []
            for shard in self.dataset.shards:
                record = self.keys[shard.shard_id].get(key)
                if record is not None:
                    physical.append(
                        {
                            "shard": shard.shard_id,
                            "state": record["state"],
                            "logical_version": record["version"],
                        }
                    )
            rows.append(
                {
                    "contract_id": key,
                    "shard": owner,
                    "logical_version": self.keys[owner][key]["version"],
                    "physical_copies": physical,
                }
            )
        return rows

    def reclaim_rows(self) -> list[dict]:
        rows: list[dict] = []
        for batch_id in self.completed_batches:
            batch = self.batch_by_id(batch_id)
            for key in batch["contracts"]:
                rows.append(
                    {
                        "contract_id": key,
                        "source_shard": batch["source_shard"],
                        "target_shard": batch["target_shard"],
                        "source_state": self.keys[batch["source_shard"]][key]["state"],
                        "target_state": self.keys[batch["target_shard"]][key]["state"],
                        "route_shard": self.route.route(key),
                    }
                )
        return rows

    def state_signature(self) -> str:
        payload = {
            "route": [segment.as_dict() for segment in self.route.segments],
            "ownership": [
                [row["contract_id"], row["shard"], row["logical_version"]]
                for row in self.contract_ownership()
            ],
            "keys": {
                shard_id: {
                    key: [record["state"], record["version"]]
                    for key, record in sorted(shard_keys.items())
                }
                for shard_id, shard_keys in sorted(self.keys.items())
            },
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def snapshot(
        self,
        request_cursor: int,
        next_batch_index: int,
        active_batch_id: str | None,
        parameters: dict,
    ) -> dict:
        return {
            "schema_version": "1.0",
            "kind": "contract_migration_snapshot",
            "seed": self.dataset.seed,
            "parameters": dict(parameters),
            "plan_fingerprint": self.plan_fingerprint,
            "request_cursor": request_cursor,
            "next_batch_index": next_batch_index,
            "active_batch_id": active_batch_id,
            "route_epoch": self.route_epoch,
            "route_segments": [segment.as_dict() for segment in self.route.segments],
            "completed_batches": list(self.completed_batches),
            "batch_states": {
                batch_id: {
                    "status": state["status"],
                    "synced_contracts": list(state["synced_contracts"]),
                }
                for batch_id, state in self.batch_states.items()
            },
            "keys": {
                shard_id: {
                    key: {
                        "state": record["state"],
                        "version": record["version"],
                        "last_write": record["last_write"],
                    }
                    for key, record in sorted(shard_keys.items())
                }
                for shard_id, shard_keys in sorted(self.keys.items())
            },
            "skipped_completed_contract_syncs": self.skipped_completed_contract_syncs,
        }

    @classmethod
    def restore(
        cls,
        dataset: Dataset,
        plan: dict,
        snapshot: dict,
    ) -> "MigrationEngine":
        try:
            if snapshot.get("kind") != "contract_migration_snapshot":
                raise ValueError("wrong snapshot kind")
            if snapshot["seed"] != dataset.seed:
                raise ValueError("seed mismatch")
            if snapshot["plan_fingerprint"] != _plan_fingerprint(plan):
                raise ContractMigrationError(
                    "RESUME_PLAN_MISMATCH",
                    "snapshot plan fingerprint does not match regenerated plan",
                )
            engine = cls(dataset, plan)
            engine.route = RangeRouter(
                [
                    RouteSegment(row["start"], row["end"], row["shard"])
                    for row in snapshot["route_segments"]
                ]
            )
            engine.route_epoch = int(snapshot["route_epoch"])
            engine.completed_batches = list(snapshot["completed_batches"])
            engine.batch_states = {
                batch_id: {
                    "status": state["status"],
                    "synced_contracts": list(state["synced_contracts"]),
                }
                for batch_id, state in snapshot["batch_states"].items()
            }
            engine.keys = {
                shard_id: {
                    key: {
                        "state": record["state"],
                        "version": int(record["version"]),
                        "last_write": record["last_write"],
                    }
                    for key, record in shard_keys.items()
                }
                for shard_id, shard_keys in snapshot["keys"].items()
            }
            engine.skipped_completed_contract_syncs = int(
                snapshot.get("skipped_completed_contract_syncs", 0)
            )
            if set(engine.keys) != {
                shard.shard_id for shard in dataset.shards
            }:
                raise ValueError("shard key set mismatch")
            engine._validate_restored_state()
            return engine
        except ContractMigrationError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractMigrationError(
                "SNAPSHOT_CORRUPT", f"invalid snapshot: {exc}"
            ) from exc

    def _validate_restored_state(self) -> None:
        for shard in self.dataset.shards:
            if shard.shard_id not in self.keys:
                raise ValueError(f"missing shard {shard.shard_id}")
        for contract in self.dataset.contracts:
            key = contract.contract_id
            owners = [
                shard_id
                for shard_id, shard_keys in self.keys.items()
                if key in shard_keys
            ]
            routed_shard = self.route.route(key)
            if routed_shard not in owners:
                raise ValueError(f"routed key is not resident: {key}")
            routed_state = self.keys[routed_shard][key]["state"]
            if routed_state not in {"active", "migrating"}:
                raise ValueError(
                    f"routed key has non-serving state: {key}/{routed_state}"
                )
        for batch_id in self.completed_batches:
            batch = self.batch_by_id(batch_id)
            if self.batch_states[batch_id]["status"] != "completed":
                raise ValueError(f"completed batch has wrong status: {batch_id}")
            for key in batch["contracts"]:
                if self.keys[batch["source_shard"]][key]["state"] != "pending_reclaim":
                    raise ValueError(f"source key not pending reclaim: {key}")
                if self.keys[batch["target_shard"]][key]["state"] != "active":
                    raise ValueError(f"target key not active: {key}")
        for batch_id, state in self.batch_states.items():
            if state["status"] == "active":
                batch = self.batch_by_id(batch_id)
                for key in batch["contracts"]:
                    if self.keys[batch["source_shard"]][key]["state"] != "migrating":
                        raise ValueError(f"active batch source key not migrating: {key}")
                    if self.keys[batch["target_shard"]][key]["state"] != "staging":
                        raise ValueError(f"active batch target key not staging: {key}")


def _plan_fingerprint(plan: dict) -> str:
    compact = {
        "plans": [
            {
                "start": item["start"],
                "end": item["end"],
                "source": item["source_shard"],
                "target": item["target_shard"],
                "status": item["status"],
            }
            for item in plan["plans"]
        ],
        "batches": [
            {
                "batch_id": batch["batch_id"],
                "start": batch["start"],
                "end": batch["end"],
                "contracts": batch["contracts"],
                "source": batch["source_shard"],
                "target": batch["target_shard"],
            }
            for batch in plan["flattened_batches"]
        ],
    }
    encoded = json.dumps(
        compact, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
