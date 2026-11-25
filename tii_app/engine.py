"""Batch engine: cache-aware execution, checkpoints, and recovery."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .budget import Budgets, validate_budget
from .cache import ArtifactStore
from .configs import ConfigStore, ResolvedConfig
from .errors import EForcedTermination, ELedgerCorrupt
from .keys import stage_keys
from .ledger import VersionedLedger
from .models import Task, load_tasks
from .ops import build_model, forward_chain, inference_chain, train_backward_chain
from .sample_data import generate_sample, quantize_dequantize
from .scheduler import LogicalClock, build_batches
from .util import remove_tree, sha256_obj


STAGE_ORDER = (
    "sample",
    "quantize",
    "model",
    "forward",
    "train_backward",
    "inference",
)

MODE_STAGES = {
    "train": ("sample", "quantize", "model", "forward", "train_backward"),
    "infer": ("sample", "quantize", "model", "forward", "inference"),
    "both": (
        "sample",
        "quantize",
        "model",
        "forward",
        "train_backward",
        "inference",
    ),
}


@dataclass
class EngineResult:
    status: str
    mode: str
    tasks_total: int
    duplicate_tasks_ignored: int
    completed_before_run: int
    executed_now: int
    reused_stages: int
    recomputed_stages: int
    checkpoints_committed: int
    progress_version: int
    cache_version: int
    recovered_uncommitted_cache: bool = False
    forced_termination: dict[str, Any] | None = None
    invalidation: list[dict[str, Any]] = field(default_factory=list)
    removed_cases: list[str] = field(default_factory=list)
    batches: list[dict[str, Any]] = field(default_factory=list)
    output_dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "mode": self.mode,
            "tasks_total": self.tasks_total,
            "duplicate_tasks_ignored": self.duplicate_tasks_ignored,
            "completed_before_run": self.completed_before_run,
            "executed_now": self.executed_now,
            "reused_stages": self.reused_stages,
            "recomputed_stages": self.recomputed_stages,
            "checkpoints_committed": self.checkpoints_committed,
            "progress_version": self.progress_version,
            "cache_version": self.cache_version,
            "recovered_uncommitted_cache": self.recovered_uncommitted_cache,
            "forced_termination": self.forced_termination,
            "invalidation": self.invalidation,
            "removed_cases": self.removed_cases,
            "batches": self.batches,
            "output_dir": self.output_dir,
        }


class Engine:
    def __init__(
        self,
        input_dir: str | Path,
        output_dir: str | Path,
        *,
        mode: str = "both",
        global_seed: int = 42,
        budgets: Budgets | None = None,
        checkpoint_batch_tasks: int = 2,
        logical_window_ms: int = 1000,
        simulate_crash_after_tasks: int | None = None,
        simulate_crash_after_stages: int | None = None,
        simulate_crash_after_cache_commit: bool = False,
    ) -> None:
        if mode not in MODE_STAGES:
            raise ValueError(f"mode must be one of {sorted(MODE_STAGES)}")
        self.input_dir = Path(input_dir)
        self.output_dir = Path(output_dir)
        self.mode = mode
        self.global_seed = int(global_seed)
        self.budgets = budgets or Budgets()
        self.checkpoint_batch_tasks = int(checkpoint_batch_tasks)
        self.logical_window_ms = int(logical_window_ms)
        self.simulate_crash_after_tasks = simulate_crash_after_tasks
        self.simulate_crash_after_stages = simulate_crash_after_stages
        self.simulate_crash_after_cache_commit = simulate_crash_after_cache_commit
        self.ledger_dir = self.output_dir / "ledgers"
        self.cache_dir = self.output_dir / "stage_cache"
        self.report_dir = self.output_dir / "reports"
        self.task_ledger = VersionedLedger(self.ledger_dir, "tasks")
        self.config_ledger = VersionedLedger(self.ledger_dir, "configs")
        self.sample_ledger = VersionedLedger(self.ledger_dir, "samples")
        self.cache_ledger = VersionedLedger(self.ledger_dir, "stage_cache")
        self.progress_ledger = VersionedLedger(self.ledger_dir, "progress")
        self.artifact_store = ArtifactStore(self.cache_dir)
        self._stage_executions = 0
        self._tasks_executed = 0
        self._pending_dir: Path | None = None
        self._pending_records: list[dict[str, Any]] = []

    def run(self) -> EngineResult:
        for path in (self.ledger_dir, self.cache_dir, self.report_dir):
            path.mkdir(parents=True, exist_ok=True)
        ingest = load_tasks(self.input_dir)
        self._synchronize_task_ledger(ingest)
        config_store = ConfigStore(
            self.input_dir / "configs" / "config_v1.json",
            self.input_dir / "configs" / "v2",
        )
        self._synchronize_config_ledger(config_store)

        sample_snapshot = self.sample_ledger.read()
        cache_snapshot = self.cache_ledger.read()
        progress_snapshot = self.progress_ledger.read()
        sample_data = sample_snapshot["data"] if sample_snapshot else {
            "schema_version": 1,
            "samples": {},
        }
        cache_data = cache_snapshot["data"] if cache_snapshot else {
            "schema_version": 1,
            "index": {},
            "cases": {},
        }
        progress_data = progress_snapshot["data"] if progress_snapshot else {
            "schema_version": 1,
            "checkpoints": {},
        }

        sample_version = sample_snapshot["version"] if sample_snapshot else 0
        cache_version = cache_snapshot["version"] if cache_snapshot else 0
        cache_checksum = cache_snapshot["checksum"] if cache_snapshot else None
        checkpoint, progress_snapshot, recovered = self._ensure_mode_checkpoint(
            progress_data=progress_data,
            progress_snapshot=progress_snapshot,
            cache_version=cache_version,
            cache_checksum=cache_checksum,
            sample_version=sample_version,
        )
        progress_data = progress_snapshot["data"]

        if recovered or self._cache_ahead_of_checkpoints(progress_data, cache_data, sample_data):
            cache_data, sample_data, cache_version, sample_version = self._reconcile_uncommitted(
                progress_data=progress_data,
                cache_data=cache_data,
                sample_data=sample_data,
                cache_version=cache_version,
                sample_version=sample_version,
                cache_checksum=cache_checksum,
            )
            cache_snapshot = self.cache_ledger.read()
            sample_snapshot = self.sample_ledger.read()
            if cache_snapshot is not None:
                cache_checksum = cache_snapshot["checksum"]
            if sample_snapshot is not None:
                sample_checksum = sample_snapshot["checksum"]
            else:
                sample_checksum = None
            checkpoint = progress_data["checkpoints"][self.mode]
            checkpoint["cache_version"] = cache_version
            checkpoint["sample_version"] = sample_version
            if cache_snapshot is not None:
                checkpoint["cache_checksum"] = cache_snapshot["checksum"]
            if sample_snapshot is not None:
                checkpoint["sample_checksum"] = sample_checksum
            progress_snapshot = self._commit_progress(
                progress_data=progress_data,
                checkpoint=checkpoint,
                expected_version=progress_snapshot["version"],
            )
            recovered = True

        completed = checkpoint.get("completed", {})
        current_case_ids = {task.case_id for task in ingest.tasks}
        removed_cases = sorted(set(completed) - current_case_ids)
        if removed_cases:
            completed = {
                case_id: entry
                for case_id, entry in completed.items()
                if case_id in current_case_ids
            }
            checkpoint = self._make_checkpoint(
                version=progress_snapshot["version"] + 1,
                completed=completed,
                cache_version=cache_version,
                cache_checksum=cache_checksum,
                sample_version=sample_version,
                sample_checksum=(
                    self.sample_ledger.read()["checksum"]
                    if self.sample_ledger.read() is not None
                    else None
                ),
                batch_index=int(checkpoint.get("batch_index", -1)),
                last_clock_ms=int(checkpoint.get("last_logical_clock_ms", 0)),
                previous=checkpoint,
            )
            progress_data = copy.deepcopy(progress_data)
            progress_data["checkpoints"][self.mode] = checkpoint
            progress_snapshot = self.progress_ledger.commit(
                progress_data,
                expected_version=progress_snapshot["version"],
                expected_checksum=progress_snapshot["checksum"],
            )
        completed_before = len(completed)
        needs_replay_epoch = any(
            not self._task_is_current(
                task,
                config_store.resolve_at(task.logical_clock_ms),
                completed.get(task.case_id),
            )
            for task in ingest.tasks
        )
        clock = LogicalClock(
            0 if needs_replay_epoch else int(checkpoint.get("last_logical_clock_ms", 0)),
            window_ms=self.logical_window_ms,
        )
        batches = build_batches(
            ingest.tasks,
            max_tasks=self.checkpoint_batch_tasks,
            window_ms=self.logical_window_ms,
        )
        batch_results: list[dict[str, Any]] = []
        checkpoints_committed = 0
        forced: dict[str, Any] | None = None
        result = EngineResult(
            status="ok",
            mode=self.mode,
            tasks_total=len(ingest.tasks),
            duplicate_tasks_ignored=len(ingest.duplicates),
            completed_before_run=completed_before,
            executed_now=0,
            reused_stages=0,
            recomputed_stages=0,
            checkpoints_committed=0,
            progress_version=progress_snapshot["version"],
            cache_version=cache_version,
            recovered_uncommitted_cache=recovered,
            removed_cases=removed_cases,
            output_dir=str(self.output_dir),
        )

        try:
            for batch in batches:
                current_status = {
                    task.case_id: self._task_is_current(
                        task,
                        config_store.resolve_at(task.logical_clock_ms),
                        completed.get(task.case_id),
                    )
                    for task in batch.tasks
                }
                if all(current_status.values()):
                    batch_results.append(
                        {
                            "batch_index": batch.index,
                            "tasks": [task.case_id for task in batch.tasks],
                            "status": "already_committed",
                            "checkpoint": False,
                        }
                    )
                    continue

                self._pending_dir = self.artifact_store.new_pending_dir()
                self._pending_records = []
                batch_entries: dict[str, dict[str, Any]] = {}
                batch_samples: dict[str, dict[str, Any]] = {}
                available_index = dict(cache_data["index"])
                available_cases = dict(cache_data["cases"])
                available_samples = dict(sample_data["samples"])
                stage_counts = {"reused": 0, "recomputed": 0}
                for task in batch.tasks:
                    resolved = config_store.resolve_at(task.logical_clock_ms)
                    keys = stage_keys(task, resolved, self.global_seed)
                    if self._task_is_current(task, resolved, completed.get(task.case_id)):
                        continue
                    validate_budget(task, self.mode, self.budgets)
                    clock.advance_to(task.logical_clock_ms)
                    invalidated = self._invalidation_detail(
                        task,
                        resolved,
                        keys,
                        completed.get(task.case_id),
                    )
                    entry, task_counts, artifact_records = self._execute_task(
                        task=task,
                        config=resolved,
                        keys=keys,
                        available_index=available_index,
                        available_samples=available_samples,
                        pending_dir=self._pending_dir,
                    )
                    stage_counts["reused"] += task_counts["reused"]
                    stage_counts["recomputed"] += task_counts["recomputed"]
                    invalidated["actual_reused_stages"] = task_counts["reused"]
                    invalidated["actual_recomputed_stages"] = task_counts["recomputed"]
                    result.invalidation.append(invalidated)
                    for record in artifact_records:
                        available_index[record["key"]] = record
                        self._pending_records.append(record)
                    batch_entries[task.case_id] = entry
                    batch_samples[task.case_id] = entry["sample_summary"]
                    batch_results.append(
                        {
                            "batch_index": batch.index,
                            "tasks": [task.case_id],
                            "status": "staged",
                            "reused_stages": task_counts["reused"],
                            "recomputed_stages": task_counts["recomputed"],
                        }
                    )
                    result.executed_now += 1
                    self._tasks_executed += 1
                    if (
                        self.simulate_crash_after_tasks is not None
                        and self._tasks_executed >= self.simulate_crash_after_tasks
                    ):
                        forced = {
                            "reason": "simulated_after_task",
                            "task": task.case_id,
                            "batch": batch.index,
                            "uncommitted_batch_tasks": [item.case_id for item in batch.tasks],
                        }
                        raise EForcedTermination(
                            "simulated forced termination after task",
                            forced,
                        )

                if not batch_entries:
                    remove_tree(self._pending_dir)
                    self._pending_dir = None
                    continue

                finalized = self.artifact_store.finalize(self._pending_records)
                for record in finalized:
                    available_index[record["key"]] = record
                available_cases.update(
                    {
                        case_id: {
                            "case_id": case_id,
                            "keys": entry["keys"],
                            "task_content_hash": entry["task_content_hash"],
                        }
                        for case_id, entry in batch_entries.items()
                    }
                )
                new_samples = dict(sample_data["samples"])
                new_samples.update(
                    {case_id: summary for case_id, summary in batch_samples.items()}
                )
                new_cache_data = {
                    "schema_version": 1,
                    "index": available_index,
                    "cases": available_cases,
                    "cache_contract": "content-addressed-stage-v1",
                }
                new_sample_data = {
                    "schema_version": 1,
                    "samples": new_samples,
                    "cache_contract": "deterministic-sample-v1",
                }

                sample_commit = self.sample_ledger.commit(
                    new_sample_data,
                    expected_version=sample_version if sample_snapshot else None,
                    expected_checksum=sample_snapshot["checksum"] if sample_snapshot else None,
                )
                sample_version = sample_commit["version"]
                sample_snapshot = sample_commit
                cache_commit = self.cache_ledger.commit(
                    new_cache_data,
                    expected_version=cache_version if cache_snapshot else None,
                    expected_checksum=cache_snapshot["checksum"] if cache_snapshot else None,
                )
                cache_version = cache_commit["version"]
                cache_checksum = cache_commit["checksum"]
                cache_snapshot = cache_commit
                cache_data = new_cache_data
                sample_data = new_sample_data
                if self.simulate_crash_after_cache_commit:
                    forced = {
                        "reason": "simulated_after_cache_commit",
                        "batch": batch.index,
                        "committed_cache_version": cache_version,
                        "uncommitted_progress_tasks": list(batch_entries),
                    }
                    raise EForcedTermination(
                        "simulated forced termination after cache ledger commit",
                        forced,
                    )

                completed.update(batch_entries)
                last_clock_ms = max(
                    int(entry["logical_clock_ms"])
                    for entry in completed.values()
                )
                checkpoint = self._make_checkpoint(
                    version=progress_snapshot["version"] + 1,
                    completed=completed,
                    cache_version=cache_version,
                    cache_checksum=cache_checksum,
                    sample_version=sample_version,
                    sample_checksum=sample_commit["checksum"],
                    batch_index=batch.index,
                    last_clock_ms=last_clock_ms,
                    previous=checkpoint,
                )
                progress_data = copy.deepcopy(progress_data)
                progress_data["checkpoints"][self.mode] = checkpoint
                progress_snapshot = self.progress_ledger.commit(
                    progress_data,
                    expected_version=progress_snapshot["version"],
                    expected_checksum=progress_snapshot["checksum"],
                )
                checkpoints_committed += 1
                batch_results.append(
                    {
                        "batch_index": batch.index,
                        "tasks": [task.case_id for task in batch.tasks],
                        "status": "committed",
                        "checkpoint": True,
                        "checkpoint_version": checkpoint["version"],
                        "logical_window": [
                            batch.logical_window_start,
                            batch.logical_window_end,
                        ],
                        "reused_stages": stage_counts["reused"],
                        "recomputed_stages": stage_counts["recomputed"],
                    }
                )
                result.reused_stages += stage_counts["reused"]
                result.recomputed_stages += stage_counts["recomputed"]
                remove_tree(self._pending_dir)
                self._pending_dir = None
                self._pending_records = []
        except EForcedTermination as exc:
            forced = exc.details
            result.status = "forced_termination"
            result.forced_termination = forced
            result.batches = batch_results
            result.checkpoints_committed = checkpoints_committed
            result.progress_version = progress_snapshot["version"]
            result.cache_version = cache_version
        finally:
            if self._pending_dir is not None:
                remove_tree(self._pending_dir)
                self._pending_dir = None
                self._pending_records = []

        result.checkpoints_committed = checkpoints_committed
        result.progress_version = progress_snapshot["version"]
        result.cache_version = cache_version
        result.batches = batch_results
        if result.status == "ok":
            result.status = "completed"
        return result

    def _task_is_current(
        self,
        task: Task,
        config: ResolvedConfig,
        entry: dict[str, Any] | None,
    ) -> bool:
        if entry is None:
            return False
        if (
            entry.get("task_content_hash") != task.content_hash
            or entry.get("config_generation") != config.generation
            or entry.get("config_hash") != config.config_hash
        ):
            return False
        required = MODE_STAGES[self.mode]
        entry_keys = entry.get("keys", {})
        current_keys = stage_keys(task, config, self.global_seed)
        return all(entry_keys.get(stage) == current_keys[stage] for stage in required)

    def _invalidation_detail(
        self,
        task: Task,
        config: ResolvedConfig,
        keys: dict[str, str],
        entry: dict[str, Any] | None,
    ) -> dict[str, Any]:
        required = MODE_STAGES[self.mode]
        if entry is None:
            return {
                "case_id": task.case_id,
                "classification": "new",
                "reused_stages": [],
                "recomputed_stages": list(required),
            }
        data_changed = entry.get("task_content_hash") != task.content_hash
        config_changed = (
            entry.get("config_generation") != config.generation
            or entry.get("config_hash") != config.config_hash
        )
        if data_changed and config_changed:
            classification = "both"
        elif config_changed:
            classification = "config_only"
        elif data_changed:
            classification = "data_only"
        else:
            classification = "mode_extension"
        old_keys = entry.get("keys", {})
        reused = [stage for stage in required if old_keys.get(stage) == keys[stage]]
        recomputed = [stage for stage in required if old_keys.get(stage) != keys[stage]]
        return {
            "case_id": task.case_id,
            "classification": classification,
            "reused_stages": reused,
            "recomputed_stages": recomputed,
        }

    def _execute_task(
        self,
        *,
        task: Task,
        config: ResolvedConfig,
        keys: dict[str, str],
        available_index: dict[str, dict[str, Any]],
        available_samples: dict[str, dict[str, Any]],
        pending_dir: Path,
    ) -> tuple[dict[str, Any], dict[str, int], list[dict[str, Any]]]:
        counts = {"reused": 0, "recomputed": 0}
        artifacts: dict[str, dict[str, np.ndarray]] = {}
        metadata: dict[str, dict[str, Any]] = {}
        pending_records: list[dict[str, Any]] = []
        needed = MODE_STAGES[self.mode]

        def ensure(stage: str, builder: Any) -> None:
            key = keys[stage]
            record = available_index.get(key)
            if record is not None:
                try:
                    arrays = self.artifact_store.load(record)
                except ELedgerCorrupt:
                    record = None
                if record is not None:
                    artifacts[stage] = arrays
                    metadata[stage] = dict(record.get("metadata", {}))
                    counts["reused"] += 1
                    return
            built = builder()
            arrays = {name: value for name, value in built.items() if isinstance(value, np.ndarray)}
            summary = {
                name: value
                for name, value in built.items()
                if not isinstance(value, np.ndarray)
            }
            record = self.artifact_store.save_pending(
                pending_dir,
                key,
                stage,
                arrays,
                summary,
            )
            artifacts[stage] = arrays
            metadata[stage] = summary
            available_index[key] = record
            pending_records.append(record)
            counts["recomputed"] += 1
            self._stage_executions += 1
            if (
                self.simulate_crash_after_stages is not None
                and self._stage_executions >= self.simulate_crash_after_stages
            ):
                raise EForcedTermination(
                    "simulated forced termination after stage",
                    {
                        "reason": "simulated_after_stage",
                        "task": task.case_id,
                        "stage": stage,
                        "key": key,
                    },
                )

        ensure(
            "sample",
            lambda: self._build_sample(task, keys["sample"]),
        )
        sample_arrays = artifacts["sample"]
        sample_summary = metadata["sample"]["summary"]
        ensure(
            "quantize",
            lambda: self._build_quantize(task, sample_arrays),
        )
        ensure(
            "model",
            lambda: self._build_model_stage(task, config),
        )
        ensure(
            "forward",
            lambda: self._build_forward(
                task,
                config,
                artifacts["quantize"]["dequantized"],
                artifacts["model"],
            ),
        )
        if "train_backward" in needed:
            ensure(
                "train_backward",
                lambda: self._build_train_suffix(
                    task,
                    config,
                    artifacts["forward"],
                    artifacts["sample"],
                    artifacts["quantize"],
                    metadata["forward"]["summary"]["forward_digest"],
                ),
            )
        if "inference" in needed:
            ensure(
                "inference",
                lambda: self._build_inference_suffix(
                    task,
                    config,
                    artifacts["forward"],
                    metadata["forward"]["summary"]["forward_digest"],
                ),
            )

        entry = {
            "case_id": task.case_id,
            "task_content_hash": task.content_hash,
            "sample_key": keys["sample"],
            "data_digest": sample_summary["data_digest"],
            "sample_summary": sample_summary,
            "quant_summary": metadata["quantize"]["summary"],
            "forward_summary": metadata["forward"]["summary"],
            "config_generation": config.generation,
            "config_hash": config.config_hash,
            "logical_clock_ms": task.logical_clock_ms,
            "priority": task.priority,
            "mode": self.mode,
            "keys": dict(keys),
            "stages": list(needed),
            "train_summary": metadata.get("train_backward", {}).get("summary"),
            "inference_summary": metadata.get("inference", {}).get("summary"),
            "content_checksum": sha256_obj(
                {
                    "case_id": task.case_id,
                    "task_content_hash": task.content_hash,
                    "keys": keys,
                    "config_generation": config.generation,
                    "config_hash": config.config_hash,
                }
            ),
        }
        return entry, counts, pending_records

    def _build_sample(self, task: Task, cache_key: str) -> dict[str, Any]:
        generated = generate_sample(task, self.global_seed)
        return {
            "features": generated["features"],
            "labels": generated["labels"],
            "label_ids": generated["label_ids"],
            "summary": {
                **generated["summary"],
                "cache_key": cache_key,
                "sample_seed": generated["sample_seed"],
            },
        }

    def _build_quantize(self, task: Task, sample_arrays: dict[str, np.ndarray]) -> dict[str, Any]:
        quantized = quantize_dequantize(sample_arrays["features"], task)
        return {
            "quantized": quantized["quantized"],
            "dequantized": quantized["dequantized"],
            "summary": {
                **quantized["summary"],
                "task_content_hash": task.content_hash,
            },
        }

    def _build_model_stage(self, task: Task, config: ResolvedConfig) -> dict[str, Any]:
        model = build_model(task, config)
        return {
            "weights": model["weights"],
            "bias": model["bias"],
            "summary": model["summary"],
        }

    def _build_forward(
        self,
        task: Task,
        config: ResolvedConfig,
        dequantized: np.ndarray,
        model_arrays: dict[str, np.ndarray],
    ) -> dict[str, Any]:
        model = {"weights": model_arrays["weights"], "bias": model_arrays["bias"]}
        forward = forward_chain(dequantized, task, model, config)
        return {
            "logits": forward["logits"],
            "activations": forward["activations"],
            "summary": {
                **forward["summary"],
                "row_boundaries": forward["row_boundaries"],
                "col_boundaries": forward["col_boundaries"],
            },
        }

    def _build_train_suffix(
        self,
        task: Task,
        config: ResolvedConfig,
        forward_arrays: dict[str, np.ndarray],
        sample_arrays: dict[str, np.ndarray],
        quant_arrays: dict[str, np.ndarray],
        forward_digest: str,
    ) -> dict[str, Any]:
        forward = {
            "activations": forward_arrays["activations"],
            "summary": {"forward_digest": forward_digest},
        }
        sample = {
            "labels": sample_arrays["labels"],
            "dequantized": quant_arrays["dequantized"],
        }
        # The digest is carried by the forward metadata in cache; this summary
        # only needs to be stable for the gradient artifact itself.
        train = train_backward_chain(forward, sample, task, config)
        return {
            "grad_weights": train["grad_weights"],
            "grad_bias": train["grad_bias"],
            "summary": train["summary"],
        }

    def _build_inference_suffix(
        self,
        task: Task,
        config: ResolvedConfig,
        forward_arrays: dict[str, np.ndarray],
        forward_digest: str,
    ) -> dict[str, Any]:
        forward = {
            "activations": forward_arrays["activations"],
            "summary": {"forward_digest": forward_digest},
        }
        inference = inference_chain(forward, task, config)
        return {
            "predictions": inference["predictions"],
            "probabilities": inference["probabilities"],
            "confidence": inference["confidence"],
            "summary": inference["summary"],
        }

    def _synchronize_task_ledger(self, ingest: Any) -> dict[str, Any]:
        identity = f"tasks:{ingest.source_version}:{ingest.source_checksum}"
        data = {
            "schema_version": 1,
            "source_identity": identity,
            "source_version": ingest.source_version,
            "source_checksum": ingest.source_checksum,
            "tasks": [task.to_dict() for task in ingest.tasks],
            "task_content_hashes": {task.case_id: task.content_hash for task in ingest.tasks},
            "duplicates": ingest.duplicates,
            "accounting_count": ingest.accounting_count,
            "version_monotonic_rule": "full_snapshot_replace",
            "checksum_rule": "sha256_canonical_snapshot",
        }
        snapshot, changed = self.task_ledger.replace_if_changed(data, identity=identity)
        if changed:
            data["source_version"] = snapshot["version"]
        return snapshot

    def _synchronize_config_ledger(self, config_store: ConfigStore) -> dict[str, Any]:
        identity = f"configs:{config_store.snapshot_identity()}"
        active = config_store.active_configuration_at_end()
        data = {
            "schema_version": 1,
            "source_identity": identity,
            "snapshot": config_store.config_snapshot(),
            "active_configuration": {
                "generation": active.generation,
                "effective_at_ms": active.effective_at_ms,
                "config_hash": active.config_hash,
                "source": active.source,
            },
            "single_active_generation_rule": "latest generation effective at a logical clock",
            "version_monotonic_rule": "full_snapshot_replace",
        }
        snapshot, _ = self.config_ledger.replace_if_changed(data, identity=identity)
        return snapshot

    def _ensure_mode_checkpoint(
        self,
        *,
        progress_data: dict[str, Any],
        progress_snapshot: dict[str, Any] | None,
        cache_version: int,
        cache_checksum: str | None,
        sample_version: int,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        checkpoints = progress_data.setdefault("checkpoints", {})
        if self.mode in checkpoints:
            checkpoint = checkpoints[self.mode]
            content = {
                "version": checkpoint["version"],
                "completed": checkpoint["completed"],
            }
            if checkpoint.get("content_checksum") != sha256_obj(content):
                raise ELedgerCorrupt(
                    "checkpoint content checksum mismatch",
                    {"mode": self.mode},
                )
            return checkpoint, progress_snapshot, False

        new_version = (progress_snapshot["version"] + 1) if progress_snapshot else 1
        checkpoint = self._make_checkpoint(
            version=new_version,
            completed={},
            cache_version=cache_version,
            cache_checksum=cache_checksum,
            sample_version=sample_version,
            sample_checksum=None,
            batch_index=-1,
            last_clock_ms=0,
            previous=None,
        )
        checkpoints[self.mode] = checkpoint
        progress_snapshot = self.progress_ledger.commit(
            progress_data,
            expected_version=progress_snapshot["version"] if progress_snapshot else None,
            expected_checksum=progress_snapshot["checksum"] if progress_snapshot else None,
        )
        return checkpoint, progress_snapshot, False

    @staticmethod
    def _make_checkpoint(
        *,
        version: int,
        completed: dict[str, Any],
        cache_version: int,
        cache_checksum: str | None,
        sample_version: int,
        sample_checksum: str | None,
        batch_index: int,
        last_clock_ms: int,
        previous: dict[str, Any] | None,
    ) -> dict[str, Any]:
        entries = dict(completed)
        content_checksum = sha256_obj({"version": version, "completed": entries})
        return {
            "version": version,
            "completed_case_ids": sorted(entries),
            "completed": entries,
            "content_checksum": content_checksum,
            "batch_index": batch_index,
            "last_logical_clock_ms": last_clock_ms,
            "cache_version": cache_version,
            "cache_checksum": cache_checksum,
            "sample_version": sample_version,
            "sample_checksum": sample_checksum,
            "previous_checkpoint_version": (
                int(previous["version"]) if previous is not None else None
            ),
            "commit_rule": "all_case_stages_then_atomic_checkpoint_replace",
        }

    @staticmethod
    def _cache_ahead_of_checkpoints(
        progress_data: dict[str, Any],
        cache_data: dict[str, Any],
        sample_data: dict[str, Any],
    ) -> bool:
        checkpoints = progress_data.get("checkpoints", {})
        if not checkpoints:
            return bool(cache_data.get("index") or sample_data.get("samples"))
        completed_keys: set[str] = set()
        completed_cases: set[str] = set()
        for checkpoint in checkpoints.values():
            for case_id, entry in checkpoint.get("completed", {}).items():
                completed_cases.add(case_id)
                completed_keys.update(entry.get("keys", {}).values())
        cache_extra = set(cache_data.get("index", {})) - completed_keys
        sample_extra = set(sample_data.get("samples", {})) - completed_cases
        return bool(cache_extra or sample_extra)

    def _reconcile_uncommitted(
        self,
        *,
        progress_data: dict[str, Any],
        cache_data: dict[str, Any],
        sample_data: dict[str, Any],
        cache_version: int,
        sample_version: int,
        cache_checksum: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any], int, int]:
        completed_keys: set[str] = set()
        completed_cases: set[str] = set()
        for checkpoint in progress_data.get("checkpoints", {}).values():
            for case_id, entry in checkpoint.get("completed", {}).items():
                completed_cases.add(case_id)
                completed_keys.update(entry.get("keys", {}).values())
        filtered_index = {
            key: record
            for key, record in cache_data.get("index", {}).items()
            if key in completed_keys
        }
        filtered_cases = {
            case_id: entry
            for case_id, entry in cache_data.get("cases", {}).items()
            if case_id in completed_cases
        }
        filtered_samples = {
            case_id: entry
            for case_id, entry in sample_data.get("samples", {}).items()
            if case_id in completed_cases
        }
        cache_data = {
            "schema_version": 1,
            "index": filtered_index,
            "cases": filtered_cases,
            "cache_contract": "content-addressed-stage-v1",
            "recovery_note": "discarded artifacts not referenced by a committed checkpoint",
        }
        sample_data = {
            "schema_version": 1,
            "samples": filtered_samples,
            "cache_contract": "deterministic-sample-v1",
            "recovery_note": "discarded samples not referenced by a committed checkpoint",
        }
        current_cache = self.cache_ledger.read()
        if current_cache is not None:
            cache_snapshot = self.cache_ledger.commit(
                cache_data,
                expected_version=current_cache["version"],
                expected_checksum=current_cache["checksum"],
            )
            cache_version = cache_snapshot["version"]
        elif cache_data["index"]:
            cache_snapshot = self.cache_ledger.commit(
                cache_data,
                expected_version=None,
                expected_checksum=None,
            )
            cache_version = cache_snapshot["version"]
        current_samples = self.sample_ledger.read()
        if current_samples is not None:
            sample_snapshot = self.sample_ledger.commit(
                sample_data,
                expected_version=current_samples["version"],
                expected_checksum=current_samples["checksum"],
            )
            sample_version = sample_snapshot["version"]
        elif sample_data["samples"]:
            sample_snapshot = self.sample_ledger.commit(
                sample_data,
                expected_version=None,
                expected_checksum=None,
            )
            sample_version = sample_snapshot["version"]
        return cache_data, sample_data, cache_version, sample_version

    def _commit_progress(
        self,
        *,
        progress_data: dict[str, Any],
        checkpoint: dict[str, Any],
        expected_version: int,
    ) -> dict[str, Any]:
        progress_data = copy.deepcopy(progress_data)
        progress_data["checkpoints"][self.mode] = checkpoint
        return self.progress_ledger.commit(
            progress_data,
            expected_version=expected_version,
            expected_checksum=self.progress_ledger.read()["checksum"],
        )
