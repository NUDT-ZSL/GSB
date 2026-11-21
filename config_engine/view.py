"""Deterministic cross-format merge and point-in-time unified view."""

from __future__ import annotations

import copy
from typing import Any, Iterable

from .canonical import sha256_value
from .models import Candidate, version_key


def _summary(candidate: Candidate, *, state: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id,
        "content_digest": candidate.content_digest,
        "effective_at_ms": candidate.effective_at_ms,
        "expires_at_ms": candidate.expires_at_ms,
        "generation": candidate.generation,
        "logical_clock_ms": candidate.logical_clock_ms,
        "source_format": candidate.source_format,
        "source_priority": candidate.source_priority,
        "state": state,
        "tombstone": candidate.tombstone,
        "upstreams": list(candidate.upstreams),
        "version": candidate.version,
    }


def _active_config(candidate: Candidate) -> dict[str, Any]:
    decision = {
        "basis": "latest_effective_generation",
        "candidate_decision": copy.deepcopy(candidate.decision),
        "generation": candidate.generation,
        "logical_clock_ms": candidate.logical_clock_ms,
        "rule": "max(generation,logical_clock,effective_at,source_priority,version)",
        "selected_candidate_id": candidate.candidate_id,
        "source_format": candidate.source_format,
    }
    provenance = {}
    for key in sorted(candidate.params):
        provenance[key] = {
            "content_digest": candidate.content_digest,
            "decision_basis": decision,
            "original_time": copy.deepcopy(candidate.original_time),
            "source_format": candidate.source_format,
            "source_priority": candidate.source_priority,
            "source_version": candidate.version,
        }
    return {
        "candidate_id": candidate.candidate_id,
        "content_digest": candidate.content_digest,
        "decision": decision,
        "effective_at_ms": candidate.effective_at_ms,
        "expires_at_ms": candidate.expires_at_ms,
        "generation": candidate.generation,
        "logical_clock_ms": candidate.logical_clock_ms,
        "param_provenance": provenance,
        "params": copy.deepcopy(candidate.params),
        "source_format": candidate.source_format,
        "tombstone": candidate.tombstone,
        "upstreams": list(candidate.upstreams),
        "version": candidate.version,
    }


def _proposal_reason(winner: Candidate, contender: Candidate) -> str | None:
    if contender.logical_clock_ms < winner.logical_clock_ms:
        return "LOWER_LOGICAL_CLOCK"
    if contender.logical_clock_ms > winner.logical_clock_ms:
        return None
    if contender.generation < winner.generation:
        return "LOWER_GENERATION"
    if contender.generation > winner.generation:
        return None
    if contender.source_priority < winner.source_priority:
        return "LOWER_SOURCE_PRIORITY"
    if contender.source_priority > winner.source_priority:
        return None
    if contender.effective_at_ms < winner.effective_at_ms:
        return "EARLIER_EFFECTIVE_AT"
    if contender.effective_at_ms > winner.effective_at_ms:
        return None
    if version_key(contender.version) < version_key(winner.version):
        return "LOWER_VERSION"
    if version_key(contender.version) > version_key(winner.version):
        return None
    if contender.content_digest < winner.content_digest:
        return "DETERMINISTIC_TIEBREAK"
    return None


