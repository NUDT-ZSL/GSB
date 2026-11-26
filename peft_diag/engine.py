"""Four-stage deterministic training orchestration, caching, checkpoints and recovery."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .cache import ContentCache
from .canonical import atomic_write_bytes, canonical_bytes, content_hash, read_json
from .catalog import Catalog
from .checkpoint import CheckpointManager
from .config import DegradationLog, EngineConfig
from .errors import (
    BusinessError,
    corrupt,
    stale_result,
)
from .snapshots import Snapshot, estimate_task_snapshot_bytes, load_snapshot
from .tasks import NormalizedTask, parse_tasks
from .training import (
    BETA1,
    BETA2,
    TrainingState,
    accumulate_micro_step,
    apply_optimizer_step,
    final_task_summaries,
    initialize_task,
    state_digest,
    state_from_dict,
    state_to_dict,
)


STAGES = ("INITIALIZE", "GRAD_ACCUM", "OPTIMIZER_UPDATE", "EXPORT")


@dataclass
class TaskRuntime:
    task: NormalizedTask
    snapshot: Snapshot
    snapshot_path: str
    stage_stats: dict[str, dict[str, int]] = field(default_factory=dict)

    def record(self, stage: str, outcome: str) -> None:
        stats = self.stage_stats.setdefault(stage, {"computed": 0, "cache_hits": 0})
        if outcome == "cache_hit":
            stats["cache_hits"] += 1
        else:
            stats["computed"] += 1


KillHook = Callable[[str, int, int, str], None]
InterruptCheck = Callable[[], bool]


class DiagnosticEngine:
    def __init__(self, config: EngineConfig) -> None:
        config.validate()
        self.config = config
        self.state_dir = Path(config.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.tasks = parse_tasks(config.tasks_path)
        self.catalog = Catalog(config.catalog_path)
        self.degradation = DegradationLog()
        self.cache = ContentCache(
            self.state_dir / "cache",
            max_entries=config.limits.max_cache_entries,
            degradation=self.degradation,
        )
        self.checkpoints = CheckpointManager(self.state_dir, config.run_id)
        self.runtimes: dict[str, TaskRuntime] = {}
        self.states: dict[str, TrainingState] = {}
        self.cache_keys: dict[str, dict[str, Any]] = {}
        self._pending_cache: list[dict[str, Any]] = []
        self._previous_report: dict[str, Any] | None = None
        report_path = self.state_dir / "reports" / f"{config.run_id}.json"
        if report_path.is_file():
            try:
                loaded_report = read_json(report_path)
                if isinstance(loaded_report, dict):
                    self._previous_report = loaded_report
            except Exception:
                self._previous_report = None
        self._load_snapshots()
        self.max_optimizer_steps = max(task.optimizer_steps for task in self.tasks)
        self.tasks_fingerprint = content_hash(
            {
                task.case_id: task.fingerprint
                for task in self.tasks
            }
        )
        self.config_hash = content_hash(
            {
                "algorithm_version": config.algorithm_version,
                "snapshot_format": config.snapshot_format,
                "config_generation": self.catalog.config_generation,
                "tasks": self.tasks_fingerprint,
                "snapshots": {
                    task.case_id: self.runtimes[task.case_id].snapshot.semantic_hash
                    for task in self.tasks
                },
                "limits": config.limits.as_dict(),
            }
        )
        self.report: dict[str, Any] = {}

    def _load_snapshots(self) -> None:
        for task in self.tasks:
            path = self.catalog.snapshot_path(task.case_id, self.config.snapshot_format)
            snapshot = load_snapshot(
                path,
                task,
                limits=self.config.limits,
                degradation=self.degradation,
            )
            catalog_entry = self.catalog.entry(task.case_id)
            expected_hash = catalog_entry.get("semantic_hash")
            if isinstance(expected_hash, str) and snapshot.semantic_hash != expected_hash:
                raise stale_result(
                    "snapshot content differs from catalog dependency hash",
                    task_id=task.case_id,
                    field="snapshot_version.semantic_hash",
                    details={
                        "catalog": expected_hash,
                        "actual": snapshot.semantic_hash,
                        "path": path,
                    },
                )
            estimate = estimate_task_snapshot_bytes(task)
            if estimate > self.config.limits.max_task_memory_bytes:
                self.degradation.record(
                    reason="single_task_memory_limit_exceeded",
                    priority=1,
                    action="spill_snapshot_and_state_to_disk",
                    task_id=task.case_id,
                    details={
                        "estimated_bytes": estimate,
                        "limit": self.config.limits.max_task_memory_bytes,
                    },
                )
            self.runtimes[task.case_id] = TaskRuntime(
                task=task,
                snapshot=snapshot,
                snapshot_path=str(path),
            )

    def _init_key(self, task: NormalizedTask) -> str:
        runtime = self.runtimes[task.case_id]
        return content_hash(
            {
                "stage": "INITIALIZE",
                "algorithm_version": self.config.algorithm_version,
                "task_id": task.case_id,
                "base_model_rev": task.base_model_rev,
                "adapter_type": task.adapter_type,
                "adapter_dim": task.adapter_dim,
                "num_layers": task.num_layers,
                "seed": task.seed,
                "revision": task.revision,
                "snapshot_semantic_hash": runtime.snapshot.semantic_hash,
                "structure_summary": task.parameter_summary,
            }
        )

    def _grad_key(
        self,
        task: NormalizedTask,
        update_cycle: int,
        previous_update_key: str,
    ) -> str:
        return content_hash(
            {
                "stage": "GRAD_ACCUM",
                "algorithm_version": self.config.algorithm_version,
                "task_id": task.case_id,
                "seed": task.seed,
                "micro_batch": task.micro_batch,
                "update_cycle": update_cycle,
                "previous_update_key": previous_update_key,
            }
        )

    def _update_key(
        self,
        task: NormalizedTask,
        update_cycle: int,
        grad_key: str,
    ) -> str:
        return content_hash(
            {
                "stage": "OPTIMIZER_UPDATE",
                "algorithm_version": self.config.algorithm_version,
                "task_id": task.case_id,
                "lr": task.lr,
                "beta1": BETA1,
                "beta2": BETA2,
                "update_cycle": update_cycle,
                "gradient_key": grad_key,
            }
        )

    def _export_key(self, task: NormalizedTask, final_update_key: str) -> str:
        return content_hash(
            {
                "stage": "EXPORT",
                "algorithm_version": self.config.algorithm_version,
                "task_id": task.case_id,
                "update_key": final_update_key,
                "steps": task.steps,
                "micro_batch": task.micro_batch,
                "revision": task.revision,
                "snapshot_semantic_hash": self.runtimes[task.case_id].snapshot.semantic_hash,
            }
        )

    def _task_cache_keys(self, task: NormalizedTask) -> dict[str, Any]:
        return self.cache_keys[task.case_id]

    def _init_keys(self) -> None:
        for task in self.tasks:
            self.cache_keys[task.case_id] = {
                "INIT": self._init_key(task),
                "GRAD": {},
                "UPDATE": {},
                "EXPORT": None,
            }

    def _maybe_resource_log(self, task: NormalizedTask, state: TrainingState, stage: str) -> None:
        if state.resident_bytes > self.config.limits.max_task_memory_bytes:
            current_marker = f"{task.case_id}:{stage}"
            if getattr(self, "_resource_markers", None) is None:
                self._resource_markers = set()
            if current_marker not in self._resource_markers:
                self._resource_markers.add(current_marker)
                self.degradation.record(
                    reason="single_task_resident_memory_limit_exceeded",
                    priority=1,
                    action="spill_stage_state_to_content_cache_and_checkpoint",
                    task_id=task.case_id,
                    details={
                        "stage": stage,
                        "resident_bytes": state.resident_bytes,
                        "limit": self.config.limits.max_task_memory_bytes,
                    },
                )

    def _cache_state_payload(
        self,
        *,
        task: NormalizedTask,
        stage: str,
        state: TrainingState,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "task_id": task.case_id,
            "stage": stage,
            "structure_summary": task.parameter_summary,
            "snapshot_semantic_hash": self.runtimes[task.case_id].snapshot.semantic_hash,
            "state": state_to_dict(state),
        }
        if extra:
            payload.update(extra)
        return payload

    def _queue_cache(
        self,
        key: str,
        payload: dict[str, Any],
        *,
        task_id: str,
        stage: str,
    ) -> None:
        """Only publish stage cache entries after the covering checkpoint commits."""
        self._pending_cache.append(
            {
                "key": key,
                "payload": payload,
                "task_id": task_id,
                "stage": stage,
            }
        )

    def _flush_pending_cache(self) -> None:
        for item in self._pending_cache:
            self.cache.put(
                item["key"],
                item["payload"],
                task_id=item["task_id"],
                stage=item["stage"],
            )
        self._pending_cache = []

    def _state_from_cache(
        self,
        payload: dict[str, Any] | None,
        *,
        task: NormalizedTask,
        stage: str,
    ) -> TrainingState | None:
        if payload is None:
            return None
        try:
            if payload.get("task_id") != task.case_id:
                return None
            if payload.get("stage") != stage:
                return None
            if payload.get("structure_summary") != task.parameter_summary:
                return None
            if payload.get("snapshot_semantic_hash") != self.runtimes[task.case_id].snapshot.semantic_hash:
                return None
            return state_from_dict(payload["state"])
        except Exception:
            return None

    def _stage_initialize(self, kill_hook: KillHook | None) -> None:
        stage = "INITIALIZE"
        self._init_keys()
        for index, task in enumerate(self.tasks):
            if kill_hook:
                kill_hook(stage, 0, index, task.case_id)
            runtime = self.runtimes[task.case_id]
            key = self.cache_keys[task.case_id]["INIT"]
            payload = self.cache.get(key)
            state = self._state_from_cache(payload, task=task, stage=stage)
            if state is None:
                state = initialize_task(task, runtime.snapshot)
                self._queue_cache(
                    key,
                    self._cache_state_payload(task=task, stage=stage, state=state),
                    task_id=task.case_id,
                    stage=stage,
                )
                runtime.record(stage, "computed")
            else:
                runtime.record(stage, "cache_hit")
            self._maybe_resource_log(task, state, stage)
            self.states[task.case_id] = state

    def _stage_grad_accum(self, cycle: int, kill_hook: KillHook | None) -> None:
        stage = "GRAD_ACCUM"
        for index, task in enumerate(self.tasks):
            if kill_hook:
                kill_hook(stage, cycle, index, task.case_id)
            if cycle > task.optimizer_steps:
                continue
            previous_key = (
                self.cache_keys[task.case_id]["UPDATE"].get(str(cycle - 1))
                if cycle > 1
                else self.cache_keys[task.case_id]["INIT"]
            )
            grad_key = self._grad_key(task, cycle, previous_key)
            self.cache_keys[task.case_id]["GRAD"][str(cycle)] = grad_key
            payload = self.cache.get(grad_key)
            state = self._state_from_cache(payload, task=task, stage=stage)
            if state is None:
                state = self.states[task.case_id].clone()
                expected_step = (cycle - 1) * task.micro_batch
                if state.step != expected_step or state.micro_in_window != 0:
                    raise corrupt(
                        "training state is not at the expected gradient window boundary",
                        task_id=task.case_id,
                        field="step",
                        details={
                            "expected_step": expected_step,
                            "actual_step": state.step,
                            "micro_in_window": state.micro_in_window,
                            "cycle": cycle,
                        },
                    )
                for _ in range(task.micro_batch):
                    state = accumulate_micro_step(task, state)
                if state.micro_in_window != task.micro_batch:
                    raise corrupt(
                        "gradient accumulation window did not close",
                        task_id=task.case_id,
                        field="micro_batch",
                    )
                self._queue_cache(
                    grad_key,
                    self._cache_state_payload(task=task, stage=stage, state=state),
                    task_id=task.case_id,
                    stage=stage,
                )
                self.runtimes[task.case_id].record(stage, "computed")
            else:
                self.runtimes[task.case_id].record(stage, "cache_hit")
            self._maybe_resource_log(task, state, f"{stage}:{cycle}")
            self.states[task.case_id] = state

    def _stage_optimizer_update(self, cycle: int, kill_hook: KillHook | None) -> None:
        stage = "OPTIMIZER_UPDATE"
        for index, task in enumerate(self.tasks):
            if kill_hook:
                kill_hook(stage, cycle, index, task.case_id)
            if cycle > task.optimizer_steps:
                continue
            grad_key = self.cache_keys[task.case_id]["GRAD"].get(str(cycle))
            if not grad_key:
                raise corrupt(
                    "gradient stage cache key is missing before optimizer update",
                    task_id=task.case_id,
                    field="cache_keys",
                    details={"cycle": cycle},
                )
            update_key = self._update_key(task, cycle, grad_key)
            self.cache_keys[task.case_id]["UPDATE"][str(cycle)] = update_key
            payload = self.cache.get(update_key)
            state = self._state_from_cache(payload, task=task, stage=stage)
            if state is None:
                state = self.states[task.case_id].clone()
                expected_step = cycle * task.micro_batch
                if state.step != expected_step or state.micro_in_window != task.micro_batch:
                    raise corrupt(
                        "training state is not at a complete optimizer window",
                        task_id=task.case_id,
                        field="step",
                        details={
                            "expected_step": expected_step,
                            "actual_step": state.step,
                            "micro_in_window": state.micro_in_window,
                            "cycle": cycle,
                        },
                    )
                state = apply_optimizer_step(task, state)
                self._queue_cache(
                    update_key,
                    self._cache_state_payload(task=task, stage=stage, state=state),
                    task_id=task.case_id,
                    stage=stage,
                )
                self.runtimes[task.case_id].record(stage, "computed")
            else:
                self.runtimes[task.case_id].record(stage, "cache_hit")
            self._maybe_resource_log(task, state, f"{stage}:{cycle}")
            self.states[task.case_id] = state

    def _trace_for_task(
        self,
        task: NormalizedTask,
        state: TrainingState,
        export_key: str,
    ) -> dict[str, Any]:
        runtime = self.runtimes[task.case_id]
        keys = self.cache_keys[task.case_id]
        return {
            "snapshot_version": {
                "snapshot_id": runtime.snapshot.snapshot_id,
                "semantic_hash": runtime.snapshot.semantic_hash,
                "format": runtime.snapshot.format_version,
                "path": runtime.snapshot_path,
                "generation": runtime.snapshot.generation,
                "revision": runtime.snapshot.revision,
            },
            "config_generation": self.catalog.config_generation,
            "param_revision": task.revision,
            "cache_keys": {
                "INIT": keys["INIT"],
                "GRAD": dict(keys["GRAD"]),
                "UPDATE": dict(keys["UPDATE"]),
                "EXPORT": export_key,
            },
            "steps": task.steps,
            "micro_batch": task.micro_batch,
            "optimizer_steps": task.optimizer_steps,
            "seed": task.seed,
            "lr": task.lr,
            "algorithm_version": self.config.algorithm_version,
        }

    def _build_artifact(self, task: NormalizedTask, state: TrainingState, export_key: str) -> dict[str, Any]:
        summaries = final_task_summaries(state)
        artifact = {
            "schema_version": "task-artifact-v1",
            "ok": True,
            "task_id": task.case_id,
            "status": "EXPORTED",
            "trace": self._trace_for_task(task, state, export_key),
            **summaries,
            "final_state_digest": state_digest(state),
            "step_records": state.records,
            "record_count": len(state.records),
        }
        artifact["artifact_checksum"] = content_hash(artifact)
        return artifact

    def _stage_export(self, kill_hook: KillHook | None) -> dict[str, dict[str, Any]]:
        stage = "EXPORT"
        artifacts: dict[str, dict[str, Any]] = {}
        artifact_root = self.state_dir / "artifacts" / self.config.run_id
        artifact_root.mkdir(parents=True, exist_ok=True)
        for index, task in enumerate(self.tasks):
            if kill_hook:
                kill_hook(stage, self.max_optimizer_steps + 1, index, task.case_id)
            final_update_key = self.cache_keys[task.case_id]["UPDATE"].get(
                str(task.optimizer_steps)
            )
            if not final_update_key:
                raise corrupt(
                    "final optimizer cache key is missing",
                    task_id=task.case_id,
                    field="cache_keys",
                )
            export_key = self._export_key(task, final_update_key)
            self.cache_keys[task.case_id]["EXPORT"] = export_key
            cached = self.cache.get(export_key)
            artifact: dict[str, Any] | None = None
            if cached is not None:
                try:
                    candidate = copy.deepcopy(cached["artifact"])
                    if candidate.get("task_id") != task.case_id:
                        candidate = None
                    if candidate is not None:
                        if (
                            candidate["trace"]["snapshot_version"]["semantic_hash"]
                            != self.runtimes[task.case_id].snapshot.semantic_hash
                        ):
                            candidate = None
                    if candidate is not None:
                        candidate["trace"]["config_generation"] = self.catalog.config_generation
                        candidate["artifact_checksum"] = content_hash(
                            {key: value for key, value in candidate.items() if key != "artifact_checksum"}
                        )
                        artifact = candidate
                        self.runtimes[task.case_id].record(stage, "cache_hit")
                except Exception:
                    artifact = None
            if artifact is None:
                artifact = self._build_artifact(
                    task,
                    self.states[task.case_id],
                    export_key,
                )
                self._queue_cache(
                    export_key,
                    {"artifact": artifact},
                    task_id=task.case_id,
                    stage=stage,
                )
                self.runtimes[task.case_id].record(stage, "computed")
            target = artifact_root / f"{task.case_id}.json"
            atomic_write_bytes(target, canonical_bytes(artifact))
            artifacts[task.case_id] = artifact
        return artifacts

    def _commit(
        self,
        *,
        stage: str,
        cycle: int,
        artifacts: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema_version": "checkpoint-v1",
            "stage": stage,
            "cycle": cycle,
            "completed_tasks": sorted(self.states),
            "task_states": {
                task.case_id: state_to_dict(self.states[task.case_id])
                for task in self.tasks
                if task.case_id in self.states
            },
            "task_fingerprints": {
                task.case_id: task.fingerprint for task in self.tasks
            },
            "snapshot_hashes": {
                task.case_id: self.runtimes[task.case_id].snapshot.semantic_hash
                for task in self.tasks
            },
            "cache_keys": self.cache_keys,
            "config_hash": self.config_hash,
            "tasks_fingerprint": self.tasks_fingerprint,
            "config_generation": self.catalog.config_generation,
            "algorithm_version": self.config.algorithm_version,
            "cache_stats": self.cache.summary(),
            "degradation": self.degradation.as_list(),
            "artifacts": sorted((artifacts or {}).keys()),
        }
        committed = self.checkpoints.write_commit(body)
        self._flush_pending_cache()
        return committed

    def _restore_from_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        if checkpoint.get("config_hash") != self.config_hash:
            task_id = self.tasks[0].case_id
            raise stale_result(
                "checkpoint dependencies do not match current configuration",
                task_id=task_id,
                field="config_hash",
                details={
                    "checkpoint": checkpoint.get("config_hash"),
                    "current": self.config_hash,
                },
            )
        if checkpoint.get("tasks_fingerprint") != self.tasks_fingerprint:
            raise stale_result(
                "checkpoint task set differs from current task list",
                task_id=self.tasks[0].case_id,
                field="tasks_fingerprint",
            )
        completed = set(checkpoint.get("completed_tasks", []))
        expected = {task.case_id for task in self.tasks}
        if completed != expected:
            raise corrupt(
                "checkpoint completed task set is incomplete",
                field="completed_tasks",
                details={"missing": sorted(expected - completed)},
            )
        for task in self.tasks:
            raw = checkpoint.get("task_states", {}).get(task.case_id)
            if not isinstance(raw, dict):
                raise corrupt(
                    "checkpoint is missing task state",
                    task_id=task.case_id,
                    field="task_states",
                )
            self.states[task.case_id] = state_from_dict(raw)
        keys = checkpoint.get("cache_keys")
        if not isinstance(keys, dict):
            self._init_keys()
        else:
            self.cache_keys = copy.deepcopy(keys)
        for task in self.tasks:
            keys_for_task = self.cache_keys.get(task.case_id)
            if not isinstance(keys_for_task, dict) or "INIT" not in keys_for_task:
                keys_for_task = {
                    "INIT": self._init_key(task),
                    "GRAD": {},
                    "UPDATE": {},
                    "EXPORT": None,
                }
                self.cache_keys[task.case_id] = keys_for_task
            keys_for_task.setdefault("GRAD", {})
            keys_for_task.setdefault("UPDATE", {})
            keys_for_task.setdefault("EXPORT", None)
        previous_degradation = checkpoint.get("degradation")
        if isinstance(previous_degradation, list):
            existing = {
                content_hash(entry): entry for entry in self.degradation.as_list()
            }
            for entry in previous_degradation:
                existing.setdefault(content_hash(entry), entry)
            self.degradation.entries = list(existing.values())

    def _next_stage(self, stage: str, cycle: int) -> tuple[str, int] | None:
        if stage == "INITIALIZE":
            return ("GRAD_ACCUM", 1)
        if stage == "GRAD_ACCUM":
            return ("OPTIMIZER_UPDATE", cycle)
        if stage == "OPTIMIZER_UPDATE":
            if cycle < self.max_optimizer_steps:
                return ("GRAD_ACCUM", cycle + 1)
            return ("EXPORT", self.max_optimizer_steps + 1)
        return None

    def _load_artifacts(self) -> dict[str, dict[str, Any]]:
        root = self.state_dir / "artifacts" / self.config.run_id
        artifacts: dict[str, dict[str, Any]] = {}
        for task in self.tasks:
            path = root / f"{task.case_id}.json"
            if not path.is_file():
                raise corrupt(
                    "committed export artifact is missing",
                    task_id=task.case_id,
                    field="artifact",
                    details={"path": str(path)},
                )
            artifact = read_json(path)
            recomputed = content_hash(
                {key: value for key, value in artifact.items() if key != "artifact_checksum"}
            )
            if recomputed != artifact.get("artifact_checksum"):
                raise corrupt(
                    "export artifact checksum mismatch",
                    task_id=task.case_id,
                    field="artifact_checksum",
                )
            artifacts[task.case_id] = artifact
        return artifacts

    def _write_report(self, report: dict[str, Any]) -> None:
        reports = self.state_dir / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        path = reports / f"{self.config.run_id}.json"
        atomic_write_bytes(path, canonical_bytes(report))
        report["report_path"] = str(path)

    def _base_report(self, status: str, checkpoint: dict[str, Any] | None) -> dict[str, Any]:
        latest = None
        if checkpoint is not None:
            latest = {
                "version": checkpoint["version"],
                "stage": checkpoint["stage"],
                "cycle": checkpoint.get("cycle", 0),
                "checksum": checkpoint["checksum"],
            }
        current_task_stage_stats = {
            task.case_id: self.runtimes[task.case_id].stage_stats
            for task in self.tasks
        }
        history_task_stage_stats = (
            self._previous_report.get("task_stage_stats", {})
            if self._previous_report
            else {}
        )
        if not any(current_task_stage_stats.values()) and history_task_stage_stats:
            task_stage_stats = history_task_stage_stats
        else:
            task_stage_stats = current_task_stage_stats
        return {
            "ok": status == "complete",
            "run_id": self.config.run_id,
            "status": status,
            "task_count": len(self.tasks),
            "config_generation": self.catalog.config_generation,
            "config_hash": self.config_hash,
            "algorithm_version": self.config.algorithm_version,
            "snapshot_format": self.config.snapshot_format,
            "latest_checkpoint": latest,
            "checkpoint_versions": self.checkpoints.list_versions(),
            "recovery_notes": self.checkpoints.recovery_notes,
            "cache": self.cache.summary(),
            "cache_entries_by_task": self.cache.entries_by_task(),
            "task_stage_stats": task_stage_stats,
            "degradation": self.degradation.as_list(),
            "resource_limits": self.config.limits.as_dict(),
        }

    def run(
        self,
        *,
        recover: bool = True,
        interrupt_check: InterruptCheck | None = None,
        kill_hook: KillHook | None = None,
    ) -> dict[str, Any]:
        status = "running"
        checkpoint: dict[str, Any] | None = None
        artifacts: dict[str, dict[str, Any]] = {}
        try:
            if recover:
                checkpoint = self.checkpoints.recover_latest()
            if checkpoint is not None:
                self._restore_from_checkpoint(checkpoint)
                if checkpoint["stage"] == "EXPORT":
                    artifacts = self._load_artifacts()
                    report = self._base_report("complete", checkpoint)
                    report["artifacts"] = artifacts
                    report["artifacts_by_task"] = {
                        task_id: {
                            "final_param_summary": artifact["final_param_summary"],
                            "final_optimizer_summary": artifact["final_optimizer_summary"],
                            "final_state_digest": artifact["final_state_digest"],
                            "steps_completed": artifact["steps_completed"],
                            "optimizer_steps_completed": artifact["optimizer_steps_completed"],
                            "trace": artifact["trace"],
                            "record_count": artifact["record_count"],
                        }
                        for task_id, artifact in artifacts.items()
                    }
                    report["status_detail"] = "recovered committed EXPORT stage without recomputation"
                    self.report = report
                    self._write_report(report)
                    return report
                next_stage = self._next_stage(checkpoint["stage"], int(checkpoint.get("cycle", 0)))
                if next_stage is None:
                    raise corrupt("checkpoint stage has no next transition", field="stage")
                stage, cycle = next_stage
            else:
                stage, cycle = "INITIALIZE", 0

            if interrupt_check and interrupt_check() and checkpoint is not None:
                report = self._base_report("interrupted_at_commit_boundary", checkpoint)
                report["next_stage"] = {"stage": stage, "cycle": cycle}
                report["status_detail"] = "exit signal observed at an existing commit boundary"
                self.report = report
                self._write_report(report)
                return report

            while True:
                if stage == "INITIALIZE":
                    self._stage_initialize(kill_hook)
                    checkpoint = self._commit(stage=stage, cycle=0)
                    if interrupt_check and interrupt_check():
                        status = "interrupted_at_commit_boundary"
                        break
                    stage, cycle = "GRAD_ACCUM", 1
                    continue
                if stage == "GRAD_ACCUM":
                    self._stage_grad_accum(cycle, kill_hook)
                    checkpoint = self._commit(stage=stage, cycle=cycle)
                    if interrupt_check and interrupt_check():
                        status = "interrupted_at_commit_boundary"
                        break
                    stage = "OPTIMIZER_UPDATE"
                    continue
                if stage == "OPTIMIZER_UPDATE":
                    self._stage_optimizer_update(cycle, kill_hook)
                    checkpoint = self._commit(stage=stage, cycle=cycle)
                    if interrupt_check and interrupt_check():
                        status = "interrupted_at_commit_boundary"
                        break
                    if cycle < self.max_optimizer_steps:
                        stage, cycle = "GRAD_ACCUM", cycle + 1
                    else:
                        stage, cycle = "EXPORT", self.max_optimizer_steps + 1
                    continue
                if stage == "EXPORT":
                    artifacts = self._stage_export(kill_hook)
                    checkpoint = self._commit(stage=stage, cycle=cycle, artifacts=artifacts)
                    status = "complete"
                    break
                raise corrupt(f"unknown stage {stage}", field="stage")
            if status == "complete":
                for task in self.tasks:
                    state = self.states[task.case_id]
                    if state.step != task.steps or state.micro_in_window != 0:
                        raise corrupt(
                            "completed training state is inconsistent with task steps",
                            task_id=task.case_id,
                            field="steps",
                            details={
                                "state_step": state.step,
                                "task_steps": task.steps,
                                "micro_in_window": state.micro_in_window,
                            },
                        )
            report = self._base_report(status, checkpoint)
            report["task_status"] = {
                task.case_id: {
                    "status": (
                        "EXPORTED"
                        if status == "complete" or task.case_id in artifacts
                        else ("TRAINING" if self.states.get(task.case_id) else "PENDING")
                    ),
                    "step": self.states[task.case_id].step if task.case_id in self.states else 0,
                    "optimizer_step": (
                        self.states[task.case_id].update_count
                        if task.case_id in self.states
                        else 0
                    ),
                }
                for task in self.tasks
            }
            if artifacts:
                report["artifacts"] = artifacts
                report["artifacts_by_task"] = {
                    task_id: {
                        "final_param_summary": artifact["final_param_summary"],
                        "final_optimizer_summary": artifact["final_optimizer_summary"],
                        "final_state_digest": artifact["final_state_digest"],
                        "steps_completed": artifact["steps_completed"],
                        "optimizer_steps_completed": artifact["optimizer_steps_completed"],
                        "trace": artifact["trace"],
                        "record_count": artifact["record_count"],
                    }
                    for task_id, artifact in artifacts.items()
                }
            if status == "interrupted_at_commit_boundary":
                report["next_stage"] = {
                    "stage": "GRAD_ACCUM" if checkpoint and checkpoint["stage"] == "INITIALIZE" else "resume",
                    "note": "re-run without recovery changes to continue from the latest commit",
                }
                report["status_detail"] = "all partial in-memory task work was discarded; checkpoint is the commit boundary"
            else:
                report["status_detail"] = "all four stage types committed for the current run"
            self.report = report
            self._write_report(report)
            return report
        except BusinessError as exc:
            report = self._base_report("failed", checkpoint)
            report["error"] = exc.to_dict()
            report["status_detail"] = "business validation failed; committed checkpoint is unchanged"
            self.report = report
            self._write_report(report)
            raise


def run_engine(config: EngineConfig, **kwargs: Any) -> dict[str, Any]:
    return DiagnosticEngine(config).run(**kwargs)


__all__ = ["DiagnosticEngine", "EngineConfig", "STAGES", "run_engine"]

