"""Structured report and dependency-graph construction."""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Sequence

from . import codes
from .models import (
    AnalysisConfig,
    ConnectionResult,
    Diagnostic,
    TrustView,
    digest_value,
)


def _graph(results: Sequence[ConnectionResult], trust: TrustView) -> Dict[str, Any]:
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: set[tuple[str, str, str]] = set()

    for fingerprint, anchor in sorted(trust.entities.items()):
        node_id = f"anchor:{fingerprint}"
        nodes[node_id] = {
            "id": node_id,
            "kind": "trust_anchor",
            "fingerprint": fingerprint,
            "alias": anchor.canonical_alias,
            "generation": anchor.generation,
            "effective_ms": anchor.effective_ms,
            "enabled": anchor.enabled,
            "trusted": anchor.trusted,
            "tombstone": anchor.tombstone,
            "revoked": anchor.revoked,
            "authoritative_source": anchor.authoritative_source,
        }

    for result in sorted(results, key=lambda r: r.connection_id):
        conn_node = f"connection:{result.connection_id}"
        nodes[conn_node] = {
            "id": conn_node,
            "kind": "connection",
            "connection_id": result.connection_id,
            "status": result.status,
            "trust_conclusion": result.trust_conclusion,
        }
        for evaluation in result.candidate_chains:
            chain_node = f"chain:{result.connection_id}:{evaluation.candidate_id}"
            nodes[chain_node] = {
                "id": chain_node,
                "kind": "candidate_chain",
                "connection_id": result.connection_id,
                "candidate_id": evaluation.candidate_id,
                "selected": evaluation is result.selected_chain,
                "trusted": evaluation.trusted,
                "root_fingerprint": evaluation.root_fingerprint,
                "root_generation": evaluation.root_generation,
            }
            edges.add(
                (
                    conn_node,
                    chain_node,
                    "selected_chain" if evaluation is result.selected_chain else "candidate_chain",
                )
            )
            for index, cert in enumerate(evaluation.certificates):
                cert_node = f"cert:{cert.public_key_fingerprint}"
                nodes.setdefault(
                    cert_node,
                    {
                        "id": cert_node,
                        "kind": "certificate",
                        "fingerprint": cert.public_key_fingerprint,
                        "subject": cert.subject,
                        "issuer": cert.issuer,
                        "not_before_ms": cert.not_before_ms,
                        "not_after_ms": cert.not_after_ms,
                    },
                )
                edges.add((chain_node, cert_node, f"contains_at_{index}"))
                if index + 1 < len(evaluation.certificates):
                    next_cert = evaluation.certificates[index + 1]
                    edges.add(
                        (
                            cert_node,
                            f"cert:{next_cert.public_key_fingerprint}",
                            "issued_by",
                        )
                    )
            if evaluation.root_fingerprint:
                anchor_node = f"anchor:{evaluation.root_fingerprint}"
                if anchor_node in nodes:
                    edges.add(
                        (
                            f"cert:{evaluation.root_fingerprint}",
                            anchor_node,
                            "anchored_by" if evaluation.root_status == "trusted" else "anchor_rejected",
                        )
                    )
                else:
                    edges.add(
                        (
                            f"cert:{evaluation.root_fingerprint}",
                            "anchor:missing",
                            "untrusted_root",
                        )
                    )
                    nodes.setdefault(
                        "anchor:missing",
                        {"id": "anchor:missing", "kind": "missing_anchor"},
                    )
    return {
        "nodes": [nodes[key] for key in sorted(nodes)],
        "edges": [
            {"from": source, "to": target, "relation": relation}
            for source, target, relation in sorted(edges)
        ],
    }


def build_report(
    *,
    input_info: Dict[str, Any],
    trust_view: TrustView,
    results: Sequence[ConnectionResult],
    propagation: Sequence[Dict[str, Any]],
    config: AnalysisConfig,
    checkpoint_info: Dict[str, Any],
    input_diagnostics: Sequence[Diagnostic],
    trust_version_change: Dict[str, Any],
) -> Dict[str, Any]:
    ordered_results = sorted(results, key=lambda r: r.connection_id)
    all_issues: List[Diagnostic] = list(input_diagnostics) + list(trust_view.diagnostics)
    for result in ordered_results:
        all_issues.extend(result.issues)
    code_counts = Counter(i.code for i in all_issues)
    error_counts = Counter(i.code for i in all_issues if i.severity == "error")
    status_counts = Counter(result.status for result in ordered_results)
    conclusion_counts = Counter(result.trust_conclusion for result in ordered_results)

    stable_summary = {
        "trust_view_version": trust_view.version,
        "merge_policy_version": trust_view.policy_version,
        "connections": [
            {
                "connection_id": result.connection_id,
                "status": result.status,
                "trust_conclusion": result.trust_conclusion,
                "primary_code": result.primary_code,
                "conclusion_digest": result.conclusion_digest,
            }
            for result in ordered_results
        ],
        "propagation": list(propagation),
    }
    conclusion_digest = digest_value(stable_summary)

    summary = {
        "connections_total": len(ordered_results),
        "connections_trusted": sum(
            1 for r in ordered_results if r.trust_conclusion == "trusted"
        ),
        "connections_untrusted": sum(
            1
            for r in ordered_results
            if r.trust_conclusion
            in (
                "untrusted_root",
                "broken_chain",
                "empty_chain",
                "root_inactive",
                "loop",
                "untrusted",
            )
        ),
        "connections_revoked": sum(
            1
            for r in ordered_results
            if r.trust_conclusion
            in ("revoked_leaf", "revoked_intermediate", "root_revoked")
        ),
        "connections_expired": sum(
            1 for r in ordered_results if r.trust_conclusion == "expired"
        ),
        "connections_indeterminate": sum(
            1 for r in ordered_results if r.trust_conclusion == "indeterminate"
        ),
        "status_counts": dict(sorted(status_counts.items())),
        "trust_conclusion_counts": dict(sorted(conclusion_counts.items())),
        "code_counts": dict(sorted(code_counts.items())),
        "error_code_counts": dict(sorted(error_counts.items())),
        "trusted_anchor_count": len(trust_view.trusted),
        "revoked_anchor_count": len(trust_view.revocations),
        "tombstone_count": len(trust_view.tombstones),
        "conclusion_digest": conclusion_digest,
    }

    trust_block = trust_view.summary()
    trust_block["version_change"] = trust_version_change
    trust_block["diagnostics"] = [d.to_dict() for d in trust_view.diagnostics]

    return {
        "schema_version": codes.REPORT_SCHEMA_VERSION,
        "engine": {
            "name": codes.ENGINE_NAME,
            "version": codes.ENGINE_VERSION,
            "deterministic": True,
            "clock_policy": "sample_declared_observed_time_only",
        },
        "config": config.to_dict(),
        "inputs": input_info,
        "input_diagnostics": [d.to_dict() for d in input_diagnostics],
        "trust": trust_block,
        "connections": [r.to_dict() for r in ordered_results],
        "propagation": list(propagation),
        "dependency_graph": _graph(ordered_results, trust_view),
        "summary": summary,
        "checkpoint": checkpoint_info,
    }


def report_bytes(report: Dict[str, Any]) -> bytes:
    import json

    return (
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
