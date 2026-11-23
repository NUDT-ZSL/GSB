"""Cross-module consistency self-checks."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence, Set

from .canonical import canonical_bytes, sha256_value
from .models import STAGES
from .nodes import quantize, recompute_quality_from_details
from .pipeline import RunResult


def _stage_node_ids(result: RunResult) -> Set[str]:
    node_ids: Set[str] = set()
    chunk_stage = result.stage_results.get("chunk_tokenize", {})
    for session_id, artifacts in chunk_stage.items():
        for artifact in artifacts:
            node_ids.add(
                f"{session_id}/chunk_tokenize/"
                f"block:{int(artifact['payload']['block_index'])}"
            )
    score_stage = result.stage_results.get("unit_score", {})
    for session_id, artifacts in score_stage.items():
        for artifact in artifacts:
            node_ids.add(
                f"{session_id}/unit_score/"
                f"block:{int(artifact['payload']['block_index'])}"
            )
    rewrite_stage = result.stage_results.get("rewrite", {})
    for session_id, blocks in rewrite_stage.items():
        for block in blocks:
            for artifact in block["rewrites"]:
                payload = artifact["payload"]
                node_ids.add(
                    f"{session_id}/rewrite/block:{int(block['block_index'])}/"
                    f"unit:{payload['unit_id']}"
                )
    merge_stage = result.stage_results.get("summary_merge", {})
    for session_id, value in merge_stage.items():
        for artifact in value["blocks"]:
            node_ids.add(
                f"{session_id}/summary_merge/"
                f"block:{int(artifact['payload']['block_index'])}"
            )
        node_ids.add(f"{session_id}/summary_merge/final")
    return node_ids


def run_self_check(result: RunResult) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []
    expected_sessions = set(result.summaries)

    stage_sets = {
        stage: set(result.stage_results.get(stage, {}).keys())
        for stage in STAGES
    }
    stage_sets_ok = all(value == expected_sessions for value in stage_sets.values())
    checks.append(
        {
            "name": "four_stage_session_sets_equal",
            "ok": stage_sets_ok,
            "details": {
                stage: sorted(session_ids)
                for stage, session_ids in stage_sets.items()
            },
        }
    )

    score_details_ok = True
    score_failures: List[Dict[str, Any]] = []
    for session_id, artifacts in result.stage_results.get("unit_score", {}).items():
        for artifact in artifacts:
            payload = artifact["payload"]
            for entry in payload["unit_scores"]:
                total = quantize(
                    sum(
                        float(component["contribution"])
                        for component in entry["components"].values()
                    ),
                    payload["precision"],
                )
                if total != float(entry["total_score"]):
                    score_details_ok = False
                    score_failures.append(
                        {
                            "session_id": session_id,
                            "unit_id": entry["unit_id"],
                            "recomputed": total,
                            "stored": entry["total_score"],
                        }
                    )
    checks.append(
        {
            "name": "score_total_recomputes_from_details",
            "ok": score_details_ok,
            "details": {"failures": score_failures},
        }
    )

    merge_failures: List[Dict[str, Any]] = []
    for session_id, value in result.stage_results.get(
        "summary_merge", {}
    ).items():
        payload = value["final"]["payload"]
        recomputed = recompute_quality_from_details(
            payload, payload["precision"]
        )
        stored = {
            "summary_text": payload["summary_text"],
            "unit_count": payload["quality"]["unit_count"],
            "summary_token_count": payload["quality"]["summary_token_count"],
            "compression_ratio": payload["quality"]["compression_ratio"],
            "coverage_ratio": payload["quality"]["coverage_ratio"],
            "average_unit_score": payload["quality"]["average_unit_score"],
            "quality_score": payload["quality"]["quality_score"],
            "unique_summary_terms": payload["quality"]["unique_summary_terms"],
        }
        if canonical_bytes(recomputed) != canonical_bytes(stored):
            merge_failures.append(
                {
                    "session_id": session_id,
                    "recomputed": recomputed,
                    "stored": stored,
                }
            )
    checks.append(
        {
            "name": "merge_recomputes_from_unit_details",
            "ok": not merge_failures,
            "details": {"failures": merge_failures},
        }
    )

    recomputed, reused = result.node_sets()
    resumed_nodes = _stage_node_ids(result) - (recomputed | reused)
    reused_all = reused | resumed_nodes
    all_nodes = _stage_node_ids(result)
    disjoint = not bool(recomputed & reused_all)
    coverage = (recomputed | reused_all) == all_nodes
    checks.append(
        {
            "name": "node_partition_disjoint_and_complete",
            "ok": disjoint and coverage,
            "details": {
                "recomputed_count": len(recomputed),
                "reused_count": len(reused_all),
                "resumed_committed_count": len(resumed_nodes),
                "all_node_count": len(all_nodes),
                "overlap": sorted(recomputed & reused_all),
                "uncovered": sorted(all_nodes - (recomputed | reused_all)),
            },
        }
    )

    return {
        "ok": all(check["ok"] for check in checks),
        "summary_digest": result.summary_digest,
        "checks": checks,
    }
