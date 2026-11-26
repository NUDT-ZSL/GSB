"""Incremental invalidation closure calculation."""

from __future__ import annotations

from typing import Any, Iterable

from .models import Plan, Task
from .storage import Storage
from .subtasks import Subtask, SubtaskChain, generate_job_subtasks
from .util import digest_obj


def generate_snapshot_chains(
    *,
    plan: Plan,
    current_tasks: dict[str, Task],
    revisions: dict[str, int],
    storage: Storage,
) -> dict[str, SubtaskChain]:
    revision_tasks: dict[tuple[str, int], Task] = {}
    for job_id, revision in revisions.items():
        revision_tasks[(job_id, revision)] = storage.get_task_revision(
            job_id, revision
        )

    order: list[str] = []
    marks: dict[str, int] = {}

    def visit(job_id: str) -> None:
        if marks.get(job_id) == 2:
            return
        marks[job_id] = 1
        for dep in sorted(plan.jobs[job_id].dependencies):
            visit(dep)
        marks[job_id] = 2
        order.append(job_id)

    for job_id in sorted(plan.jobs):
        visit(job_id)
    chains: dict[str, SubtaskChain] = {}
    for job_id in order:
        revision = revisions.get(job_id)
        if revision is None:
            task = current_tasks.get(job_id)
        else:
            task = revision_tasks.get((job_id, revision))
        if task is None:
            continue
        external: list[str] = []
        missing_dep = False
        for dep in plan.jobs[job_id].dependencies:
            dep_chain = chains.get(dep)
            if dep_chain is None:
                missing_dep = True
                break
            external.append(dep_chain.final_subtask.subtask_id)
        if missing_dep:
            continue
        chains[job_id] = generate_job_subtasks(
            task,
            plan.global_seed,
            external_dependencies=external,
        )
    return chains


def compute_chain_closure(
    *,
    plan_version: str,
    reason: str,
    old_chains: dict[str, SubtaskChain],
    new_chains: dict[str, SubtaskChain],
    committed_ids: set[str],
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    old_by_key: dict[tuple[str, str, int], Subtask] = {}
    new_by_key: dict[tuple[str, str, int], Subtask] = {}
    for chain in old_chains.values():
        for subtask in chain.subtasks:
            old_by_key[subtask.semantic_key] = subtask
    for chain in new_chains.values():
        for subtask in chain.subtasks:
            new_by_key[subtask.semantic_key] = subtask

    affected_ids: set[str] = set()
    affected_reasons: dict[str, set[str]] = {}
    reused_ids: set[str] = set()
    protected_ids: set[str] = set()

    for key, new_subtask in new_by_key.items():
        old_subtask = old_by_key.get(key)
        if old_subtask is None:
            affected_ids.add(new_subtask.subtask_id)
            affected_reasons.setdefault(new_subtask.subtask_id, set()).add("added")
        elif old_subtask.subtask_id != new_subtask.subtask_id:
            affected_ids.add(new_subtask.subtask_id)
            affected_reasons.setdefault(new_subtask.subtask_id, set()).add("hash_changed")
            if old_subtask.subtask_id in committed_ids:
                protected_ids.add(old_subtask.subtask_id)
        else:
            reused_ids.add(new_subtask.subtask_id)

    for key, old_subtask in old_by_key.items():
        if key not in new_by_key:
            affected_ids.add(old_subtask.subtask_id)
            affected_reasons.setdefault(old_subtask.subtask_id, set()).add("removed")
            if old_subtask.subtask_id in committed_ids:
                protected_ids.add(old_subtask.subtask_id)

    # A changed upstream subtask recursively invalidates all new descendants,
    # even if a malformed input somehow preserved a downstream local hash.
    dependents: dict[str, set[str]] = {}
    for subtask in new_by_key.values():
        for dep in subtask.dependencies:
            dependents.setdefault(dep, set()).add(subtask.subtask_id)
    queue = sorted(affected_ids)
    seen = set(affected_ids)
    while queue:
        current = queue.pop(0)
        for dependent in sorted(dependents.get(current, ())):
            if dependent not in seen:
                seen.add(dependent)
                queue.append(dependent)
                affected_ids.add(dependent)
                affected_reasons.setdefault(dependent, set()).add("dependency_invalidated")
    reused_ids -= affected_ids

    new_ids = {
        subtask.subtask_id
        for subtask in new_by_key.values()
    }
    items: list[dict[str, Any]] = []
    for subtask_id in sorted(affected_ids | protected_ids):
        if subtask_id in protected_ids and subtask_id not in new_ids:
            disposition = "protected_committed_removed"
        elif subtask_id in committed_ids and subtask_id in new_ids:
            disposition = "already_committed"
        elif subtask_id in new_ids:
            disposition = "recompute"
        else:
            disposition = "removed"
        items.append(
            {
                "subtask_id": subtask_id,
                "disposition": disposition,
                "reason": ",".join(sorted(affected_reasons.get(subtask_id, {"protected"}))),
            }
        )
    fingerprint = digest_obj(
        {
            "plan_version": plan_version,
            "reason": reason,
            "affected": sorted(affected_ids),
            "protected": sorted(protected_ids),
            "old": sorted(old_by_key),
            "new": sorted(new_by_key),
            "details": details or {},
        }
    )
    return {
        "fingerprint": fingerprint,
        "plan_version": plan_version,
        "reason": reason,
        "affected": sorted(affected_ids),
        "reused": sorted(reused_ids),
        "protected_committed": sorted(protected_ids),
        "details": details or {},
        "items": items,
    }


def compute_cache_closure(
    *,
    plan_version: str,
    new_chains: dict[str, SubtaskChain],
    committed_ids: set[str],
    change_ids: Iterable[str],
    changes: list[dict[str, Any]],
) -> dict[str, Any]:
    subtasks = sorted(
        (
            subtask
            for chain in new_chains.values()
            for subtask in chain.subtasks
        ),
        key=lambda item: item.subtask_id,
    )
    affected = [subtask.subtask_id for subtask in subtasks]
    reused = [subtask.subtask_id for subtask in subtasks if subtask.subtask_id in committed_ids]
    change_id_list = sorted(set(change_ids))
    items = [
        {
            "subtask_id": subtask.subtask_id,
            "disposition": "reuse_ledger_cost_invalidated",
            "reason": "cache_layer_change",
        }
        for subtask in subtasks
    ]
    details = {
        "change_ids": change_id_list,
        "changes": changes,
        "ledger_effect": "none",
    }
    fingerprint = digest_obj(
        {
            "plan_version": plan_version,
            "reason": "cache_layer_change",
            "changes": change_id_list,
            "subtasks": affected,
        }
    )
    return {
        "fingerprint": fingerprint,
        "plan_version": plan_version,
        "reason": "cache_layer_change",
        "affected": affected,
        "reused": reused,
        "protected_committed": [],
        "details": details,
        "items": items,
    }

