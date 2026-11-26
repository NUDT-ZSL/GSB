"""Independent evidence/trace replay and stale-cache detection."""

from __future__ import annotations

from typing import Any

from .cache import dependency_hash
from .canonical import content_hash
from .chains import build_chains
from .errors import E_STALE_LINK, LinkError
from .evidence import CACHE_KEY_VERSION, compute_evidence, document_stats


def assert_not_stale(trace: dict[str, Any], current_dependency: str) -> None:
    trace_dependency = trace.get("dependency_hash")
    old_key_version = trace.get("cache_key_version") != CACHE_KEY_VERSION
    dependency_changed = trace_dependency is not None and trace_dependency != current_dependency
    served_from_old_key = bool(
        trace.get("cache_served")
        or trace.get("served_from_old_key")
        or trace.get("cache_status") == "hit_old_key"
    )
    if served_from_old_key and (old_key_version or dependency_changed):
        raise LinkError(
            E_STALE_LINK,
            "link result was served from a cache key that no longer matches dependencies",
            stage="replay",
            details={
                "pair_key": trace.get("pair_key"),
                "trace_cache_key": trace.get("cache_key"),
                "current_cache_key_version": CACHE_KEY_VERSION,
                "trace_cache_key_version": trace.get("cache_key_version"),
                "dependency_changed": dependency_changed,
            },
        )


def replay_traces(
    traces: list[dict[str, Any]],
    mentions: list[dict[str, Any]],
    book: dict[str, Any],
    *,
    enforce_stale: bool = True,
) -> dict[str, Any]:
    by_id = {item["mention_id"]: item for item in mentions}
    stats = document_stats(mentions)
    current_dependency = dependency_hash(mentions, book)
    replayed_evidence: list[dict[str, Any]] = []
    replay_details: list[dict[str, Any]] = []

    for trace in traces:
        if enforce_stale:
            assert_not_stale(trace, current_dependency)
        a = by_id[trace["pair_key"].split("|", 1)[0]]
        b = by_id[trace["pair_key"].split("|", 1)[1]]
        path = trace.get("path", "exact")
        evidence = compute_evidence(a, b, book, stats, path=path)
        component_hashes = [
            content_hash(item)
            for item in evidence["score"]["components"]
        ]
        score_recomputed = sum(
            item["contribution_bps"] for item in evidence["score"]["components"]
        )
        checks = {
            "evidence_hash": evidence["evidence_hash"] == trace["evidence_hash"],
            "score": evidence["score"]["total_bps"] == trace["score_total_bps"] == score_recomputed,
            "rule_generation": evidence["rule_generation"] == trace["rule_generation"],
            "component_hashes": component_hashes == trace.get("component_hashes"),
            "cache_key": evidence["cache_key"] == trace["cache_key"],
        }
        if not all(checks.values()):
            raise LinkError(
                "E_INCONSISTENT",
                "trace replay does not reproduce evidence",
                stage="replay",
                details={"pair_key": trace["pair_key"], "checks": checks},
            )
        replayed_evidence.append(evidence)
        replay_details.append({"pair_key": trace["pair_key"], "ok": True, "checks": checks})

    rebuilt = build_chains(replayed_evidence, mentions)
    trace_chain_ids = {trace["chain_id"] for trace in traces}
    rebuilt_chain_ids = {chain["chain_id"] for chain in rebuilt["chains"]}
    if trace_chain_ids != rebuilt_chain_ids:
        raise LinkError(
            "E_INCONSISTENT",
            "replayed chain set differs from trace chain set",
            stage="replay",
            details={
                "missing": sorted(trace_chain_ids - rebuilt_chain_ids),
                "unexpected": sorted(rebuilt_chain_ids - trace_chain_ids),
            },
        )
    confidence_by_chain = {
        chain["chain_id"]: chain["confidence_bps"] for chain in rebuilt["chains"]
    }
    trace_confidence: dict[str, int] = {}
    for trace in traces:
        trace_confidence.setdefault(trace["chain_id"], trace["score_total_bps"])
        trace_confidence[trace["chain_id"]] = min(
            trace_confidence[trace["chain_id"]], trace["score_total_bps"]
        )
    for chain_id, confidence in trace_confidence.items():
        if confidence_by_chain.get(chain_id) != confidence:
            raise LinkError(
                "E_INCONSISTENT",
                "replayed confidence differs from trace",
                stage="replay",
                details={
                    "chain_id": chain_id,
                    "trace_confidence": confidence,
                    "replayed_confidence": confidence_by_chain.get(chain_id),
                },
            )

    return {
        "ok": True,
        "replayed_edge_count": len(replay_details),
        "replayed_chain_count": len(rebuilt_chain_ids),
        "current_dependency_hash": current_dependency,
        "details": replay_details,
    }
