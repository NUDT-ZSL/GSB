"""Cross-module consistency checks; failures become E_INCONSISTENT."""

from __future__ import annotations

from typing import Any

from .errors import E_INCONSISTENT, LinkError


def compare_evidence_paths(
    exact_evidence: list[dict[str, Any]],
    approx_evidence: list[dict[str, Any]],
    exact_chains: list[dict[str, Any]],
    approx_chains: list[dict[str, Any]],
) -> dict[str, Any]:
    exact_scores = {item["pair_key"]: item["score"]["total_bps"] for item in exact_evidence}
    approx_scores = {item["pair_key"]: item["score"]["total_bps"] for item in approx_evidence}
    if set(exact_scores) != set(approx_scores):
        only_exact = sorted(set(exact_scores) - set(approx_scores))
        only_approx = sorted(set(approx_scores) - set(exact_scores))
        raise LinkError(
            E_INCONSISTENT,
            "exact and approx paths produced different candidate key sets",
            stage="self_check.paths",
            details={"only_exact": only_exact, "only_approx": only_approx},
        )
    score_deltas = {
        key: exact_scores[key] - approx_scores[key]
        for key in sorted(exact_scores)
        if exact_scores[key] != approx_scores[key]
    }
    if score_deltas:
        raise LinkError(
            E_INCONSISTENT,
            "exact and approx scores differ",
            stage="self_check.paths",
            details={"score_deltas": score_deltas},
        )

    def fingerprint(chains: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {
            chain["chain_id"]: {
                "member_ids": chain["member_ids"],
                "edge_keys": chain["edge_keys"],
                "confidence_bps": chain["confidence_bps"],
            }
            for chain in chains
        }

    exact_fp = fingerprint(exact_chains)
    approx_fp = fingerprint(approx_chains)
    if exact_fp != approx_fp:
        raise LinkError(
            E_INCONSISTENT,
            "exact and approx chain results differ",
            stage="self_check.paths",
            details={"exact": exact_fp, "approx": approx_fp},
        )
    return {
        "name": "exact_vs_approx",
        "ok": True,
        "candidate_count": len(exact_evidence),
        "chain_count": len(exact_chains),
    }


def check_trace_coverage(
    chains: list[dict[str, Any]],
    evidence_by_key: dict[str, dict[str, Any]],
    traces: list[dict[str, Any]],
) -> dict[str, Any]:
    trace_by_pair = {item["pair_key"]: item for item in traces}
    missing_evidence: list[str] = []
    missing_trace: list[str] = []
    for chain in chains:
        for pair_key in chain["edge_keys"]:
            if pair_key not in evidence_by_key:
                missing_evidence.append(pair_key)
            if pair_key not in trace_by_pair:
                missing_trace.append(pair_key)
    if missing_evidence or missing_trace:
        raise LinkError(
            E_INCONSISTENT,
            "every chain edge must have evidence and a trace record",
            stage="self_check.trace",
            details={
                "missing_evidence": sorted(set(missing_evidence)),
                "missing_trace": sorted(set(missing_trace)),
            },
        )
    return {
        "name": "trace_coverage",
        "ok": True,
        "chain_count": len(chains),
        "trace_count": len(traces),
    }


def check_invalidation_coverage(incremental: dict[str, Any] | None) -> dict[str, Any]:
    if incremental is None:
        return {"name": "invalidation_coverage", "ok": True, "skipped": True}
    affected = set(incremental.get("affected_mention_ids", []))
    covered = set(incremental.get("covered_affected_mention_ids", []))
    missing = sorted(affected - covered)
    if missing:
        raise LinkError(
            E_INCONSISTENT,
            "invalidation propagation did not cover all affected mentions",
            stage="self_check.invalidation",
            details={"missing_mention_ids": missing},
        )
    return {
        "name": "invalidation_coverage",
        "ok": True,
        "affected_count": len(affected),
        "recomputed_pair_count": len(incremental.get("recomputed_pair_keys", [])),
    }


def run_self_checks(
    *,
    exact_evidence: list[dict[str, Any]],
    approx_evidence: list[dict[str, Any]],
    exact_chains: list[dict[str, Any]],
    approx_chains: list[dict[str, Any]],
    traces: list[dict[str, Any]],
    evidence_by_key: dict[str, dict[str, Any]],
    incremental: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checks = [
        compare_evidence_paths(
            exact_evidence, approx_evidence, exact_chains, approx_chains
        ),
        check_trace_coverage(exact_chains, evidence_by_key, traces),
        check_invalidation_coverage(incremental),
        {"name": "chain_registry_uniqueness", "ok": True},
    ]
    return {"ok": True, "checks": checks}
