"""Dependency-aware invalidation between two DAG plans."""

from __future__ import annotations

from typing import Any

from .cache import ContentAddressedCache
from .formats import Repository
from .planning import Plan


def compare_plans(
    old_plan: Plan,
    new_plan: Plan,
    cache: ContentAddressedCache,
    old_repository: Repository,
    new_repository: Repository,
) -> dict[str, Any]:
    old_ids = set(old_plan.nodes)
    new_ids = set(new_plan.nodes)
    common = old_ids & new_ids
    changed_nodes = sorted(
        node_id
        for node_id in new_ids
        if node_id not in old_ids
        or old_plan.nodes[node_id].key != new_plan.nodes[node_id].key
    )
    removed_nodes = sorted(old_ids - new_ids)
    unchanged_new = sorted(
        node_id
        for node_id in common
        if old_plan.nodes[node_id].key == new_plan.nodes[node_id].key
    )
    changed_set = set(changed_nodes)
    removed_set = set(removed_nodes)
    affected_requests = sorted(
        {
            (
                new_plan.nodes[node_id].owner
                if node_id in new_plan.nodes
                else old_plan.nodes[node_id].owner
            )
            for node_id in changed_nodes + removed_nodes
            if (
                new_plan.nodes[node_id].owner
                if node_id in new_plan.nodes
                else old_plan.nodes[node_id].owner
            )
            != "global"
        }
    )
    affected_stages = sorted(
        {
            (
                new_plan.nodes[node_id].stage
                if node_id in new_plan.nodes
                else old_plan.nodes[node_id].stage
            )
            for node_id in changed_nodes + removed_nodes
        }
    )
    recompute_nodes = sorted(
        node_id for node_id in changed_nodes if node_id in new_plan.nodes
    )
    reuse_nodes = sorted(
        node_id
        for node_id in unchanged_new
        if cache.has(new_plan.nodes[node_id].key)
    )
    recompute_set = set(recompute_nodes)
    reuse_set = set(reuse_nodes)
    disjoint = not bool(recompute_set & reuse_set)
    coverage_owners = {
        new_plan.nodes[node_id].owner
        for node_id in recompute_set | reuse_set
        if new_plan.nodes[node_id].owner != "global"
    }
    covered = sorted(set(affected_requests) & coverage_owners)
    complete = disjoint and set(affected_requests) <= coverage_owners
    repository_changes = []
    old_index = {row["result_ref"]: row for row in old_repository.canonical_index()["entries"]}
    new_index = {row["result_ref"]: row for row in new_repository.canonical_index()["entries"]}
    for result_ref in sorted(set(old_index) | set(new_index)):
        if old_index.get(result_ref) != new_index.get(result_ref):
            repository_changes.append(
                {
                    "result_ref": result_ref,
                    "old": old_index.get(result_ref),
                    "new": new_index.get(result_ref),
                }
            )
    per_request_stages: dict[str, list[str]] = {}
    for node_id in changed_nodes + removed_nodes:
        node = (
            new_plan.nodes[node_id]
            if node_id in new_plan.nodes
            else old_plan.nodes[node_id]
        )
        if node.owner == "global":
            continue
        per_request_stages.setdefault(node.owner, []).append(node.stage)
    per_request_stages = {
        unit: sorted(set(stages))
        for unit, stages in sorted(per_request_stages.items())
    }
    result = {
        "baseline": {
            "repository_snapshot_version": old_repository.snapshot_version,
            "repository_index_hash": old_repository.index_hash(),
            "node_count": len(old_plan.nodes),
        },
        "current": {
            "repository_snapshot_version": new_repository.snapshot_version,
            "repository_index_hash": new_repository.index_hash(),
            "node_count": len(new_plan.nodes),
        },
        "repository_changes": repository_changes,
        "affected_requests": affected_requests,
        "affected_stages": affected_stages,
        "per_request_stages": per_request_stages,
        "recompute_nodes": recompute_nodes,
        "reuse_nodes": reuse_nodes,
        "recompute_set": recompute_nodes,
        "reuse_set": reuse_nodes,
        "removed_nodes": removed_nodes,
        "coverage": {
            "disjoint": disjoint,
            "affected_requests_covered": complete,
            "covered_affected_requests": covered,
            "missing_affected_requests": sorted(
                set(affected_requests) - coverage_owners
            ),
        },
        "counts": {
            "affected_requests": len(affected_requests),
            "affected_stages": len(affected_stages),
            "recompute_nodes": len(recompute_nodes),
            "reuse_nodes": len(reuse_nodes),
        },
    }
    if not disjoint or not complete:
        from .errors import E_INCONSISTENT, WorkbenchError

        raise WorkbenchError(
            E_INCONSISTENT,
            "invalidation reuse/recompute sets are inconsistent",
            coverage=result["coverage"],
        )
    return result


