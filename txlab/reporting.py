"""Stable human-readable summaries for traces and demonstration output."""

from __future__ import annotations

from typing import Any

from .util import canonical_json


def format_run_summary(result: dict[str, Any], *, include_events: bool = True) -> str:
    code = result.get("code") or "OK"
    lines = [
        f"SCRIPT {result['script_name']} | STATUS {code} | "
        f"DATA_VERSION {result['final_data_version']} | "
        f"ISOLATION_OK {str(result['isolation_ok']).lower()}",
        f"CONFIG budget={result['config']['lock_wait_budget_ms']}ms "
        f"concurrency={result['config']['max_concurrency']} "
        f"tick={result['config']['tick_ms']}ms",
        "SESSIONS",
    ]
    for session in result.get("sessions", []):
        reads = ",".join(
            f"{item['row_key']}={canonical_json(item['payload'])}"
            f"@{item['value_version']}" for item in session.get("reads", [])
        ) or "-"
        writes = ",".join(sorted(session.get("committed_writes", {}))) or "-"
        lines.append(
            "  "
            f"{session['session_id']} | {session['isolation']} | "
            f"{session['outcome']} | code={session['code'] or '-'} | "
            f"wait={session['wait_budget_used_ms']}ms | "
            f"start={session['started_at_ms']} end={session['ended_at_ms']} | "
            f"reads={reads} | committed_writes={writes}"
        )

    deadlocks = result.get("deadlocks", [])
    lines.append(
        "DEADLOCKS "
        + (
            "none"
            if not deadlocks
            else canonical_json(
                [
                    {
                        "rule": item["rule"],
                        "cycle_members": item["cycle_members"],
                        "victim": item["victim_session_id"],
                    }
                    for item in deadlocks
                ]
            )
        )
    )
    lines.append(
        "ISOLATION "
        f"serialization_order={result['serialization_order']} "
        f"expected_version={result['expected_data_version']} "
        f"actual_version={result['final_data_version']} "
        f"expected_digest={result['expected_digest']} "
        f"actual_digest={result['actual_digest']}"
    )
    lines.append(
        "SUMMARY "
        f"sessions={result['summary']['sessions']} "
        f"committed={result['summary']['committed']} "
        f"rolled_back={result['summary']['rolled_back']} "
        f"timeouts={result['summary']['timeouts']} "
        f"deadlocks={result['summary']['deadlocks']}"
    )

    if include_events:
        lines.append("EVENTS")
        for event in result.get("events", []):
            lines.append("  " + format_event(event))
    return "\n".join(lines)


def format_event(event: dict[str, Any]) -> str:
    session = event.get("session_id") or "-"
    line = (
        f"+{event.get('ts_ms', 0):>4}ms "
        f"#{event.get('seq', 0):03d} "
        f"{event.get('type', 'event'):<22} {session}"
    )
    details = []
    for key in (
        "op_index",
        "row_key",
        "mode",
        "queue_position",
        "blockers",
        "waited_ms",
        "wait_budget_ms",
        "code",
        "reason",
        "data_version",
        "source",
        "isolation",
        "scope",
        "cycle_members",
        "victim_session_id",
        "rule",
    ):
        if key in event and event[key] is not None:
            value = event[key]
            if isinstance(value, (dict, list, tuple)):
                value = canonical_json(value)
            details.append(f"{key}={value}")
    if "payload" in event:
        details.append(f"payload={canonical_json(event['payload'])}")
    if details:
        line += " | " + " ".join(details)
    return line


def format_replay_report(report: dict[str, Any]) -> str:
    lines = [
        f"REPLAY {report.get('snapshot_name')} | STATUS {report.get('code')} | "
        f"PARAMETERS_MATCH {str(not report.get('parameter_mismatch')).lower()}",
        f"  diverged_sessions={report.get('diverged_sessions')}",
        f"  events_equal={report.get('event_comparison', {}).get('equal')} "
        f"expected={report.get('event_comparison', {}).get('expected_event_count')} "
        f"actual={report.get('event_comparison', {}).get('actual_event_count')}",
    ]
    for item in report.get("outcome_comparison", []):
        if not item["outcome_equal"] or not item["session_events_equal"]:
            lines.append(
                "  "
                f"{item['session_id']} expected={item['expected_outcome']}/"
                f"{item['expected_code'] or '-'} actual={item['actual_outcome']}/"
                f"{item['actual_code'] or '-'} "
                f"events_equal={str(item['session_events_equal']).lower()}"
            )
    return "\n".join(lines)

