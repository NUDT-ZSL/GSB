from __future__ import annotations

from collections import deque
from typing import Any, Iterable, Optional


def segment_digest_map(report: dict[str, Any]) -> dict[str, str]:
    result = {}
    for segment in report.get("segments", []):
        key = segment["canonical_key"]
        digest = (
            segment.get("content_sha256")
            or segment.get("actual_digest")
            or segment.get("declared_digest")
            or segment.get("digest")
        )
        if digest:
            result[key] = digest
    return result


def edge_list(report: dict[str, Any]) -> list[tuple[str, str, str]]:
    edges = []
    for edge in report.get("dependencies", {}).get("edges", []):
        edges.append((edge["from"], edge["to"], edge.get("kind", "dependency")))
    return sorted(set(edges))


def compute_invalidation(
    old_report: Optional[dict[str, Any]],
    new_report: dict[str, Any],
    *,
    file_id: Optional[str] = None,
    additional_edges: Optional[Iterable[tuple[str, str]]] = None,
) -> dict[str, Any]:
    """Return deterministic changed sources and transitive invalidation closure.

    An edge ``source -> target`` means source depends on target.  Therefore a
    changed target invalidates sources by traversing reverse edges.
    """
    old_digests = segment_digest_map(old_report) if old_report else {}
    new_digests = segment_digest_map(new_report)
    effective_file_id = file_id or new_report.get("file", {}).get("file_id")

    changed: list[dict[str, Any]] = []
    for key in sorted(set(old_digests) | set(new_digests)):
        old_value = old_digests.get(key)
        new_value = new_digests.get(key)
        if old_value is None:
            reason = "added"
        elif new_value is None:
            reason = "removed"
        elif old_value != new_value:
            reason = "digest_changed"
        else:
            continue
        changed.append(
            {
                "canonical_key": key,
                "reason": reason,
                "old_content_sha256": old_value,
                "new_content_sha256": new_value,
            }
        )

    edges: set[tuple[str, str]] = set()
    for report in (old_report, new_report):
        if not report:
            continue
        for source, target, _kind in edge_list(report):
            edges.add((source, target))
    if additional_edges:
        for source, target in additional_edges:
            edges.add((source, target))

    reverse: dict[str, set[str]] = {}
    for source, target in edges:
        reverse.setdefault(target, set()).add(source)
        reverse.setdefault(source, set())

    seeds = sorted(item["canonical_key"] for item in changed)
    seed_set = set(seeds)
    invalidated: dict[str, dict[str, Any]] = {}

    for seed in seeds:
        invalidated.setdefault(
            seed,
            {
                "canonical_key": seed,
                "root_cause": seed,
                "distance": 0,
                "path": [seed],
                "is_change_source": True,
            },
        )
        queue: deque[tuple[str, list[str]]] = deque([(seed, [seed])])
        seen = {seed}
        while queue:
            node, path = queue.popleft()
            for dependent in sorted(reverse.get(node, ())):
                if dependent in seen:
                    continue
                seen.add(dependent)
                dependent_path = path + [dependent]
                candidate = {
                    "canonical_key": dependent,
                    "root_cause": seed,
                    "distance": len(path),
                    "path": dependent_path,
                    "is_change_source": dependent in seed_set,
                }
                existing = invalidated.get(dependent)
                if existing is None or _candidate_preferred(candidate, existing):
                    invalidated[dependent] = candidate
                queue.append((dependent, dependent_path))

    recompute = sorted(invalidated)
    invalidated_items = [invalidated[key] for key in recompute]
    return {
        "schema_version": "bsl-revision-v1",
        "file_id": effective_file_id,
        "arrival_order_independent": True,
        "changed_segments": changed,
        "invalidated_segments": invalidated_items,
        "recompute_segments": recompute,
        "dependency_edges": [
            {"from": source, "to": target} for source, target in sorted(edges)
        ],
        "summary": {
            "changed_count": len(changed),
            "invalidated_count": len(
                [item for item in invalidated_items if not item["is_change_source"]]
            ),
            "recompute_count": len(recompute),
        },
    }


def _candidate_preferred(candidate: dict[str, Any], existing: dict[str, Any]) -> bool:
    candidate_tuple = (
        candidate["distance"],
        candidate["root_cause"],
        tuple(candidate["path"]),
    )
    existing_tuple = (
        existing["distance"],
        existing["root_cause"],
        tuple(existing["path"]),
    )
    return candidate_tuple < existing_tuple