def compare_active_snapshot(
    snapshot_manifest: dict[str, Any],
    new_plan: Plan,
    cache: ContentAddressedCache,
    new_repository: Repository,
) -> dict[str, Any]:
    """Compare the current plan with the previously activated snapshot."""

    old_nodes = snapshot_manifest.get("nodes", {})
    new_ids = set(new_plan.nodes)
    old_ids = set(old_nodes)
    common = new_ids & old_ids
    changed_nodes = sorted(
        node_id
        for node_id in new_ids
        if node_id not in old_ids
        or old_nodes[node_id].get("key") != new_plan.nodes[node_id].key
    )
    removed_nodes = sorted(old_ids - new_ids)
    unchanged_new = sorted(
        node_id
        for node_id in common
        if old_nodes[node_id].get("key") == new_plan.nodes[node_id].key
    )
    affected_requests = sorted(
        {
            (
                new_plan.nodes[node_id].owner
                if node_id in new_plan.nodes
                else old_nodes[node_id].get("owner")
            )
            for node_id in changed_nodes + removed_nodes
            if (
                new_plan.nodes[node_id].owner
                if node_id in new_plan.nodes
                else old_nodes[node_id].get("owner")
            )
            not in (None, "global")
        }
    )
    affected_stages = sorted(
        {
            (
                new_plan.nodes[node_id].stage
                if node_id in new_plan.nodes
                else old_nodes[node_id].get("stage")
            )
            for node_id in changed_nodes + removed_nodes
        }
    )
    recompute_nodes = [
        node_id for node_id in changed_nodes if node_id in new_plan.nodes
    ]
    reuse_nodes = [
        node_id
        for node_id in unchanged_new
        if cache.has(new_plan.nodes[node_id].key)
    ]
    recompute_set = set(recompute_nodes)
    reuse_set = set(reuse_nodes)
    disjoint = not bool(recompute_set & reuse_set)
    coverage_owners = {
        new_plan.nodes[node_id].owner
        for node_id in recompute_set | reuse_set
        if new_plan.nodes[node_id].owner != "global"
    }
    covered = sorted(set(affected_requests) & coverage_owners)
    complete = disjoint and set(affected_requests) <= coverage_owners
    old_index = {
        row["result_ref"]: row
        for row in snapshot_manifest.get("repository", {}).get("entries", [])
    }
    new_index = {
        row["result_ref"]: row
        for row in new_repository.canonical_index()["entries"]
    }
    repository_changes = []
    for result_ref in sorted(set(old_index) | set(new_index)):
        if old_index.get(result_ref) != new_index.get(result_ref):
            repository_changes.append(
                {
                    "result_ref": result_ref,
                    "old": old_index.get(result_ref),
                    "new": new_index.get(result_ref),
                }
            )
    per_request_stages: dict[str, list[str]] = {}
    for node_id in changed_nodes + removed_nodes:
        node = (
            new_plan.nodes[node_id]
            if node_id in new_plan.nodes
            else None
        )
        owner = (
            node.owner
            if node is not None
            else old_nodes[node_id].get("owner")
        )
        stage = (
            node.stage
            if node is not None
            else old_nodes[node_id].get("stage")
        )
        if owner and owner != "global" and stage:
            per_request_stages.setdefault(owner, []).append(stage)
    per_request_stages = {
        unit: sorted(set(stages))
        for unit, stages in sorted(per_request_stages.items())
    }
    result = {
        "baseline": {
            "source": "active_snapshot",
            "repository_snapshot_version": snapshot_manifest.get(
                "repository", {}
            ).get("snapshot_version"),
            "repository_index_hash": snapshot_manifest.get(
                "repository_index_hash"
            ),
            "node_count": len(old_nodes),
        },
        "current": {
            "repository_snapshot_version": new_repository.snapshot_version,
            "repository_index_hash": new_repository.index_hash(),
            "node_count": len(new_plan.nodes),
        },
        "repository_changes": repository_changes,
        "affected_requests": affected_requests,
        "affected_stages": affected_stages,
        "per_request_stages": per_request_stages,
        "recompute_nodes": sorted(recompute_nodes),
        "reuse_nodes": sorted(reuse_nodes),
        "recompute_set": sorted(recompute_nodes),
        "reuse_set": sorted(reuse_nodes),
        "removed_nodes": removed_nodes,
        "coverage": {
            "disjoint": disjoint,
            "affected_requests_covered": complete,
            "covered_affected_requests": covered,
            "missing_affected_requests": sorted(
                set(affected_requests) - coverage_owners
            ),
        },
        "counts": {
            "affected_requests": len(affected_requests),
            "affected_stages": len(affected_stages),
            "recompute_nodes": len(recompute_nodes),
            "reuse_nodes": len(reuse_nodes),
        },
    }
    if not disjoint or not complete:
        from .errors import E_INCONSISTENT, WorkbenchError

        raise WorkbenchError(
            E_INCONSISTENT,
            "active snapshot invalidation coverage is inconsistent",
            coverage=result["coverage"],
        )
    return result

