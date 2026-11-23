"""Cross-stage independent recomputation and consistency self-checks."""

from __future__ import annotations

from typing import Any

from .errors import E_INCONSISTENT, RecallError
from .ingest import IngestResult
from .inverted import merge_global_partitions, recompute_stats_from_documents
from .models import Document
from .projection import project_document
from .scoring import score_query
from .weights import WeightTable


def _decision_map(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["batch_key"]: item
        for item in plan["decisions"]
        if item["status"] in {"admitted", "degraded"}
    }


def _expected_stage_sets(
    plan: dict[str, Any],
) -> dict[str, set[str]]:
    accepted = set(_decision_map(plan))
    docs = {key for key in accepted if key.startswith("doc:")}
    queries = {key for key in accepted if key.startswith("query:")}
    return {
        "ingest": accepted,
        "inverted": docs,
        "projection": docs,
        "scoring": queries,
    }


def _fail(message: str, **details: Any) -> None:
    raise RecallError(E_INCONSISTENT, "consistency", message, details=details)


def _sort_postings(
    postings: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    return {
        term: sorted(postings[term], key=lambda item: item["doc_id"])
        for term in sorted(postings)
    }


def verify_consistency(
    *,
    ingest: IngestResult,
    plan: dict[str, Any],
    payload: dict[str, Any],
    completed: dict[str, set[str]],
    failed: dict[str, dict[str, Any]],
    weights: WeightTable,
    chunk_size: int,
    compensated: bool,
    decimals: int,
    subnormal_floor: float,
    threshold: float | None,
    compare_mode: str,
    tolerance: float,
    recomputed: dict[str, set[str]],
    reused: dict[str, set[str]],
) -> dict[str, Any]:
    decisions = _decision_map(plan)
    expected = _expected_stage_sets(plan)

    stage_sets: dict[str, Any] = {}
    for stage in ("ingest", "inverted", "projection", "scoring"):
        actual = set(completed.get(stage, set())) | set(failed.get(stage, {}))
        if actual != expected[stage]:
            _fail(
                f"{stage} batch set mismatch",
                stage=stage,
                expected=sorted(expected[stage]),
                actual=sorted(actual),
            )
        source = recomputed.get(stage, set()) | reused.get(stage, set())
        if recomputed.get(stage, set()) & reused.get(stage, set()):
            _fail(f"{stage} recomputed/reused sets overlap", stage=stage)
        if source != expected[stage]:
            _fail(
                f"{stage} recomputed/reused sets do not cover affected batches",
                stage=stage,
                expected=sorted(expected[stage]),
                actual=sorted(source),
            )
        stage_sets[stage] = {
            "expected": len(expected[stage]),
            "completed": len(completed.get(stage, set())),
            "failed": len(failed.get(stage, {})),
        }

    executed_docs: dict[int, list[Document]] = {}
    for batch_no in sorted(ingest.doc_batches):
        key = f"doc:{batch_no}"
        decision = decisions.get(key)
        if decision is None:
            continue
        count = int(decision["execution"]["count"])
        executed_docs[batch_no] = ingest.doc_batches[batch_no][:count]

    # Recompute each committed partition from document details.
    checked_partitions = 0
    for batch_no in sorted(executed_docs):
        key = f"doc:{batch_no}"
        if key not in completed.get("inverted", set()):
            continue
        docs = executed_docs[batch_no]
        stored = payload["partitions"].get(str(batch_no))
        if stored is None:
            _fail("missing partition for committed inverted batch", batch_no=batch_no)
        expected_partition, expected_postings = recompute_stats_from_documents(
            docs,
            compensated=compensated,
            decimals=decimals,
        )
        if expected_partition["document_count"] != stored["document_count"]:
            _fail("partition document count mismatch", batch_no=batch_no)
        if expected_partition["doc_len_sum"] != stored["doc_len_sum"]:
            _fail("partition document-length sum mismatch", batch_no=batch_no)
        if expected_partition["terms"] != stored["terms"]:
            _fail("partition term statistics mismatch", batch_no=batch_no)
        stored_postings = payload.get("batch_postings", {}).get(str(batch_no), {})
        if _sort_postings(expected_postings) != _sort_postings(stored_postings):
            _fail("partition postings mismatch", batch_no=batch_no)
        for term in sorted(stored["terms"]):
            if int(stored["terms"][term]["df"]) != len(stored_postings.get(term, [])):
                _fail("posting count does not equal document frequency", term=term)
        checked_partitions += 1

    committed_partitions = [
        payload["partitions"][key]
        for key in sorted(payload["partitions"], key=lambda item: int(item))
        if f"doc:{key}" in completed.get("inverted", set())
    ]
    expected_global = merge_global_partitions(
        committed_partitions,
        compensated=compensated,
        decimals=decimals,
    )
    stored_global = payload.get("global_stats")
    if stored_global != expected_global:
        _fail("global statistics do not match independent partition rollup")

    # Recompute every committed sparse document vector.
    checked_vectors = 0
    for batch_no in sorted(executed_docs):
        key = f"doc:{batch_no}"
        if key not in completed.get("projection", set()):
            continue
        for document in executed_docs[batch_no]:
            expected_vector = project_document(
                document,
                weights,
                decimals=decimals,
                subnormal_floor=subnormal_floor,
            )
            if payload["vectors"].get(document.doc_id) != expected_vector:
                _fail(
                    "sparse document vector mismatch",
                    batch_no=batch_no,
                    doc_id=document.doc_id,
                )
            checked_vectors += 1

    doc_vectors = [
        payload["vectors"][doc_id]
        for doc_id in sorted(payload["vectors"])
    ]
    checked_queries = 0
    for batch_no in sorted(ingest.query_batches):
        key = f"query:{batch_no}"
        if key not in completed.get("scoring", set()):
            continue
        decision = decisions[key]
        query_count = int(decision["execution"]["count"])
        executed_top_k = int(decision["execution"]["top_k"])
        for query in ingest.query_batches[batch_no][:query_count]:
            expected_result, expected_vector = score_query(
                query,
                doc_vectors,
                weights,
                executed_top_k=executed_top_k,
                threshold=threshold,
                compare_mode=compare_mode,
                tolerance=tolerance,
                decimals=decimals,
                subnormal_floor=subnormal_floor,
            )
            if payload["query_vectors"].get(query.query_id) != expected_vector:
                _fail("query sparse vector mismatch", query_id=query.query_id)
            if payload["results"].get(query.query_id) != expected_result:
                _fail("top-k result mismatch", query_id=query.query_id)
            for row in expected_result["top_k"]:
                vector = payload["vectors"][row["doc_id"]]
                from .projection import sparse_dot

                recomputed_score = sparse_dot(
                    expected_vector["dims"],
                    vector["dims"],
                    decimals=decimals,
                    subnormal_floor=subnormal_floor,
                )
                if recomputed_score != row["score"]:
                    _fail(
                        "top-k score cannot be recomputed from sparse vectors",
                        query_id=query.query_id,
                        doc_id=row["doc_id"],
                    )
            checked_queries += 1

    return {
        "status": "ok",
        "stage_batch_sets": stage_sets,
        "checked_partitions": checked_partitions,
        "checked_document_vectors": checked_vectors,
        "checked_queries": checked_queries,
        "provenance_disjoint": True,
        "topk_independently_recomputed": True,
    }
