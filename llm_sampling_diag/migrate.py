from __future__ import annotations

from typing import Any, Iterable

from .canonical import canonical_json
from .errors import DiagnosticError, ErrorCode
from .formats.v2 import extended_sampling, task_to_v2_json
from .model import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_REVISION,
    DEFAULT_REVISION_ID,
    MigrationOutcome,
    MigrationPolicy,
    UnifiedTask,
)


def _loss(
    records: list[dict[str, Any]],
    *,
    field: str,
    action: str,
    source: str,
    value: Any,
    lossy: bool = False,
) -> None:
    records.append(
        {
            "field": field,
            "action": action,
            "source": source,
            "value": value,
            "lossy": lossy,
        }
    )


def _raise_if_incomplete(task: UnifiedTask, policy: MigrationPolicy, records: list[dict[str, Any]]) -> None:
    losses = [record for record in records if record.get("lossy")]
    if not losses:
        return
    if policy.strict or policy.revision_policy == "strict":
        raise DiagnosticError(
            ErrorCode.E_SEMANTIC_INCOMPLETE,
            "migration would discard task semantics",
            field=losses[0]["field"],
            case_id=task.case_id,
            details={"losses": losses, "policy": _policy_dict(policy)},
        )


def _policy_dict(policy: MigrationPolicy) -> dict[str, Any]:
    return {
        "mask_policy": policy.mask_policy,
        "revision_policy": policy.revision_policy,
        "strict": policy.strict,
    }


def _v1_effective_after_s(task: UnifiedTask, base_time_ms: int) -> float:
    return (task.effective_at_ms - base_time_ms) / 1000.0


