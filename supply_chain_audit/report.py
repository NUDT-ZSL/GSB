"""JSON report assembly."""

from __future__ import annotations

from typing import Any

from .io_utils import digest_value


def build_closure_report(view: dict[str, Any], source_key: str | None = None) -> dict[str, Any]:
    invalidation_index = view.get("invalidation_index", {})
    chains = []
    for affected_key in sorted(invalidation_index):
        for entry in invalidation_index[affected_key]:
            if source_key and entry.get("source_key") != source_key:
                continue
            chains.append(
                {
                    "affected_key": affected_key,
                    "source_key": entry["source_key"],
                    "level": entry["level"],
                    "chain": entry["chain"],
                    "source_to_dependent_path": entry.get("source_to_dependent_path"),
                }
            )

    artifacts = []
    for key in sorted(view.get("artifacts", {})):
        artifact = view["artifacts"][key]
        if source_key:
            related = any(item.get("source_key") == source_key for item in artifact.get("invalidations", []))
            if not related and key != source_key:
                continue
        artifacts.append(artifact)

    closure_sets = view.get("closure_sets", {})
    if source_key:
        closure_sets = {source_key: closure_sets.get(source_key, [])}

    return {
        "schema": "supply-chain-audit/closure-report/v1",
        "view_digest": digest_value(view),
        "policy_versions": view.get("policy_versions"),
        "source_key_filter": source_key,
        "summary": {
            **view.get("stats", {}),
            "closure_chain_count": len(chains),
            "reported_artifact_count": len(artifacts),
        },
        "closure_sets": closure_sets,
        "closure_chains": chains,
        "artifacts": artifacts,
        "conflicts": view.get("conflicts", []),
        "cycles": view.get("cycles", []),
        "rejections": view.get("rejections", []),
    }


def build_query_result(view: dict[str, Any], key: str | None = None, status: str | None = None) -> dict[str, Any]:
    artifacts = list(view.get("artifacts", {}).values())
    if key:
        artifacts = [artifact for artifact in artifacts if artifact["artifact_key"] == key]
    if status:
        artifacts = [artifact for artifact in artifacts if artifact["status"] == status]
    return {
        "schema": "supply-chain-audit/query-view/v1",
        "view_digest": digest_value(view),
        "policy_versions": view.get("policy_versions"),
        "filters": {"artifact_key": key, "status": status},
        "count": len(artifacts),
        "artifacts": artifacts,
        "conflicts": view.get("conflicts", []),
        "cycles": view.get("cycles", []),
    }
