"""Cross-node consistency assertions used by the acceptance report."""
from __future__ import annotations

from typing import Any

from .canonical import canonical_bytes
from .candidates import generate_candidates
from .errors import E_INCONSISTENT, WorkbenchError


def run_self_check(bundle: dict[str, Any]) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []

    def record(name: str, passed: bool, details: dict[str, Any] | None = None) -> None:
        checks.append({"name": name, "passed": passed, "details": details or {}})
        if not passed:
            failures.append({"name": name, "details": details or {}})

    input_ids = {doc["doc_id"] for doc in bundle["documents"]}
    signature_ids = {item["doc_id"] for item in bundle["signatures"]}
    candidate_ids = set(bundle["candidates"]["doc_pair_index"])
    decision_ids = set(bundle["decisions"]["decisions"])
    record_ids = {record["doc_id"] for record in bundle["doc_records"]}

    sets = {
        "ingest": sorted(input_ids),
        "signature": sorted(signature_ids),
        "candidate_index": sorted(candidate_ids),
        "decision": sorted(decision_ids),
        "query_record": sorted(record_ids),
    }
    same_set = input_ids == signature_ids == candidate_ids == decision_ids == record_ids
    record("doc_id_sets_identical", same_set, {"sets": sets} if not same_set else {"doc_count": len(input_ids)})

    recomputed_candidates = generate_candidates(bundle["signatures"], bundle["config"])
    closure_matches = canonical_bytes(recomputed_candidates) == canonical_bytes(bundle["candidates"])
    record(
        "candidate_pair_closure_recomputable",
        closure_matches,
        {
            "stored_pair_count": bundle["candidates"]["pair_count"],
            "recomputed_pair_count": recomputed_candidates["pair_count"],
        },
    )

    pair_ids = {pair["pair_id"] for pair in bundle["candidates"]["pairs"]}
    comparison_pair_ids = {item["pair_id"] for item in bundle["comparisons"]}
    record(
        "comparison_set_matches_candidate_set",
        pair_ids == comparison_pair_ids,
        {"missing": sorted(pair_ids - comparison_pair_ids), "extra": sorted(comparison_pair_ids - pair_ids)},
    )

    decisions = bundle["decisions"]["decisions"]
    removed_ids = set(bundle["decisions"]["removed_doc_ids"])
    decision_removed_ids = {doc_id for doc_id, decision in decisions.items() if decision["removed"]}
    record("removal_list_matches_decisions", removed_ids == decision_removed_ids, {"removed_doc_ids": sorted(removed_ids)})

    removal_entries = {item["doc_id"]: item for item in bundle["decisions"]["removed"]}
    removal_detail_ok = True
    removal_details: list[dict[str, Any]] = []
    for doc_id in sorted(removed_ids):
        entry = removal_entries.get(doc_id)
        decision = decisions[doc_id]
        ok = (
            entry is not None
            and entry["representative_doc_id"] == decision["representative_doc_id"]
            and len(decision["near_duplicate_neighbors"]) > 0
            and all("threshold_check" in neighbor and "hit_positions" in neighbor for neighbor in decision["near_duplicate_neighbors"])
        )
        if not ok:
            removal_detail_ok = False
            removal_details.append({"doc_id": doc_id})
    record("removed_docs_have_neighbors_hits_and_threshold_details", removal_detail_ok, {"bad_docs": removal_details})

    byte_mismatches: list[str] = []
    for record_item in bundle["doc_records"]:
        raw = bundle.get("doc_record_raw_bytes", {}).get(record_item["doc_id"])
        if raw is not None and canonical_bytes(record_item) != raw:
            byte_mismatches.append(record_item["doc_id"])
    record("doc_records_canonical_bytes_match_export", not byte_mismatches, {"mismatches": byte_mismatches})

    incremental = bundle.get("incremental")
    if incremental is not None:
        recomputed = set(incremental.get("recomputed_docs", []))
        reused = set(incremental.get("reused_docs", []))
        disjoint = recomputed.isdisjoint(reused)
        covers = recomputed | reused == input_ids
        record(
            "incremental_sets_disjoint_and_cover_all_docs",
            disjoint and covers,
            {
                "disjoint": disjoint,
                "covers": covers,
                "missing": sorted(input_ids - (recomputed | reused)),
                "overlap": sorted(recomputed & reused),
            },
        )

    if failures:
        raise WorkbenchError(
            E_INCONSISTENT,
            "Cross-module consistency self-check failed",
            details={"checks": checks, "failures": failures},
        )
    return {"ok": True, "code": "OK", "checks": checks, "check_count": len(checks)}
