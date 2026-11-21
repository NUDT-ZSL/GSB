from __future__ import annotations

from typing import Any

from .errors import fail
from .models import AnswerPackage


def consistency_check(
    package: AnswerPackage,
    stage_sets: dict[str, Any],
    snapshot_versions: dict[str, Any] | None = None,
    retain_set: list[Any] | None = None,
    evict_set: list[Any] | None = None,
) -> dict[str, Any]:
    snapshot_versions = snapshot_versions or {}
    issues: list[dict[str, Any]] = []
    conv_ids = {
        str(stage["conv_id"])
        for stage in stage_sets.values()
        if "conv_id" in stage
    }
    if conv_ids != {package.conv_id}:
        issues.append(
            {
                "category": "conv_id_mismatch",
                "stage_conv_ids": sorted(conv_ids),
                "package_conv_id": package.conv_id,
            }
        )
    stage_doc_ids = {
        name: set(stage.get("doc_ids", []))
        for name, stage in stage_sets.items()
    }
    citation_doc_ids = {citation.doc_id for citation in package.citations}
    assembly_docs = stage_doc_ids.get("assembly")
    if assembly_docs is not None and assembly_docs != citation_doc_ids:
        issues.append(
            {
                "category": "assembly_doc_id_mismatch",
                "assembly_doc_ids": sorted(assembly_docs),
                "citation_doc_ids": sorted(citation_doc_ids),
            }
        )
    recall_docs = stage_doc_ids.get("recall", set())
    if not citation_doc_ids <= recall_docs:
        issues.append(
            {
                "category": "citation_not_in_recall",
                "citation_doc_ids": sorted(citation_doc_ids),
                "recall_doc_ids": sorted(recall_docs),
            }
        )
    citation_counts: dict[tuple[str, int, int], int] = {}
    for citation in package.citations:
        citation_counts[citation.key] = citation_counts.get(citation.key, 0) + 1
    repeated = [
        {"doc_id": key[0], "start": key[1], "end": key[2], "count": count}
        for key, count in sorted(citation_counts.items())
        if count != 1
    ]
    if repeated:
        issues.append(
            {
                "category": "citation_not_exactly_once",
                "citations": repeated,
            }
        )
    snippet_counts: dict[tuple[str, int, int], int] = {}
    for snippet in package.snippets:
        snippet_counts[snippet.key] = snippet_counts.get(snippet.key, 0) + 1
    snippet_set = set(snippet_counts)
    citation_set = set(citation_counts)
    if snippet_set != citation_set:
        issues.append(
            {
                "category": "snippet_citation_set_mismatch",
                "only_snippets": [list(key) for key in sorted(snippet_set - citation_set)],
                "only_citations": [list(key) for key in sorted(citation_set - snippet_set)],
            }
        )
    retain = {tuple(item) if isinstance(item, list) else item for item in (retain_set or [])}
    evict = {tuple(item) if isinstance(item, list) else item for item in (evict_set or [])}
    overlap = retain & evict
    if overlap:
        issues.append(
            {
                "category": "retain_evict_overlap",
                "overlap": [list(item) if isinstance(item, tuple) else item for item in sorted(overlap)],
            }
        )
    if issues:
        fail(
            "E_INCONSISTENT",
            "cross-module consistency check failed",
            {
                "categories": sorted({issue["category"] for issue in issues}),
                "issues": issues,
                "snapshot_versions": snapshot_versions,
                "conv_id": package.conv_id,
                "turn_no": package.turn_no,
            },
        )
    return {
        "ok": True,
        "conv_id": package.conv_id,
        "turn_no": package.turn_no,
        "conv_id_set": sorted(conv_ids),
        "recall_doc_ids": sorted(recall_docs),
        "citation_doc_ids": sorted(citation_doc_ids),
        "citation_count": len(package.citations),
        "each_citation_once": True,
        "retain_evict_disjoint": True,
        "snapshot_versions": snapshot_versions,
    }