def build_arbitration(
    candidates: Iterable[Candidate],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Build cross-format arbitration over manifest-origin candidates only."""

    grouped: dict[tuple[str, str], list[Candidate]] = {}
    for candidate in candidates:
        if candidate.origin == "base":
            grouped.setdefault(candidate.target, []).append(candidate)
    report: list[dict[str, Any]] = []
    decisions: dict[str, dict[str, Any]] = {}
    for target in sorted(grouped):
        items = sorted(grouped[target], key=lambda item: item.proposal_key, reverse=True)
        winner = items[0]
        entries = []
        for item in items:
            reason = None if item is winner else _proposal_reason(winner, item)
            entries.append(
                {
                    "candidate_id": item.candidate_id,
                    "effective_at_ms": item.effective_at_ms,
                    "generation": item.generation,
                    "logical_clock_ms": item.logical_clock_ms,
                    "reason": reason,
                    "source_format": item.source_format,
                    "version": item.version,
                    "winner": item is winner,
                }
            )
            decisions[item.candidate_id] = {
                "basis": "cross_format_arbitration",
                "reason": reason,
                "rule": (
                    "max(logical_clock_ms,generation,source_format_priority,"
                    "effective_at_ms,version,digest)"
                ),
                "winner": item is winner,
                "winner_candidate_id": winner.candidate_id,
            }
        report.append(
            {
                "candidates": entries,
                "environment": target[1],
                "service": target[0],
                "winner_candidate_id": winner.candidate_id,
                "winner_version": winner.version,
            }
        )
    return report, decisions


def build_view(
    candidates: Iterable[Candidate],
    *,
    clock_ms: int,
) -> dict[str, Any]:
    all_candidates = sorted(
        (item for item in candidates if item.accepted),
        key=lambda item: (
            item.service,
            item.environment,
            item.generation,
            item.proposal_key,
            item.candidate_id,
        ),
    )
    arbitration, arbitration_decisions = build_arbitration(all_candidates)
    grouped: dict[tuple[str, str], list[Candidate]] = {}
    for candidate in all_candidates:
        grouped.setdefault(candidate.target, []).append(candidate)

    services: list[dict[str, Any]] = []
    effective_services: list[dict[str, Any]] = []
    for target in sorted(grouped):
        target_candidates = grouped[target]
        ordered_candidates = sorted(target_candidates, key=lambda item: item.timeline_key)
        effective = [
            item for item in ordered_candidates if item.effective_at_ms <= clock_ms
        ]
        future = [
            item for item in ordered_candidates if item.effective_at_ms > clock_ms
        ]
        effective_leader = effective[-1] if effective else None

        active: Candidate | None = None
        state: str
        state_reason: str
        if effective_leader is None:
            state = "pending"
            state_reason = "first_configuration_is_not_yet_effective"
        elif effective_leader.tombstone:
            state = "tombstoned"
            state_reason = "latest_effective_generation_is_a_tombstone"
        elif (
            effective_leader.expires_at_ms is not None
            and effective_leader.expires_at_ms <= clock_ms
        ):
            state = "expired"
            state_reason = "latest_effective_generation_has_expired"
        else:
            state = "active"
            state_reason = "latest_effective_generation_is_active"
            active = effective_leader

        history = []
        for item in ordered_candidates:
            if item.effective_at_ms > clock_ms:
                history_state = "pending"
            elif item.tombstone:
                history_state = "tombstoned"
            elif item.expires_at_ms is not None and item.expires_at_ms <= clock_ms:
                history_state = "expired"
            elif active is not None and item.candidate_id == active.candidate_id:
                history_state = "active"
            else:
                history_state = "expired"
            history.append(_summary(item, state=history_state))

        tombstones = [
            _summary(item, state="pending" if item.effective_at_ms > clock_ms else "tombstoned")
            for item in sorted(target_candidates, key=lambda item: item.timeline_key)
            if item.tombstone
        ]
        latest_proposal = max(target_candidates, key=lambda item: item.proposal_key)
        entry = {
            "active": active is not None,
            "active_config": _active_config(active) if active is not None else None,
            "environment": target[1],
            "future_versions": [_summary(item, state="pending") for item in future],
            "generation": latest_proposal.generation,
            "history": history,
            "latest_proposal": _summary(
                latest_proposal,
                state=(
                    "pending"
                    if latest_proposal.effective_at_ms > clock_ms
                    else "tombstoned"
                    if latest_proposal.tombstone
                    else "expired"
                    if latest_proposal.expires_at_ms is not None
                    and latest_proposal.expires_at_ms <= clock_ms
                    else "active"
                ),
            ),
            "service": target[0],
            "state": state,
            "state_reason": state_reason,
            "tombstones": tombstones,
        }
        services.append(entry)
        if active is not None:
            effective_services.append(
                {
                    "config": _active_config(active),
                    "environment": target[1],
                    "service": target[0],
                }
            )

    base = {
        "arbitration": arbitration,
        "clock_ms": clock_ms,
        "effective_services": effective_services,
        "schema_version": 1,
        "services": services,
    }
    view_version = sha256_value(base)
    payload = dict(base)
    payload["view_version"] = view_version
    return payload


def service_state_map(payload: dict[str, Any] | None) -> dict[tuple[str, str], str]:
    if payload is None:
        return {}
    return {
        (entry["service"], entry["environment"]): entry["state"]
        for entry in payload.get("services", [])
    }


def active_map(payload: dict[str, Any] | None) -> dict[tuple[str, str], str | None]:
    if payload is None:
        return {}
    return {
        (entry["service"], entry["environment"]): (
            entry["active_config"]["candidate_id"] if entry.get("active_config") else None
        )
        for entry in payload.get("services", [])
    }


def view_summary(payload: dict[str, Any]) -> dict[str, Any]:
    counts = {"active": 0, "expired": 0, "pending": 0, "tombstoned": 0}
    for service in payload.get("services", []):
        counts[service["state"]] = counts.get(service["state"], 0) + 1
    return {
        "clock_ms": payload["clock_ms"],
        "effective_service_count": len(payload.get("effective_services", [])),
        "service_count": len(payload.get("services", [])),
        "state_counts": counts,
        "view_version": payload["view_version"],
    }
