"""Stable diagnostic projections shared by CLI output and the Tkinter view."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .session import AlarmSession
from .util import pretty_json


def _condition_text(condition: Mapping[str, Any]) -> str:
    return f"{condition['metric']} {condition['operator']} {condition['threshold']}"


def build_diagnostic_model(
    session: AlarmSession,
    *,
    increment: Mapping[str, Any] | None = None,
    restore: Mapping[str, Any] | None = None,
    snapshots: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    selected_increment = increment or session.diagnostic_trace()
    clusters: list[dict[str, Any]] = []
    for analysis in session.analyses:
        cluster = analysis["cluster"]
        root = analysis["root_cause"]
        dedup = analysis["deduplicated"]
        clusters.append(
            {
                "cluster_id": analysis["cluster_id"],
                "members": list(cluster["members"]),
                "services": list(cluster["services"]),
                "start_ts": cluster["start_ts"],
                "end_ts": cluster["end_ts"],
                "root_candidate": root["selected_service"],
                "root_reason": root["reason"],
                "candidates": [
                    {
                        "rank": candidate["rank"],
                        "service": candidate["service"],
                        "in_degree": candidate["in_degree"],
                        "cluster_in_degree": candidate["cluster_in_degree"],
                        "severity": candidate["severity"],
                        "earliest_ts": candidate["earliest_ts"],
                        "support_alerts": list(candidate["support_alerts"]),
                        "reason": candidate["reason"],
                    }
                    for candidate in root["candidates"]
                ],
                "retained_alerts": list(dedup["retained_alerts"]),
                "folded_alerts": list(dedup["folded_alerts"]),
                "retained_count": dedup["retained_count"],
                "folded_count": dedup["folded_count"],
            }
        )
    invalidation = {
        "latest_trace": dict(selected_increment) if selected_increment else None,
        "history": [dict(item) for item in session.invalidation_trace],
    }
    snapshot_section: dict[str, Any] | None = None
    if restore is not None:
        snapshot_section = {
            "version_diff": restore.get("version_diff"),
            "replay": restore.get("replay"),
            "whole_state_replaced": restore.get("whole_state_replaced"),
            "incremental_merge_used": restore.get("incremental_merge_used"),
        }
    return {
        "schema": "alarm-diagnostic-v1",
        "versions": {
            "rules": session.rules_version,
            "topology": session.topology_version,
        },
        "window_ms": session.window_ms,
        "event_cursor": session.event_cursor,
        "event_count": len(session.events),
        "cluster_count": len(session.analyses),
        "cluster_summary_hash": session.cluster_summary_hash,
        "analysis_hash": session.analysis_hash,
        "rules": [
            {
                "rule_id": rule.rule_id,
                "service": rule.service,
                "severity": rule.severity,
                "condition": _condition_text(rule.condition),
            }
            for rule in session.rules
        ],
        "edges": [
            {
                "upstream": edge.upstream,
                "downstream": edge.downstream,
                "weight": edge.weight,
            }
            for edge in session.topology.edges
        ],
        "clusters": clusters,
        "invalidation": invalidation,
        "snapshot": snapshot_section,
        "snapshots": [dict(item) for item in snapshots],
    }


def render_diagnostic(model: Mapping[str, Any]) -> str:
    return pretty_json(model)
