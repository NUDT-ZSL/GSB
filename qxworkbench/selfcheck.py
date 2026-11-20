"""Cross-module consistency checks and auditable score recomputation."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from .data import Query, read_queries
from .errors import WorkbenchError, inconsistent
from .expand import ExpansionConfig
from .scoring import RetrievalConfig
from .snapshots import SnapshotStore, SnapshotView
from .util import round_score
from .workbench import query_view


def run_selfcheck(
    store: SnapshotStore,
    *,
    queries: Sequence[Query] | None = None,
    queries_path: str | Path | None = None,
    expansion_config: ExpansionConfig | None = None,
    retrieval_config: RetrievalConfig | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    if queries is None and queries_path is not None:
        queries = read_queries(queries_path)
    queries = list(queries or ())
    issues: list[dict[str, Any]] = []
    checked_queries = 0
    checked_contributions = 0
    checked_postings = 0

    try:
        view = store.load_current()
        verification = store.verify_commit(view)
    except WorkbenchError as exc:
        raise inconsistent(
            "cannot load a consistent committed snapshot",
            categories=["VERSION_CROSSCHECK"],
            underlying_code=exc.code,
            underlying_details=exc.details,
        ) from exc

    try:
        index_manifest, partitions = store.load_index_snapshot(
            view.index_manifest["version"]
        )
    except WorkbenchError as exc:
        raise inconsistent(
            "index snapshot cannot be loaded consistently",
            categories=["INDEX_SNAPSHOT"],
            underlying_code=exc.code,
            underlying_details=exc.details,
        ) from exc

    _check_snapshot_arithmetic(view, index_manifest, issues)
    _check_recovery_sets(view.commit, partitions, issues)
    _check_partition_integrity(view, partitions, issues)
    provenance = _check_posting_provenance(partitions, issues)
    checked_postings += provenance

    effective_retrieval = retrieval_config or RetrievalConfig(
        max_postings=1_000_000_000,
        max_resident_bytes=1_000_000_000,
    )
    for query in queries:
        checked_queries += 1
        try:
            result = query_view(
                view,
                query,
                expansion_config=expansion_config,
                retrieval_config=effective_retrieval,
            )
        except WorkbenchError as exc:
            issues.append(
                {
                    "category": "QUERY_EXECUTION",
                    "query_id": query.query_id,
                    "code": exc.code,
                    "details": exc.details,
                }
            )
            continue
        expansion = result["expansion"]
        if (
            expansion["bridge_version"] != view.commit["bridge_version"]
            or expansion["bridge_checksum"] != view.commit["bridge_checksum"]
        ):
            issues.append(
                {
                    "category": "RULE_EXPANSION_BRIDGE_VERSION",
                    "query_id": query.query_id,
                    "expected": {
                        "version": view.commit["bridge_version"],
                        "checksum": view.commit["bridge_checksum"],
                    },
                    "actual": {
                        "version": expansion["bridge_version"],
                        "checksum": expansion["bridge_checksum"],
                    },
                }
            )
        for key in ("baseline_results", "rewritten_results"):
            for result_row in result[key]:
                checked_contributions += _check_result_contributions(
                    query.query_id,
                    key,
                    result_row,
                    issues,
                )
        _check_delta_spectrum(query.query_id, result, issues)

    report = {
        "ok": not issues,
        "commit_id": verification["commit_id"],
        "rule_version": verification["rule_version"],
        "bridge_version": verification["bridge_version"],
        "index_version": verification["index_version"],
        "partition_count": len(partitions),
        "posting_count": index_manifest["posting_count"],
        "decoded_bytes": index_manifest["decoded_bytes"],
        "checked_queries": checked_queries,
        "checked_contributions": checked_contributions,
        "checked_postings": checked_postings,
        "categories": sorted({issue["category"] for issue in issues}),
        "issues": issues,
    }
    if issues and strict:
        raise inconsistent(
            "cross-module consistency self-check failed",
            categories=report["categories"],
            issues=issues,
        )
    return report


def _check_snapshot_arithmetic(
    view: SnapshotView,
    index_manifest: Mapping[str, Any],
    issues: list[dict[str, Any]],
) -> None:
    entries = index_manifest["partitions"]
    if sum(item["posting_count"] for item in entries) != index_manifest["posting_count"]:
        issues.append(
            {
                "category": "INDEX_STATISTICS",
                "detail": "partition posting counts do not sum to manifest posting_count",
            }
        )
    if sum(item["decoded_bytes"] for item in entries) != index_manifest["decoded_bytes"]:
        issues.append(
            {
                "category": "INDEX_STATISTICS",
                "detail": "partition decoded bytes do not sum to manifest decoded_bytes",
            }
        )
    if index_manifest.get("bridge_version") != view.commit["bridge_version"]:
        issues.append(
            {
                "category": "VERSION_CROSSCHECK",
                "detail": "index bridge version differs from commit",
            }
        )
    if index_manifest.get("rule_version") != view.commit["rule_version"]:
        issues.append(
            {
                "category": "VERSION_CROSSCHECK",
                "detail": "index rule version differs from commit",
            }
        )


def _check_recovery_sets(
    commit: Mapping[str, Any],
    partitions: Mapping[str, Mapping[str, Any]],
    issues: list[dict[str, Any]],
) -> None:
    affected = set(commit["affected_partitions"])
    reused = set(commit["reused_partitions"])
    recomputed = set(commit["recomputed_partitions"])
    if reused & recomputed:
        issues.append(
            {
                "category": "PARTITION_RECOVERY",
                "detail": "reused and recomputed sets overlap",
                "overlap": sorted(reused & recomputed),
            }
        )
    if not affected <= (reused | recomputed):
        issues.append(
            {
                "category": "PARTITION_RECOVERY",
                "detail": "reuse/recompute union does not cover affected partitions",
                "uncovered": sorted(affected - (reused | recomputed)),
            }
        )
    if set(partitions) != (reused | recomputed):
        issues.append(
            {
                "category": "PARTITION_RECOVERY",
                "detail": "snapshot partition set differs from reuse/recompute union",
                "snapshot_only": sorted(set(partitions) - (reused | recomputed)),
                "sets_only": sorted((reused | recomputed) - set(partitions)),
            }
        )


def _check_partition_integrity(
    view: SnapshotView,
    partitions: Mapping[str, Mapping[str, Any]],
    issues: list[dict[str, Any]],
) -> None:
    for lang, partition in sorted(partitions.items()):
        if partition.get("bridge_version") != view.commit["bridge_version"]:
            issues.append(
                {
                    "category": "BRIDGE_VERSION",
                    "partition_lang": lang,
                    "detail": "partition bridge version differs from committed bridge",
                }
            )
        if partition.get("bridge_checksum") != view.commit["bridge_checksum"]:
            issues.append(
                {
                    "category": "BRIDGE_VERSION",
                    "partition_lang": lang,
                    "detail": "partition bridge checksum differs from committed bridge",
                }
            )
        docs = partition.get("docs", [])
        doc_ids = [doc["doc_id"] for doc in docs]
        if doc_ids != sorted(doc_ids):
            issues.append(
                {
                    "category": "INDEX_ORDER",
                    "partition_lang": lang,
                    "detail": "documents are not in doc_id order",
                }
            )
        posting_order = [
            (posting["doc_id"], posting["term"])
            for posting in partition.get("postings", [])
        ]
        if posting_order != sorted(posting_order):
            issues.append(
                {
                    "category": "INDEX_ORDER",
                    "partition_lang": lang,
                    "detail": "postings are not in doc_id/term order",
                }
            )
        if partition.get("stats", {}).get("posting_count") != len(
            partition.get("postings", [])
        ):
            issues.append(
                {
                    "category": "INDEX_STATISTICS",
                    "partition_lang": lang,
                    "detail": "partition posting_count statistic is wrong",
                }
            )
        term_index = partition.get("term_index", {})
        postings = partition.get("postings", [])
        for term, indexes in term_index.items():
            if any(postings[index]["term"] != term for index in indexes):
                issues.append(
                    {
                        "category": "INDEX_TABLE",
                        "partition_lang": lang,
                        "term": term,
                        "detail": "term_index points to a posting with a different term",
                    }
                )
                break


def _check_posting_provenance(
    partitions: Mapping[str, Mapping[str, Any]],
    issues: list[dict[str, Any]],
) -> int:
    line_cache: dict[str, list[str]] = {}
    checked = 0
    for lang, partition in sorted(partitions.items()):
        docs = {doc["doc_id"]: doc for doc in partition.get("docs", [])}
        for posting in partition.get("postings", []):
            checked += 1
            doc = docs.get(posting["doc_id"])
            if doc is None:
                issues.append(
                    {
                        "category": "POSTING_PROVENANCE",
                        "partition_lang": lang,
                        "doc_id": posting["doc_id"],
                        "detail": "posting refers to an unknown document",
                    }
                )
                continue
            source = posting.get("source", {})
            source_path = source.get("source")
            line_number = source.get("line")
            if not isinstance(source_path, str) or not isinstance(line_number, int):
                issues.append(
                    {
                        "category": "POSTING_PROVENANCE",
                        "partition_lang": lang,
                        "doc_id": posting["doc_id"],
                        "detail": "posting has invalid source location",
                    }
                )
                continue
            if source_path not in line_cache:
                try:
                    line_cache[source_path] = Path(source_path).read_text(
                        encoding="utf-8"
                    ).splitlines()
                except OSError as exc:
                    issues.append(
                        {
                            "category": "POSTING_PROVENANCE",
                            "source": source_path,
                            "detail": str(exc),
                        }
                    )
                    line_cache[source_path] = []
            lines = line_cache[source_path]
            if line_number < 1 or line_number > len(lines):
                issues.append(
                    {
                        "category": "POSTING_PROVENANCE",
                        "source": source_path,
                        "line": line_number,
                        "detail": "source line is out of range",
                    }
                )
                continue
            try:
                source_doc = json.loads(lines[line_number - 1])
            except json.JSONDecodeError:
                issues.append(
                    {
                        "category": "POSTING_PROVENANCE",
                        "source": source_path,
                        "line": line_number,
                        "detail": "source line is not valid JSON",
                    }
                )
                continue
            if source_doc.get("doc_id") != posting["doc_id"]:
                issues.append(
                    {
                        "category": "POSTING_PROVENANCE",
                        "source": source_path,
                        "line": line_number,
                        "detail": "source line doc_id does not match posting",
                    }
                )
    return checked


def _check_result_contributions(
    query_id: str,
    section: str,
    result_row: Mapping[str, Any],
    issues: list[dict[str, Any]],
) -> int:
    details = result_row.get("contributions", [])
    contributions = [detail["contribution"] for detail in details]
    merge_policy = result_row["merge_policy"]
    expected = (
        round_score(math.fsum(contributions))
        if merge_policy == "sum"
        else round_score(max(contributions, default=0.0))
    )
    if (
        expected != result_row["score"]
        or expected != result_row["recomputed_score"]
    ):
        issues.append(
            {
                "category": "CONTRIBUTION_RECOMPUTE",
                "query_id": query_id,
                "section": section,
                "doc_id": result_row["doc_id"],
                "expected": expected,
                "score": result_row["score"],
                "recomputed_score": result_row["recomputed_score"],
            }
        )
    return len(details)


def _check_delta_spectrum(
    query_id: str,
    result: Mapping[str, Any],
    issues: list[dict[str, Any]],
) -> None:
    baseline = {row["doc_id"]: row["score"] for row in result["baseline_results"]}
    rewritten = {row["doc_id"]: row["score"] for row in result["rewritten_results"]}
    for row in result["delta_spectrum"]:
        expected_delta = round_score(
            rewritten.get(row["doc_id"], 0.0)
            - baseline.get(row["doc_id"], 0.0)
        )
        if row["delta"] != expected_delta:
            issues.append(
                {
                    "category": "DELTA_SPECTRUM",
                    "query_id": query_id,
                    "doc_id": row["doc_id"],
                    "expected": expected_delta,
                    "actual": row["delta"],
                }
            )
