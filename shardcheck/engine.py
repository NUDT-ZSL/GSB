from __future__ import annotations

import copy
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .checkpoints import CheckpointStore
from .config import DataShard, PolicyRule, SampleBundle, TrainingConfig
from .errors import (
    CheckpointCorrupt,
    FailureRecord,
    InjectedFingerprintConflict,
    InjectedGenerateError,
    InjectedMergeError,
    InjectedOccupancyError,
    InjectedRetentionError,
    InjectedWriteError,
    MergeError,
    PolicyError,
    ShardCheckError,
    ShardConflict,
    ShardGenerationError,
    ShardSizeExceeded,
    ShardWriteError,
    SnapshotManifestMismatch,
    SnapshotVersionMismatch,
    StorageBudgetExceeded,
)
from .injection import Injection, InjectionTable
from .io import atomic_write_bytes, atomic_write_json, remove_tree, sha256_bytes, sha256_json
from .model import DeterministicModel
from .report import build_consistency_report, seal_report, write_report
from .scheduler import BudgetPlan, plan_budget
from .shards import (
    AtomicShardWriter,
    PhysicalShard,
    build_logical_shards,
    estimate_metadata_bytes,
    make_physical_shard,
    plan_merge_groups,
    read_generation_payload,
    sort_physical_shards,
    validate_logical_shards,
)
from .snapshot import StateStore


REPORT_SCHEMA_VERSION = 1


