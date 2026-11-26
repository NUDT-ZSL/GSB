"""Traceability replay and stale-result rejection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import content_hash, read_json
from .config import DegradationLog, ResourceLimits
from .errors import corrupt, stale_result
from .snapshots import Snapshot, load_snapshot
from .tasks import NormalizedTask
from .training import (
    BETA1,
    BETA2,
    final_task_summaries,
    state_digest,
    train_task,
)


def artifact_checksum_valid(artifact: dict[str, Any]) -> bool:
    expected = artifact.get("artifact_checksum")
    actual = content_hash(
        {key: value for key, value in artifact.items() if key != "artifact_checksum"}
    )
    return isinstance(expected, str) and expected == actual


def load_artifact(path: str | Path) -> dict[str, Any]:
    artifact_path = Path(path)
    if not artifact_path.is_file():
        raise corrupt(
            "task artifact is missing",
            field="artifact",
            details={"path": str(artifact_path)},
        )
    try:
        artifact = read_json(artifact_path)
    except Exception as exc:
        raise corrupt(
            f"task artifact is unreadable: {exc}",
            field="artifact",
            details={"path": str(artifact_path)},
        )
    if not isinstance(artifact, dict):
        raise corrupt("task artifact root must be an object", field="artifact")
    if artifact.get("schema_version") != "task-artifact-v1":
        raise corrupt(
            "unsupported task artifact schema",
            field="schema_version",
            details={"actual": artifact.get("schema_version")},
        )
    if not artifact_checksum_valid(artifact):
        raise corrupt(
            "task artifact checksum mismatch",
            task_id=artifact.get("task_id"),
            field="artifact_checksum",
        )
    return artifact


def _expected_stage_keys(task: NormalizedTask, snapshot: Snapshot) -> dict[str, Any]:
    algorithm = "adam-peft-v1"
    init_key = content_hash(
        {
            "stage": "INITIALIZE",
            "algorithm_version": algorithm,
            "task_id": task.case_id,
            "base_model_rev": task.base_model_rev,
            "adapter_type": task.adapter_type,
            "adapter_dim": task.adapter_dim,
            "num_layers": task.num_layers,
            "seed": task.seed,
            "revision": task.revision,
            "snapshot_semantic_hash": snapshot.semantic_hash,
            "structure_summary": task.parameter_summary,
        }
    )
    grad_keys: dict[str, str] = {}
    update_keys: dict[str, str] = {}
    previous_update = init_key
    for cycle in range(1, task.optimizer_steps + 1):
        grad_key = content_hash(
            {
                "stage": "GRAD_ACCUM",
                "algorithm_version": algorithm,
                "task_id": task.case_id,
                "seed": task.seed,
                "micro_batch": task.micro_batch,
                "update_cycle": cycle,
                "previous_update_key": previous_update,
            }
        )
        update_key = content_hash(
            {
                "stage": "OPTIMIZER_UPDATE",
                "algorithm_version": algorithm,
                "task_id": task.case_id,
                "lr": task.lr,
                "beta1": BETA1,
                "beta2": BETA2,
                "update_cycle": cycle,
                "gradient_key": grad_key,
            }
        )
        grad_keys[str(cycle)] = grad_key
        update_keys[str(cycle)] = update_key
        previous_update = update_key
    export_key = content_hash(
        {
            "stage": "EXPORT",
            "algorithm_version": algorithm,
            "task_id": task.case_id,
            "update_key": previous_update,
            "steps": task.steps,
            "micro_batch": task.micro_batch,
            "revision": task.revision,
            "snapshot_semantic_hash": snapshot.semantic_hash,
        }
    )
    return {
        "INIT": init_key,
        "GRAD": grad_keys,
        "UPDATE": update_keys,
        "EXPORT": export_key,
    }


def stale_fields(
    task: NormalizedTask,
    snapshot: Snapshot,
    artifact: dict[str, Any],
    *,
    current_algorithm_version: str = "adam-peft-v1",
) -> list[str]:
    trace = artifact.get("trace")
    if not isinstance(trace, dict):
        return ["trace"]
    fields: list[str] = []
    snapshot_trace = trace.get("snapshot_version")
    if not isinstance(snapshot_trace, dict):
        fields.append("trace.snapshot_version")
    else:
        if snapshot_trace.get("semantic_hash") != snapshot.semantic_hash:
            fields.append("snapshot_version.semantic_hash")
        if snapshot_trace.get("revision") != snapshot.revision:
            fields.append("snapshot_version.revision")
        if snapshot_trace.get("snapshot_id") != snapshot.snapshot_id:
            fields.append("snapshot_version.snapshot_id")
    if trace.get("param_revision") != task.revision:
        fields.append("param_revision")
    if trace.get("lr") != task.lr:
        fields.append("lr")
    if trace.get("steps") != task.steps:
        fields.append("steps")
    if trace.get("micro_batch") != task.micro_batch:
        fields.append("micro_batch")
    if trace.get("seed") != task.seed:
        fields.append("seed")
    if trace.get("algorithm_version") != current_algorithm_version:
        fields.append("algorithm_version")
    expected = _expected_stage_keys(task, snapshot)
    trace_keys = trace.get("cache_keys")
    if not isinstance(trace_keys, dict):
        fields.append("cache_keys")
    else:
        if trace_keys.get("INIT") != expected["INIT"]:
            fields.append("cache_keys.INIT")
        trace_grad = trace_keys.get("GRAD")
        if not isinstance(trace_grad, dict):
            fields.append("cache_keys.GRAD")
        else:
            for cycle, key in expected["GRAD"].items():
                if trace_grad.get(cycle) != key:
                    fields.append(f"cache_keys.GRAD.{cycle}")
                    break
        trace_update = trace_keys.get("UPDATE")
        if not isinstance(trace_update, dict):
            fields.append("cache_keys.UPDATE")
        else:
            for cycle, key in expected["UPDATE"].items():
                if trace_update.get(cycle) != key:
                    fields.append(f"cache_keys.UPDATE.{cycle}")
                    break
        if trace_keys.get("EXPORT") != expected["EXPORT"]:
            fields.append("cache_keys.EXPORT")
    return sorted(set(fields))


def replay_task(
    *,
    artifact_path: str | Path,
    task: NormalizedTask,
    current_snapshot_path: str | Path,
    limits: ResourceLimits | None = None,
) -> dict[str, Any]:
    artifact = load_artifact(artifact_path)
    if artifact.get("task_id") != task.case_id:
        raise stale_result(
            "artifact belongs to a different task",
            task_id=task.case_id,
            field="task_id",
            details={"artifact_task": artifact.get("task_id")},
        )
    degradation = DegradationLog()
    snapshot = load_snapshot(
        current_snapshot_path,
        task,
        limits=limits,
        degradation=degradation,
    )
    mismatch = stale_fields(task, snapshot, artifact)
    if mismatch:
        raise stale_result(
            "recorded dependencies no longer match current versions",
            task_id=task.case_id,
            field=mismatch[0],
            details={
                "changed_fields": mismatch,
                "recorded_config_generation": artifact.get("trace", {}).get("config_generation"),
                "recorded_snapshot": artifact.get("trace", {}).get("snapshot_version", {}),
                "current_snapshot_hash": snapshot.semantic_hash,
            },
        )
    state = train_task(task, snapshot)
    summaries = final_task_summaries(state)
    replayed = {
        **summaries,
        "final_state_digest": state_digest(state),
        "record_count": len(state.records),
    }
    comparisons = {
        "final_param_summary": artifact.get("final_param_summary"),
        "final_optimizer_summary": artifact.get("final_optimizer_summary"),
        "final_state_digest": artifact.get("final_state_digest"),
        "steps_completed": artifact.get("steps_completed"),
        "optimizer_steps_completed": artifact.get("optimizer_steps_completed"),
        "record_count": artifact.get("record_count"),
    }
    differing = [
        field_name
        for field_name, recorded in comparisons.items()
        if recorded != replayed[field_name]
    ]
    if differing:
        raise stale_result(
            "independent replay did not reproduce the recorded artifact",
            task_id=task.case_id,
            field=differing[0],
            details={"differing_fields": differing, "replayed": replayed},
        )
    return {
        "ok": True,
        "task_id": task.case_id,
        "replay_equal": True,
        "artifact_path": str(artifact_path),
        "snapshot_path": str(current_snapshot_path),
        "final_param_summary": replayed["final_param_summary"],
        "final_optimizer_summary": replayed["final_optimizer_summary"],
        "final_state_digest": replayed["final_state_digest"],
        "steps_completed": replayed["steps_completed"],
        "optimizer_steps_completed": replayed["optimizer_steps_completed"],
        "used_cache": False,
        "degradation": degradation.as_list(),
    }


__all__ = ["artifact_checksum_valid", "load_artifact", "replay_task", "stale_fields"]

