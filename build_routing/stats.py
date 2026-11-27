from __future__ import annotations

from typing import Any, Mapping

from .model import (
    ALL_FILTERED,
    LOAD_ERROR,
    READY,
    SINGLE_CANDIDATE,
    NormalizedProject,
    SelectionResult,
)


def _empty_bucket() -> dict[str, Any]:
    return {
        "prefix_count": 0,
        "reachable_prefixes": 0,
        "unreachable_prefixes": 0,
        "backup_covered_prefixes": 0,
        "no_usable_candidate_count": 0,
        "no_backup_count": 0,
        "reason_counts": {
            LOAD_ERROR: 0,
            ALL_FILTERED: 0,
            SINGLE_CANDIDATE: 0,
        },
        "prefixes": [],
        "issues": [],
    }


def _add_selection(
    bucket: dict[str, Any],
    prefix_id: str,
    result: SelectionResult,
) -> None:
    bucket["prefix_count"] += 1
    reachable = result.best is not None
    bucket["reachable_prefixes" if reachable else "unreachable_prefixes"] += 1
    if result.backup is not None:
        bucket["backup_covered_prefixes"] += 1
    else:
        bucket["no_backup_count"] += 1
    if not reachable:
        bucket["no_usable_candidate_count"] += 1
    if result.status in bucket["reason_counts"]:
        bucket["reason_counts"][result.status] += 1
    bucket["prefixes"].append(
        {
            "prefix_id": prefix_id,
            "status": result.status,
            "best_next_hop_id": result.best.next_hop_id if result.best else None,
            "backup_next_hop_id": (
                result.backup.next_hop_id if result.backup else None
            ),
            "available_count": result.available_count,
            "filtered_count": result.filtered_count,
            "down_count": result.down_count,
            "load_error_count": result.load_error_count,
            "reachable": reachable,
            "backup_covered": result.backup is not None,
        }
    )
    if not reachable:
        details = {
            "community_filtered_candidates": result.filtered_count,
            "link_down_candidates": result.down_count,
            "reference_error_candidates": result.load_error_count,
        }
        bucket["issues"].append(
            {
                "prefix_id": prefix_id,
                "issue": "no_available_candidate",
                "reason": result.status,
                "details": details,
            }
        )
    elif result.backup is None:
        bucket["issues"].append(
            {
                "prefix_id": prefix_id,
                "issue": "no_backup_path",
                "reason": SINGLE_CANDIDATE,
                "details": {
                    "available_candidates": result.available_count,
                    "community_filtered_candidates": result.filtered_count,
                    "link_down_candidates": result.down_count,
                },
            }
        )
    if result.load_error_count:
        bucket["issues"].append(
            {
                "prefix_id": prefix_id,
                "issue": "reference_loading_error",
                "reason": LOAD_ERROR,
                "details": {
                    "reference_error_candidates": result.load_error_count,
                    "reachable_despite_error": reachable,
                },
            }
        )


def _finish_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    count = bucket["prefix_count"]
    bucket["reachability_ratio"] = (
        round(bucket["reachable_prefixes"] / count, 6) if count else 0.0
    )
    bucket["backup_coverage_ratio"] = (
        round(bucket["backup_covered_prefixes"] / count, 6) if count else 0.0
    )
    bucket["prefixes"].sort(key=lambda item: item["prefix_id"])
    bucket["issues"].sort(key=lambda item: (item["prefix_id"], item["issue"]))
    return bucket


def build_availability_stats(
    project: NormalizedProject,
    selections: Mapping[str, SelectionResult],
) -> dict[str, Any]:
    nodes = project.node_by_id()
    regions = project.region_by_id()
    reference_issue_prefixes: set[str] = set()
    for issue in project.reference_issues:
        if issue.entity == "prefix":
            reference_issue_prefixes.add(issue.entity_id)
        elif issue.entity == "candidate" and ":" in issue.entity_id:
            reference_issue_prefixes.add(issue.entity_id.split(":", 1)[0])
    node_buckets: dict[str, dict[str, Any]] = {
        node.id: _empty_bucket() for node in project.build_nodes
    }
    region_buckets: dict[str, dict[str, Any]] = {
        region.id: _empty_bucket() for region in project.regions
    }

    for prefix in project.prefixes:
        result = selections[prefix.id]
        node = nodes.get(prefix.build_node_id)
        if node is None:
            synthetic_node = f"__unresolved__:{prefix.build_node_id}"
            synthetic_region = "__unresolved__"
            node_buckets.setdefault(synthetic_node, _empty_bucket())
            region_buckets.setdefault(synthetic_region, _empty_bucket())
            _add_selection(node_buckets[synthetic_node], prefix.id, result)
            _add_selection(region_buckets[synthetic_region], prefix.id, result)
            node_buckets[synthetic_node]["issues"].append(
                {
                    "prefix_id": prefix.id,
                    "issue": "reference_loading_error",
                    "reason": LOAD_ERROR,
                    "details": {
                        "missing_build_node_id": prefix.build_node_id,
                    },
                }
            )
            region_buckets[synthetic_region]["issues"].append(
                {
                    "prefix_id": prefix.id,
                    "issue": "reference_loading_error",
                    "reason": LOAD_ERROR,
                    "details": {
                        "missing_build_node_id": prefix.build_node_id,
                    },
                }
            )
            continue
        _add_selection(node_buckets[node.id], prefix.id, result)
        _add_selection(region_buckets[node.region_id], prefix.id, result)
        if (
            prefix.id in reference_issue_prefixes
            and result.load_error_count == 0
        ):
            issue = {
                "prefix_id": prefix.id,
                "issue": "reference_loading_error",
                "reason": LOAD_ERROR,
                "details": {"reference_issue_count": 1},
            }
            node_buckets[node.id]["issues"].append(issue)
            region_buckets[node.region_id]["issues"].append(dict(issue))

    node_output: list[dict[str, Any]] = []
    for node_id in sorted(node_buckets):
        node = nodes.get(node_id)
        bucket = _finish_bucket(node_buckets[node_id])
        node_output.append(
            {
                "node_id": node_id,
                "node_name": node.name if node else "未解析构建节点",
                "region_id": node.region_id if node else "__unresolved__",
                **bucket,
            }
        )

    region_output: list[dict[str, Any]] = []
    for region_id in sorted(region_buckets):
        region = regions.get(region_id)
        bucket = _finish_bucket(region_buckets[region_id])
        node_ids = [
            node_id
            for node_id, node in nodes.items()
            if node.region_id == region_id
        ]
        region_output.append(
            {
                "region_id": region_id,
                "region_name": region.name if region else "未解析区域",
                "node_ids": sorted(node_ids),
                **bucket,
            }
        )

    total = _empty_bucket()
    for result in selections.values():
        _add_selection(total, result.prefix_id, result)
    _finish_bucket(total)
    return {
        "ordering": ["node", "region"],
        "reason_semantics": {
            LOAD_ERROR: "prefix has no candidates or all candidates have invalid references",
            ALL_FILTERED: (
                "no candidate survived community filters and link availability; "
                "details distinguish community_filtered_candidates and "
                "link_down_candidates"
            ),
            SINGLE_CANDIDATE: "exactly one candidate is usable, so no backup exists",
        },
        "totals": total,
        "nodes": node_output,
        "regions": region_output,
    }