class TrainingEngine:
    def __init__(
        self,
        bundle: SampleBundle,
        run_dir: Path,
        injection_table: InjectionTable,
        *,
        write_delay_seconds: float = 0.0,
        round_delay_seconds: float = 0.0,
        fresh: bool = False,
    ):
        self.bundle = bundle
        self.config = bundle.config
        self.policy = bundle.policy
        self.run_dir = run_dir
        self.injection_table = injection_table
        self.write_delay_seconds = max(0.0, float(write_delay_seconds))
        self.round_delay_seconds = max(0.0, float(round_delay_seconds))
        self.fresh = fresh
        self.store = CheckpointStore(run_dir / "checkpoint_store", self.config)
        self.state = StateStore(run_dir / "state")
        self.report_path = run_dir / "report.json"
        self.final_params_path = run_dir / "final_parameters.bin"
        self.stop_event = threading.Event()
        self.stop_signal: int | None = None
        self.manifest: dict[str, Any] = {}
        self.model: DeterministicModel | None = None
        self.round_cursor = 0
        self.start_round = 1
        self.previous_run_state: dict[str, Any] | None = None
        self.recovery_info: dict[str, Any] = {}
        self.checkpoint_verification = {
            "shard_files_verified": False,
            "split_lossless": False,
            "checkpoint_verifies_at_its_round": False,
            "checkpoint_round": 0,
        }

    def request_stop(self, signum: int) -> None:
        self.stop_signal = int(signum)
        self.stop_event.set()

    def _replay_model(self, round_cursor: int) -> DeterministicModel:
        model = DeterministicModel(self.config)
        for round_number in range(1, round_cursor + 1):
            model.apply_round(round_number, self.bundle.data_shards)
        return model

    def _initialize(self) -> None:
        if self.fresh and self.run_dir.exists():
            remove_tree(self.run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.store.root.mkdir(parents=True, exist_ok=True)
        self.store.generations_dir.mkdir(parents=True, exist_ok=True)
        self.state.root.mkdir(parents=True, exist_ok=True)
        self.previous_run_state = self.state.read_run_state()
        self.manifest = self.store.load_manifest()
        snapshot = self.state.load_snapshot(self.config, self.policy)
        orphans = self.store.recover_orphans(self.manifest)

        snapshot_cursor = int(snapshot["round_cursor"]) if snapshot is not None else 0
        manifest_round = self.store.current_round(self.manifest)
        cursor = max(snapshot_cursor, manifest_round)
        reconciled = manifest_round > snapshot_cursor

        if snapshot is not None and snapshot.get("manifest_digest") != self.manifest.get(
            "manifest_digest"
        ):
            if not reconciled:
                raise SnapshotManifestMismatch(
                    "snapshot manifest digest does not match the active checkpoint manifest",
                    details={
                        "snapshot_manifest_digest": snapshot.get("manifest_digest"),
                        "manifest_digest": self.manifest.get("manifest_digest"),
                    },
                )
        if cursor > self.config.total_rounds:
            raise CheckpointCorrupt(
                f"round cursor {cursor} exceeds configured total_rounds {self.config.total_rounds}"
            )

        checkpoint_model, generation, _ = self.store.verify_and_restore(self.manifest)
        checkpoint_round = int(generation["round"]) if generation else 0
        checkpoint_ok = generation is None
        if generation is not None and checkpoint_model is not None:
            replayed_checkpoint = self._replay_model(checkpoint_round)
            checkpoint_ok = replayed_checkpoint.digest == checkpoint_model.digest
            if not checkpoint_ok:
                raise CheckpointCorrupt(
                    "checkpoint parameters cannot be reproduced from the training transcript",
                    details={"generation_id": generation.get("generation_id")},
                )
        self.checkpoint_verification = {
            "shard_files_verified": True,
            "split_lossless": True,
            "checkpoint_verifies_at_its_round": checkpoint_ok,
            "checkpoint_round": checkpoint_round,
        }

        if snapshot is not None and snapshot.get("phase") in {"write", "retention"}:
            record = snapshot.get("round_record")
            if isinstance(record, dict) and int(record.get("round", -1)) == cursor:
                existing = {int(item["round"]) for item in self.state.read_journal()}
                if cursor not in existing:
                    self.state.upsert_journal_record(record)
        journal = [item for item in self.state.read_journal() if int(item["round"]) <= cursor]
        self.state.write_journal(journal)

        self.round_cursor = cursor
        self.start_round = cursor + 1
        self.model = self._replay_model(cursor)

        if self.previous_run_state is None and not orphans and snapshot is None and manifest_round == 0:
            mode = "fresh"
        elif self.previous_run_state is not None and self.previous_run_state.get("status") == "running":
            mode = "forced_termination_resume"
        elif reconciled:
            mode = "manifest_reconciliation_resume"
        elif snapshot is not None:
            mode = "snapshot_resume"
        else:
            mode = "manifest_recovery"
        previous_status = (
            self.previous_run_state.get("status") if isinstance(self.previous_run_state, dict) else None
        )
        self.recovery_info = {
            "mode": mode,
            "previous_run_status": previous_status,
            "resume_cursor": cursor,
            "resume_from_round": self.start_round,
            "manifest_reconciled": reconciled,
            "orphan_directories_removed": orphans,
            "snapshot_phase": snapshot.get("phase") if snapshot else None,
            "checkpoint_round": checkpoint_round,
            "clean_previous_exit": previous_status in {"clean_exit", "completed"},
        }
        self.state.write_run_state(
            {
                "schema_version": 1,
                "status": "running",
                "pid": os.getpid(),
                "last_round": cursor,
                "start_round": self.start_round,
                "recovery_mode": mode,
                "previous_run_status": previous_status,
            }
        )
        self.state.write_progress(
            {
                "status": "running",
                "last_round": cursor,
                "round_cursor": cursor,
                "parameter_digest": self.model.digest,
                "checkpoint_generation": self.manifest.get("global_generation", 0),
                "recovery_mode": mode,
            }
        )

    @staticmethod
    def _failure(
        round_number: int,
        shard_id: str | None,
        stage: str,
        code: str,
        exception_type: str,
        message: str,
        *,
        injection_id: str | None = None,
        fatal: bool = True,
        details: dict[str, Any] | None = None,
    ) -> FailureRecord:
        return FailureRecord(
            round=round_number,
            shard_id=shard_id,
            stage=stage,
            code=code,
            exception_type=exception_type,
            message=message,
            injection_id=injection_id,
            fatal=fatal,
            details=details or {},
        )

    @staticmethod
    def _hit(
        injection: Injection, stage: str, code: str, outcome: str, shard_id: str | None = None
    ) -> dict[str, Any]:
        return {
            "injection_id": injection.injection_id,
            "type": injection.injection_type,
            "round": injection.round,
            "stage": stage,
            "code": code,
            "outcome": outcome,
            "shard_id": shard_id,
        }

    def _snapshot(
        self,
        phase: str,
        *,
        round_cursor: int,
        active_round: int,
        round_record: dict[str, Any] | None = None,
        pending_retention: bool = False,
    ) -> None:
        if self.model is None:
            raise RuntimeError("engine model is not initialized")
        self.state.write_snapshot(
            config=self.config,
            policy=self.policy,
            phase=phase,
            round_cursor=round_cursor,
            active_round=active_round,
            manifest_digest=self.manifest["manifest_digest"],
            params_digest=self.model.digest,
            checkpoint_generation=int(self.manifest.get("global_generation", 0)),
            pending_retention=pending_retention,
            round_record=round_record,
        )

    def _build_round_record(self, round_number: int) -> dict[str, Any]:
        return {
            "round": round_number,
            "remaining_rounds": self.config.total_rounds - round_number + 1,
            "checkpoint_due": False,
            "training": {},
            "policy": {},
            "budget": {},
            "merge_operations": [],
            "split_operations": [],
            "write_operations": [],
            "evictions": [],
            "checkpoint": {},
            "failures": [],
            "injection_hits": [],
        }

    def _logical_shards_with_injections(
        self, round_number: int, record: dict[str, Any]
    ) -> tuple[list[tuple[dict[str, Any], bytes]], bool]:
        logical = build_logical_shards(self.model, round_number)
        failed_indexes: set[int] = set()
        injection_for_index: dict[int, Injection] = {}
        for injection in self.injection_table.for_round(round_number):
            if injection.injection_type not in {"oversized_shard", "generate_exception"}:
                continue
            index = injection.target_shard_index if injection.target_shard_index is not None else 0
            if index >= len(logical):
                continue
            descriptor, payload = logical[index]
            if injection.injection_type == "oversized_shard":
                new_size = self.config.shard_size_limit_bytes + 8
                if len(payload) < new_size:
                    payload = payload + b"P" * (new_size - len(payload))
                descriptor = dict(descriptor)
                descriptor["size_bytes"] = len(payload)
                descriptor["content_fingerprint"] = sha256_bytes(payload)
                descriptor["injected_oversize"] = injection.injection_id
                logical[index] = (descriptor, payload)
                injection_for_index[index] = injection
            else:
                failed_indexes.add(index)
                record["failures"].append(
                    self._failure(
                        round_number,
                        str(descriptor["logical_shard_id"]),
                        "generate",
                        ShardGenerationError.code,
                        "InjectedGenerateError",
                        f"injected generation exception for {descriptor['logical_shard_id']}",
                        injection_id=injection.injection_id,
                        fatal=True,
                    ).as_dict()
                )
                record["injection_hits"].append(
                    self._hit(
                        injection,
                        "generate",
                        ShardGenerationError.code,
                        "exception_injected",
                        str(descriptor["logical_shard_id"]),
                    )
                )

        validation = validate_logical_shards(logical, self.config)
        validation_by_id = {
            str(descriptor["logical_shard_id"]): message
            for descriptor, _, message in validation
        }
        for index, (descriptor, _) in enumerate(logical):
            if index in failed_indexes:
                continue
            logical_id = str(descriptor["logical_shard_id"])
            if logical_id not in validation_by_id:
                continue
            failed_indexes.add(index)
            injection = injection_for_index.get(index)
            if logical_id in {str(item[0]["logical_shard_id"]) for item in logical}:
                message = validation_by_id[logical_id]
                if "size limit" in message:
                    code = ShardSizeExceeded.code
                    exception_type = "ShardSizeExceeded"
                    stage = "generate"
                else:
                    code = ShardGenerationError.code
                    exception_type = "ShardGenerationError"
                    stage = "generate"
                record["failures"].append(
                    self._failure(
                        round_number,
                        logical_id,
                        stage,
                        code,
                        exception_type,
                        message,
                        injection_id=injection.injection_id if injection else None,
                        fatal=True,
                        details={"size_bytes": int(descriptor["size_bytes"]), "limit": self.config.shard_size_limit_bytes},
                    ).as_dict()
                )
                if injection is not None:
                    record["injection_hits"].append(
                        self._hit(injection, "generate", code, "validation_failed", logical_id)
                    )
        valid = [item for index, item in enumerate(logical) if index not in failed_indexes]
        return valid, bool(failed_indexes)

    def _write_phase(
        self,
        round_number: int,
        record: dict[str, Any],
        physicals: list[PhysicalShard],
        *,
        sequence: int,
    ) -> tuple[bool, list[dict[str, Any]]]:
        staging = self.store.create_staging_dir(sequence)
        ordered = sort_physical_shards(physicals, record["policy"]["write_priority"])
        writer = AtomicShardWriter(staging / "shards", delay_seconds=self.write_delay_seconds)
        write_injection = self.injection_table.get_one(round_number, "write_exception")
        conflict_injection = self.injection_table.get_one(round_number, "fingerprint_conflict")
        target_write_index = (
            write_injection.target_physical_index
            if write_injection is not None and write_injection.target_physical_index is not None
            else 0
        )
        target_conflict_index = (
            conflict_injection.target_physical_index
            if conflict_injection is not None and conflict_injection.target_physical_index is not None
            else 0
        )

        def write_one(index: int, shard: PhysicalShard) -> dict[str, Any]:
            result = writer.write(
                shard,
                injected_write_error=(
                    write_injection is not None and index == target_write_index
                ),
            )
            if conflict_injection is not None and index == target_conflict_index:
                mutated = bytes([shard.payload[0] ^ 0xFF]) + shard.payload[1:]
                writer.write(shard, payload_override=mutated)
            return result

        failures: list[dict[str, Any]] = []
        write_operations: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=self.config.write_concurrency) as executor:
            futures = [executor.submit(write_one, index, shard) for index, shard in enumerate(ordered)]
            for index, (shard, future) in enumerate(zip(ordered, futures)):
                try:
                    result = future.result()
                    result = dict(result)
                    result["priority_index"] = index
                    write_operations.append(result)
                except BaseException as exc:
                    if isinstance(exc, ShardConflict):
                        code = ShardConflict.code
                        exception_type = type(exc).__name__
                        injection = (
                            conflict_injection
                            if conflict_injection is not None and index == target_conflict_index
                            else None
                        )
                    elif isinstance(exc, (InjectedWriteError, ShardWriteError, OSError)):
                        code = ShardWriteError.code
                        exception_type = type(exc).__name__
                        injection = (
                            write_injection
                            if write_injection is not None and index == target_write_index
                            else None
                        )
                    else:
                        code = ShardWriteError.code
                        exception_type = type(exc).__name__
                        injection = None
                    failure = self._failure(
                        round_number,
                        shard.shard_id,
                        "write",
                        code,
                        exception_type,
                        str(exc),
                        injection_id=injection.injection_id if injection else None,
                        fatal=True,
                    )
                    failures.append(failure.as_dict())
                    write_operations.append(
                        {
                            "physical_shard_id": shard.shard_id,
                            "priority_index": index,
                            "status": "failed",
                            "code": code,
                            "exception_type": exception_type,
                        }
                    )
                    if injection is not None:
                        record["injection_hits"].append(
                            self._hit(
                                injection,
                                "write",
                                code,
                                "exception_injected" if code == ShardWriteError.code else "conflict_detected",
                                shard.shard_id,
                            )
                        )
        record["failures"].extend(failures)
        record["write_operations"] = write_operations
        if failures:
            remove_tree(staging)
            return False, ordered
        return True, ordered

    def _commit_generation(
        self,
        round_number: int,
        record: dict[str, Any],
        physicals: list[PhysicalShard],
        merge_operations: list[dict[str, Any]],
        split_operations: list[dict[str, Any]],
        staging: bool,
        sequence: int,
        plan: BudgetPlan,
    ) -> None:
        staging_dir = self.store.root / f".staging-gen-{sequence:06d}"
        final_dir = self.store.finalize_generation_dir(staging_dir, sequence)
        payload_bytes = sum(shard.size_bytes for shard in physicals)
        members = [member for shard in physicals for member in shard.descriptor["members"]]
        logical_sorted = sorted(members, key=lambda item: int(item["chunk_index"]))
        logical_manifest_digest = sha256_json(
            [
                {
                    "logical_shard_id": item["logical_shard_id"],
                    "file_offset": item["file_offset"],
                    "size_bytes": item["size_bytes"],
                    "content_fingerprint": item["content_fingerprint"],
                }
                for item in logical_sorted
            ]
        )
        shards_payload: list[dict[str, Any]] = []
        for shard in physicals:
            descriptor = copy.deepcopy(shard.descriptor)
            descriptor["shard_version"] = sequence
            for member in descriptor["members"]:
                member["shard_version"] = sequence
            shards_payload.append(descriptor)
        generation = {
            "generation_id": final_dir.name,
            "generation": sequence,
            "round": round_number,
            "directory": str(final_dir.relative_to(self.store.root)),
            "status": "complete",
            "shard_version": sequence,
            "config_hash": self.config.config_hash,
            "policy_hash": self.policy.policy_hash,
            "policy_rule_id": record["policy"]["rule_id"],
            "params_digest": self.model.digest,
            "parameter_bytes": self.model.parameter_bytes,
            "payload_bytes": payload_bytes,
            "metadata_overhead_bytes": estimate_metadata_bytes(physicals),
            "physical_shard_count": len(physicals),
            "logical_shard_count": len(members),
            "logical_manifest_digest": logical_manifest_digest,
            "shards": shards_payload,
            "merge_operations": merge_operations,
            "split_operations": split_operations,
        }
        self.manifest, _ = self.store.commit_generation(
            self.manifest,
            generation,
            evicted_generation_ids=plan.evicted_generation_ids,
        )
        removed = set(self.store.delete_generation_dirs(plan.evicted_generation_ids, self.manifest))
        record["evictions"] = [
            dict(item, directory_removed=item["generation_id"] in removed) for item in plan.evictions
        ]
        record["checkpoint"] = {
            "due": True,
            "committed": True,
            "generation_id": generation["generation_id"],
            "generation": generation["generation"],
            "round": round_number,
            "physical_shard_count": len(physicals),
            "logical_shard_count": len(members),
            "payload_bytes": payload_bytes,
            "metadata_overhead_bytes": generation["metadata_overhead_bytes"],
            "params_digest": self.model.digest,
            "manifest_digest": self.manifest["manifest_digest"],
        }
        self.round_cursor = round_number

    def _run_round(self, round_number: int) -> dict[str, Any]:
        if self.model is None:
            raise RuntimeError("engine model is not initialized")
        record = self._build_round_record(round_number)
        training = self.model.apply_round(round_number, self.bundle.data_shards)
        record["training"] = training
        self._snapshot(
            "train",
            round_cursor=round_number - 1,
            active_round=round_number,
            round_record=record,
        )

        current_usage = self.store.current_usage(self.manifest)
        actual_occupancy = current_usage / self.config.global_budget_bytes
        occupancy_override: float | None = None
        occupancy_injection = self.injection_table.get_one(
            round_number, "budget_occupancy_out_of_range"
        )
        if occupancy_injection is not None:
            try:
                occupancy_override = float(occupancy_injection.value)
            except (TypeError, ValueError):
                occupancy_override = 1.5
            if not (0.0 <= occupancy_override <= 1.0):
                record["failures"].append(
                    self._failure(
                        round_number,
                        None,
                        "budget",
                        "E_BUDGET_OCCUPANCY_OUT_OF_RANGE",
                        "InjectedOccupancyError",
                        f"injected budget occupancy {occupancy_override} is outside [0, 1]",
                        injection_id=occupancy_injection.injection_id,
                        fatal=False,
                        details={"injected_occupancy": occupancy_override},
                    ).as_dict()
                )
                record["injection_hits"].append(
                    self._hit(
                        occupancy_injection,
                        "budget",
                        "E_BUDGET_OCCUPANCY_OUT_OF_RANGE",
                        "value_clamped_for_policy_lookup",
                    )
                )
        policy_occupancy = min(
            1.0, max(0.0, occupancy_override if occupancy_override is not None else actual_occupancy)
        )
        remaining_rounds = self.config.total_rounds - round_number + 1
        try:
            rule = self.policy.select(policy_occupancy, remaining_rounds)
        except PolicyError as exc:
            record["failures"].append(
                self._failure(
                    round_number,
                    None,
                    "budget",
                    exc.code,
                    type(exc).__name__,
                    str(exc),
                    fatal=True,
                ).as_dict()
            )
            record["policy"] = {
                "rule_id": None,
                "policy_occupancy": policy_occupancy,
                "remaining_rounds": remaining_rounds,
                "error": str(exc),
            }
            record["checkpoint"] = {
                "due": False,
                "committed": False,
                "decision": "reject_policy_error",
            }
            record["budget"] = {
                "current_usage_bytes": current_usage,
                "occupancy_ratio": actual_occupancy,
                "policy_occupancy": policy_occupancy,
                "decision": "reject_policy_error",
            }
            self._snapshot(
                "write",
                round_cursor=round_number,
                active_round=round_number,
                round_record=record,
            )
            self._snapshot(
                "retention",
                round_cursor=round_number,
                active_round=round_number,
                round_record=record,
            )
            self.round_cursor = round_number
            return self._finish_round_record(record)

        record["policy"] = rule.as_dict()
        record["policy"]["policy_occupancy"] = policy_occupancy
        record["policy"]["remaining_rounds"] = remaining_rounds
        checkpoint_due = (
            round_number % self.config.snapshot_commit_interval_rounds == 0
            or round_number == self.config.total_rounds
        )
        record["checkpoint_due"] = checkpoint_due

        valid_logical, generation_failed = self._logical_shards_with_injections(round_number, record)
        merge_groups = plan_merge_groups(valid_logical, rule)
        physicals: list[PhysicalShard] = []
        merge_operations: list[dict[str, Any]] = []
        split_operations: list[dict[str, Any]] = []
        merge_failed = False
        merge_injection = self.injection_table.get_one(round_number, "merge_exception")
        target_merge_index = (
            merge_injection.target_physical_index
            if merge_injection is not None and merge_injection.target_physical_index is not None
            else 0
        )
        for group_index, group in enumerate(merge_groups):
            if merge_injection is not None and group_index == target_merge_index:
                shard_id = str(group[0][0]["logical_shard_id"])
                record["failures"].append(
                    self._failure(
                        round_number,
                        shard_id,
                        "merge",
                        MergeError.code,
                        "InjectedMergeError",
                        f"injected merge exception for group {group_index}",
                        injection_id=merge_injection.injection_id,
                        fatal=True,
                    ).as_dict()
                )
                record["injection_hits"].append(
                    self._hit(
                        merge_injection,
                        "merge",
                        MergeError.code,
                        "exception_injected",
                        shard_id,
                    )
                )
                merge_failed = True
                continue
            try:
                physical, merge_op, split_op = make_physical_shard(group, round_number, group_index)
                physicals.append(physical)
                if merge_op is not None:
                    merge_operations.append(merge_op)
                if split_op is not None:
                    split_operations.append(split_op)
            except BaseException as exc:
                merge_failed = True
                record["failures"].append(
                    self._failure(
                        round_number,
                        str(group[0][0]["logical_shard_id"]),
                        "merge",
                        MergeError.code,
                        type(exc).__name__,
                        str(exc),
                        fatal=True,
                    ).as_dict()
                )
        record["merge_operations"] = merge_operations
        record["split_operations"] = split_operations
        pending_bytes = sum(shard.size_bytes for shard in physicals)

        retention_injection = self.injection_table.get_one(round_number, "illegal_retention")
        retention_override: int | None = None
        if retention_injection is not None:
            try:
                retention_override = int(retention_injection.value)
            except (TypeError, ValueError):
                retention_override = 0
        plan = plan_budget(
            manifest=self.manifest,
            pending_bytes=pending_bytes,
            global_budget_bytes=self.config.global_budget_bytes,
            desired_retention=rule.retention_generations,
            max_retention=self.config.retention_generations,
            budget_action=rule.budget_action,
            checkpoint_due=checkpoint_due,
            occupancy_override=occupancy_override,
            retention_override=retention_override,
        )
        record["budget"] = plan.as_dict()
        if plan.retention_illegal:
            injection_id = retention_injection.injection_id if retention_injection is not None else None
            record["failures"].append(
                self._failure(
                    round_number,
                    None,
                    "retention",
                    "E_ILLEGAL_RETENTION",
                    "InjectedRetentionError" if retention_injection else "RetentionPolicyError",
                    f"retention request {plan.desired_retention} was clamped to {plan.effective_retention}; "
                    f"configured maximum is {self.config.retention_generations}",
                    injection_id=injection_id,
                    fatal=False,
                    details={"requested": plan.desired_retention, "effective": plan.effective_retention},
                ).as_dict()
            )
            if retention_injection is not None:
                record["injection_hits"].append(
                    self._hit(
                        retention_injection,
                        "retention",
                        "E_ILLEGAL_RETENTION",
                        "retention_clamped",
                    )
                )

        round_failed = generation_failed or merge_failed
        if round_failed:
            record["checkpoint"] = {
                "due": checkpoint_due,
                "committed": False,
                "decision": "reject_round_failure",
                "reason": "one or more shard generation or merge operations failed",
            }
        elif plan.decision == "reject":
            bucket_failure = self._failure(
                round_number,
                None,
                "budget",
                StorageBudgetExceeded.code,
                "StorageBudgetExceeded",
                plan.reason,
                fatal=False,
                details={
                    "current_usage_bytes": plan.current_usage_bytes,
                    "pending_bytes": plan.pending_bytes,
                    "global_budget_bytes": self.config.global_budget_bytes,
                },
            )
            record["failures"].append(bucket_failure.as_dict())
            record["checkpoint"] = {
                "due": checkpoint_due,
                "committed": False,
                "decision": "reject_storage_budget",
                "reason": plan.reason,
            }
        elif plan.decision == "deferred_by_interval":
            record["checkpoint"] = {
                "due": False,
                "committed": False,
                "decision": "deferred_by_interval",
            }
        elif plan.decision == "commit":
            sequence = int(self.manifest.get("global_generation", 0)) + 1
            wrote, ordered = self._write_phase(
                round_number,
                record,
                physicals,
                sequence=sequence,
            )
            if wrote and not round_failed:
                self._commit_generation(
                    round_number,
                    record,
                    physicals,
                    merge_operations,
                    split_operations,
                    True,
                    sequence,
                    plan,
                )
            elif wrote:
                remove_tree(self.store.root / f".staging-gen-{sequence:06d}")
            if not wrote:
                record["checkpoint"] = {
                    "due": checkpoint_due,
                    "committed": False,
                    "decision": "reject_write_failure",
                    "reason": "one or more shard writes failed",
                }
        else:
            record["checkpoint"] = {
                "due": checkpoint_due,
                "committed": False,
                "decision": plan.decision,
            }

        self.round_cursor = round_number
        self._snapshot(
            "write",
            round_cursor=round_number,
            active_round=round_number,
            round_record=record,
        )
        self._snapshot(
            "retention",
            round_cursor=round_number,
            active_round=round_number,
            round_record=record,
        )
        return self._finish_round_record(record)

    def _finish_round_record(self, record: dict[str, Any]) -> dict[str, Any]:
        round_number = int(record["round"])
        self.state.upsert_journal_record(record)
        self.state.write_progress(
            {
                "status": "running",
                "last_round": round_number,
                "round_cursor": self.round_cursor,
                "parameter_digest": self.model.digest if self.model else None,
                "checkpoint_generation": self.manifest.get("global_generation", 0),
                "checkpoint_committed": bool(record.get("checkpoint", {}).get("committed")),
            }
        )
        return record

    def _build_report(self) -> dict[str, Any]:
        if self.model is None:
            raise RuntimeError("engine model is not initialized")
        rounds = sorted(self.state.read_journal(), key=lambda item: int(item["round"]))
        failures = [failure for record in rounds for failure in record.get("failures", [])]
        injection_hits = [hit for record in rounds for hit in record.get("injection_hits", [])]
        merge_ops = [op for record in rounds for op in record.get("merge_operations", [])]
        split_ops = [op for record in rounds for op in record.get("split_operations", [])]
        evictions = [op for record in rounds for op in record.get("evictions", [])]
        snapshot = self.state.load_snapshot(self.config, self.policy)
        current_generation = self.store.current_generation(self.manifest)
        checkpoint_round = int(current_generation["round"]) if current_generation else 0
        shard_verified = False
        split_lossless = False
        checkpoint_matches = False
        try:
            checkpoint_model, generation, split_operations = self.store.verify_and_restore(self.manifest)
            shard_verified = True
            split_lossless = all(bool(op.get("lossless")) for op in split_operations)
            if generation is not None and checkpoint_model is not None:
                replay = self._replay_model(int(generation["round"]))
                checkpoint_matches = replay.digest == checkpoint_model.digest
        except ShardCheckError:
            shard_verified = False
        consistency = build_consistency_report(
            config=self.config,
            manifest=self.manifest,
            rounds=rounds,
            final_params_digest=self.model.digest,
            checkpoint_verifies_at_its_round=checkpoint_matches or current_generation is None,
            checkpoint_round=checkpoint_round,
            round_cursor=self.round_cursor,
            snapshot=snapshot,
            shard_files_verified=shard_verified,
            split_lossless=split_lossless,
        )
        consistency["checks"]["final_parameter_digest_is_reported"] = (
            consistency["final_params_digest"] == self.model.digest
        )
        consistency["all_passed"] = all(consistency["checks"].values())
        final_shards = current_generation.get("shards", []) if current_generation else []
        generations = [
            {
                "generation_id": generation["generation_id"],
                "generation": generation["generation"],
                "round": generation["round"],
                "payload_bytes": generation["payload_bytes"],
                "physical_shard_count": generation["physical_shard_count"],
                "logical_shard_count": generation["logical_shard_count"],
                "params_digest": generation["params_digest"],
                "shard_version": generation["shard_version"],
            }
            for generation in self.manifest.get("generations", [])
        ]
        exit_path = "graceful_signal" if self.stop_signal is not None else "natural_completion"
        if self.start_round > self.config.total_rounds and self.stop_signal is None:
            exit_path = "already_complete"
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "report_kind": "shardcheck_budget_consistency",
            "config_hash": self.config.config_hash,
            "policy_hash": self.policy.policy_hash,
            "data_hash": self.bundle.data_hash,
            "injection_source_hash": self.injection_table.source_hash,
            "total_rounds": self.config.total_rounds,
            "round_cursor": self.round_cursor,
            "rounds": rounds,
            "parameter_layout": self.model.layout_dict(),
            "final_params_digest": self.model.digest,
            "final_parameter_bytes": self.model.parameter_bytes,
            "final_parameters_file": "final_parameters.bin",
            "final_shards": final_shards,
            "checkpoint_manifest": {
                "manifest_version": self.manifest.get("manifest_version"),
                "manifest_digest": self.manifest.get("manifest_digest"),
                "global_generation": self.manifest.get("global_generation"),
                "current_generation": self.manifest.get("current_generation"),
                "total_bytes": self.manifest.get("total_bytes"),
                "config_hash": self.manifest.get("config_hash"),
            },
            "generations": generations,
            "operations": {
                "merge": merge_ops,
                "split": split_ops,
                "evictions": evictions,
                "writes": [write for record in rounds for write in record.get("write_operations", [])],
            },
            "recovery": self.recovery_info,
            "exit": {
                "path": exit_path,
                "clean": self.stop_signal is not None or exit_path in {"natural_completion", "already_complete"},
                "signal_received": self.stop_signal,
                "last_round": self.round_cursor,
                "report_guaranteed_after_sigkill": False,
            },
            "failures": failures,
            "injection_hits": injection_hits,
            "consistency": consistency,
        }
        report["report_id"] = sha256_json(
            {key: value for key, value in report.items() if key not in {"recovery", "exit"}}
        )[:24]
        return seal_report(report)

    def run(self) -> tuple[int, dict[str, Any]]:
        self._initialize()
        assert self.model is not None
        round_number = self.start_round
        while round_number <= self.config.total_rounds:
            if self.stop_event.is_set():
                break
            self._run_round(round_number)
            round_number += 1
            if self.round_delay_seconds:
                time.sleep(self.round_delay_seconds)
            if self.stop_event.is_set():
                break

        atomic_write_bytes(self.final_params_path, self.model.to_bytes())
        report = self._build_report()
        write_report(self.report_path, report)
        exit_path = report["exit"]["path"]
        self.state.write_run_state(
            {
                "schema_version": 1,
                "status": "clean_exit" if self.stop_signal is not None else "completed",
                "pid": os.getpid(),
                "last_round": self.round_cursor,
                "exit_path": exit_path,
                "report_path": str(self.report_path),
            }
        )
        self.state.write_progress(
            {
                "status": exit_path,
                "last_round": self.round_cursor,
                "round_cursor": self.round_cursor,
                "parameter_digest": self.model.digest,
                "checkpoint_generation": self.manifest.get("global_generation", 0),
            }
        )
        return 0, report
