"""Deterministic proxy forwarding state machine."""

from __future__ import annotations

from typing import Any

STAGE_ORDER = [
    "declared",
    "request_arrived",
    "table_lookup",
    "origin_fetch",
    "response_returned",
    "tombstone_applied",
    "failed",
    "completed",
]


def build_transitions(
    session: dict[str, Any],
    *,
    tombstoned: bool,
    table_version_created_clock: int | None,
    replay_clock_ms: int,
    outcome: str | None = None,
) -> list[dict[str, Any]]:
    records = sorted(session.get("records", []), key=lambda item: int(item["seq"]))
    forwards = [record for record in records if record["direction"] == "forward"]
    reverses = [record for record in records if record["direction"] == "reverse"]
    transitions: list[dict[str, Any]] = []

    def add(stage: str, clock_ms: int | None, basis: str) -> None:
        transitions.append({"stage": stage, "at_clock_ms": clock_ms, "basis": basis})

    if records:
        add("declared", min(int(r["logical_clock_ms"]) for r in records), "unified_session_view")
    if forwards:
        first = forwards[0]
        add("request_arrived", int(first["logical_clock_ms"]), f"seq={first['seq']},hop={first['hop']}")
        add("table_lookup", int(first["logical_clock_ms"]), f"route_refs={session.get('route_keys', [])}")
        last_forward = forwards[-1]
        add("origin_fetch", int(last_forward["logical_clock_ms"]), f"hop={last_forward['hop']}")
    if tombstoned:
        add(
            "tombstone_applied",
            table_version_created_clock if table_version_created_clock is not None else replay_clock_ms,
            "selected_route_has_valid_tombstone",
        )
        return transitions
    if outcome == "failed":
        add("failed", replay_clock_ms, "loopback_replay_returned_failure_status_or_digest_mismatch")
        return transitions
    if reverses:
        first_reverse = reverses[0]
        add("response_returned", int(first_reverse["logical_clock_ms"]), f"seq={first_reverse['seq']},hop={first_reverse['hop']}")
    add("completed", replay_clock_ms, "all_deterministic_hops_replayed")
    return transitions