def _to_v1_task(
    task: UnifiedTask,
    *,
    base_time_ms: int,
    policy: MigrationPolicy,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    records: list[dict[str, Any]] = []
    lossy = False
    payload: dict[str, Any] = {
        "case_id": task.case_id,
        "effective_after_s": _v1_effective_after_s(task, base_time_ms),
        "logits": list(task.logits),
        "k_mode": task.k_mode,
        "k_value": task.k_value,
        "temperature": task.temperature,
        "tie_policy": task.tie_policy,
        "seed": task.seed,
        "error_budget": task.error_budget,
        "num_samples": task.num_samples,
        "top_p": task.top_p,
        "chunk_size": task.chunk_plan.chunk_size,
    }
    if policy.mask_policy == "flatten":
        payload["mask_excluded_indices"] = list(task.candidate_mask.excluded_indices)
        _loss(
            records,
            field="candidate_mask",
            action="flatten",
            source="v1_flat_mask",
            value=list(task.candidate_mask.excluded_indices),
        )
        if task.candidate_mask.selectors:
            _loss(
                records,
                field="candidate_mask.selectors",
                action="flatten_structure",
                source="v1_nested_mask_unrepresentable",
                value={"excluded_union": list(task.candidate_mask.excluded_indices)},
            )
    else:
        if task.candidate_mask.excluded_indices:
            lossy = True
            _loss(
                records,
                field="candidate_mask",
                action="drop",
                source="explicit_drop_policy",
                value=list(task.candidate_mask.excluded_indices),
                lossy=True,
            )
        else:
            _loss(
                records,
                field="candidate_mask",
                action="omit",
                source="empty_mask",
                value=[],
            )
    if task.revision == DEFAULT_REVISION and task.revision_id == DEFAULT_REVISION_ID:
        _loss(
            records,
            field="revision",
            action="implicit_default",
            source="v1_has_no_revision",
            value=DEFAULT_REVISION,
        )
    else:
        lossy = True
        _loss(
            records,
            field="revision",
            action="drop",
            source="v1_has_no_revision",
            value={"revision": task.revision, "revision_id": task.revision_id},
            lossy=True,
        )
    _raise_if_incomplete(task, policy, records)
    record = {
        "case_id": task.case_id,
        "from_format": "unified",
        "to_format": "v1",
        "policy": _policy_dict(policy),
        "actions": records,
        "semantic_complete": not lossy,
    }
    return payload, record, not lossy


def _to_v2_task(
    task: UnifiedTask,
    *,
    policy: MigrationPolicy,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    records: list[dict[str, Any]] = []
    lossy = False
    payload = task_to_v2_json(task)
    if task.candidate_mask.selectors:
        _loss(
            records,
            field="candidate_mask.selectors",
            action="flatten_structure",
            source="v2_flat_mask_only",
            value={"excluded_union": list(task.candidate_mask.excluded_indices)},
        )
    else:
        _loss(
            records,
            field="candidate_mask",
            action="preserve_flat",
            source="v2_flat_mask",
            value=list(task.candidate_mask.excluded_indices),
        )
    if task.revision == DEFAULT_REVISION and task.revision_id == DEFAULT_REVISION_ID:
        _loss(
            records,
            field="revision",
            action="implicit_default",
            source="v2_has_no_revision",
            value=DEFAULT_REVISION,
        )
    else:
        lossy = True
        _loss(
            records,
            field="revision",
            action="drop",
            source="v2_has_no_revision",
            value={"revision": task.revision, "revision_id": task.revision_id},
            lossy=True,
        )
    _raise_if_incomplete(task, policy, records)
    metadata = {
        "case_id": task.case_id,
        "effective_at_ms": task.effective_at_ms,
        "chunk_size": task.chunk_plan.chunk_size,
        "chunk_count": task.chunk_plan.chunk_count,
        "error_budget": task.error_budget,
        "mask_json": canonical_json(list(task.candidate_mask.excluded_indices)),
        "extended_json": canonical_json(extended_sampling(task)),
    }
    record = {
        "case_id": task.case_id,
        "from_format": "unified",
        "to_format": "v2",
        "policy": _policy_dict(policy),
        "actions": records,
        "semantic_complete": not lossy,
    }
    return {"shard_task": payload, "metadata_row": metadata}, record, not lossy


def _to_v3_task(
    task: UnifiedTask,
    *,
    policy: MigrationPolicy,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    records: list[dict[str, Any]] = []
    nested_mask = task.candidate_mask.to_nested_dict()
    if task.candidate_mask.selectors:
        _loss(
            records,
            field="candidate_mask",
            action="preserve_nested",
            source="v3_native",
            value=list(task.candidate_mask.excluded_indices),
        )
    else:
        _loss(
            records,
            field="candidate_mask",
            action="synthesize_flat_selector",
            source="empty_or_flat_input_mask",
            value=list(task.candidate_mask.excluded_indices),
        )
    _loss(
        records,
        field="revision",
        action="preserve",
        source="v3_native",
        value={"number": task.revision, "revision_id": task.revision_id},
    )
    payload = {
        "case": {
            "id": task.case_id,
            "effective": {"at_unix_ms": task.effective_at_ms},
        },
        "logits": {
            "dtype": "float64",
            "shape": list(task.logits_shape),
            "values": list(task.logits),
        },
        "sampling": {
            "mode": task.k_mode,
            "value": task.k_value,
            "temperature": task.temperature,
            "tie_policy": task.tie_policy,
            "seed": task.seed,
            "num_samples": task.num_samples,
            "top_p": task.top_p,
        },
        "candidate_mask": nested_mask,
        "error_budget": task.error_budget,
        "revision": {"number": task.revision, "revision_id": task.revision_id},
        "chunk_plan": task.chunk_plan.to_dict(),
    }
    _raise_if_incomplete(task, policy, records)
    record = {
        "case_id": task.case_id,
        "from_format": "unified",
        "to_format": "v3",
        "policy": _policy_dict(policy),
        "actions": records,
        "semantic_complete": True,
    }
    return payload, record, True


def migrate_task(
    task: UnifiedTask,
    target_format: str,
    *,
    policy: MigrationPolicy | None = None,
    base_time_ms: int | None = None,
) -> MigrationOutcome:
    policy = policy or MigrationPolicy()
    policy.validate()
    target = target_format.lower()
    if target == "v1":
        base = task.effective_at_ms if base_time_ms is None else base_time_ms
        payload, record, complete = _to_v1_task(task, base_time_ms=base, policy=policy)
    elif target == "v2":
        payload, record, complete = _to_v2_task(task, policy=policy)
    elif target == "v3":
        payload, record, complete = _to_v3_task(task, policy=policy)
    elif target in {"unified", "v0"}:
        payload = task.to_snapshot_dict()
        record = {
            "case_id": task.case_id,
            "from_format": "unified",
            "to_format": "unified",
            "actions": [
                {"field": "all", "action": "identity", "source": "unified_native", "value": "preserved"}
            ],
            "semantic_complete": True,
        }
        complete = True
    else:
        raise DiagnosticError(
            ErrorCode.E_VERSION_UNSUPPORTED,
            f"unsupported migration target {target_format!r}",
            field="target_format",
        )
    return MigrationOutcome(
        target_format=target,
        payload=payload,
        records=[record],
        semantic_complete=complete,
    )


def migrate_bundle(
    tasks: Iterable[UnifiedTask],
    target_format: str,
    *,
    policy: MigrationPolicy | None = None,
) -> MigrationOutcome:
    task_list = list(tasks)
    policy = policy or MigrationPolicy()
    policy.validate()
    target = target_format.lower()
    records: list[dict[str, Any]] = []
    semantic_complete = True
    if target == "v1":
        if not task_list:
            payload = {"format": "task-v1", "version": 1, "base_time_unix_ms": 0, "tasks": []}
        else:
            base = min(task.effective_at_ms for task in task_list)
            migrated_tasks = []
            for task in task_list:
                v1_task, record, complete = _to_v1_task(task, base_time_ms=base, policy=policy)
                migrated_tasks.append(v1_task)
                records.append(record)
                semantic_complete = semantic_complete and complete
            payload = {
                "format": "task-v1",
                "version": 1,
                "base_time_unix_ms": base,
                "tasks": migrated_tasks,
            }
    elif target == "v2":
        shard_tasks: list[dict[str, Any]] = []
        metadata_rows: list[dict[str, Any]] = []
        for task in task_list:
            payload_item, record, complete = _to_v2_task(task, policy=policy)
            shard_tasks.append(payload_item["shard_task"])
            metadata_rows.append(payload_item["metadata_row"])
            records.append(record)
            semantic_complete = semantic_complete and complete
        payload = {
            "format": "task-v2-bundle",
            "version": 2,
            "shard_count": 1,
            "shard": {"shard_id": "shard-000", "tasks": shard_tasks},
            "metadata_rows": metadata_rows,
        }
    elif target == "v3":
        migrated = []
        for task in task_list:
            v3_task, record, complete = _to_v3_task(task, policy=policy)
            migrated.append(v3_task)
            records.append(record)
            semantic_complete = semantic_complete and complete
        payload = {"format": "task-v3", "version": 3, "tasks": migrated}
    else:
        raise DiagnosticError(
            ErrorCode.E_VERSION_UNSUPPORTED,
            f"unsupported migration target {target_format!r}",
            field="target_format",
        )
    return MigrationOutcome(
        target_format=target,
        payload=payload,
        records=records,
        semantic_complete=semantic_complete,
    )


def validate_migration_roundtrip(task: UnifiedTask, target_format: str) -> bool:
    outcome = migrate_task(task, target_format)
    if target_format == "v1":
        from .formats.v1 import _build_task

        rebuilt, _ = _build_task(
            outcome.payload,
            base_time_ms=_v1_base_for_single(outcome.payload, task),
            line=1,
            source_ref="<migration-roundtrip>",
            default_chunk_size=DEFAULT_CHUNK_SIZE,
        )
    elif target_format == "v2":
        from .formats.v2 import read_v2
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            from .formats.v2 import write_v2_bundle

            write_v2_bundle([task], temp_dir, shard_size=1)
            rebuilt = read_v2(temp_dir).tasks[0]
    elif target_format == "v3":
        from .formats.v3 import _build_task

        rebuilt, _ = _build_task(outcome.payload, line=1, source_ref="<migration-roundtrip>")
    else:
        rebuilt = task
    left = task.semantic_projection(include_revision=False)
    right = rebuilt.semantic_projection(include_revision=False)
    return canonical_json(left) == canonical_json(right)


def _v1_base_for_single(payload: dict[str, Any], task: UnifiedTask) -> int:
    return int(round(task.effective_at_ms - float(payload["effective_after_s"]) * 1000.0))

