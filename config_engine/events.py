"""Local change-stream loading and deterministic two-source event adjudication."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import (
    digest_token,
    is_int,
    load_json_file,
    sha256_value,
    validate_flat_params,
    validate_name,
)
from .db import (
    get_event_commit,
    insert_config_version,
    insert_event_commit,
    list_candidates,
    max_generation,
    set_candidate_acceptance,
    update_event_status,
)
from .errors import EngineError, error
from .models import (
    Candidate,
    candidate_id,
    compare_candidate,
    content_digest,
    version_key,
)
from .validation import Package


@dataclass
class EventStream:
    source: str
    path: Path
    events: list[dict[str, Any]]


def load_event_stream(path: Path, expected_source: str) -> EventStream:
    document = load_json_file(path)
    if not isinstance(document, dict):
        raise error(
            "INVALID_EVENT",
            "event stream must be a JSON object",
            path=str(path),
            source=expected_source,
        )
    if document.get("schema_version", 1) != 1:
        raise error(
            "INVALID_EVENT",
            "event stream schema_version must be 1",
            source=expected_source,
            path=str(path),
        )
    if document.get("source") != expected_source:
        raise error(
            "INVALID_EVENT",
            "event stream source does not match package key",
            expected_source=expected_source,
            actual_source=document.get("source"),
            path=str(path),
        )
    events = document.get("events")
    if not isinstance(events, list):
        raise error(
            "INVALID_EVENT",
            "event stream events must be an array",
            source=expected_source,
            path=str(path),
        )
    result: list[dict[str, Any]] = []
    seen_seq: dict[int, str] = {}
    seen_id: dict[str, int] = {}
    for index, raw in enumerate(events):
        if not isinstance(raw, dict):
            raise error(
                "INVALID_EVENT",
                "event entry must be an object",
                source=expected_source,
                index=index,
            )
        seq = raw.get("seq")
        if not is_int(seq) or seq <= 0:
            raise error(
                "EVENT_SEQUENCE_INVALID",
                "event seq must be a positive integer",
                source=expected_source,
                index=index,
                seq=seq,
            )
        event_id = raw.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise error(
                "INVALID_EVENT",
                "event_id must be a non-empty string",
                source=expected_source,
                index=index,
                seq=seq,
            )
        clock = raw.get("logical_clock_ms")
        if not is_int(clock) or clock < 0:
            raise error(
                "CLOCK_INVALID",
                "event logical_clock_ms must be a non-negative integer",
                source=expected_source,
                index=index,
                seq=seq,
                value=clock,
            )
        if seq in seen_seq:
            raise error(
                "EVENT_SEQUENCE_INVALID",
                "event stream has duplicate seq",
                source=expected_source,
                seq=seq,
                event_id=event_id,
                first_event_id=seen_seq[seq],
            )
        if event_id in seen_id:
            raise error(
                "EVENT_SEQUENCE_INVALID",
                "event stream has duplicate event_id",
                source=expected_source,
                event_id=event_id,
                first_seq=seen_id[event_id],
                seq=seq,
            )
        seen_seq[seq] = event_id
        seen_id[event_id] = seq
        item = dict(raw)
        item["source"] = expected_source
        item["_file"] = path.name
        item["_index"] = index
        result.append(item)
    result.sort(key=lambda item: item["seq"])
    return EventStream(source=expected_source, path=path, events=result)


def eligible_events(
    streams: list[EventStream],
    *,
    clock_ms: int,
    committed: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Select events in deterministic global order; defer future clock/steps."""

    eligible: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    for stream in streams:
        blocked = False
        for event in stream.events:
            if blocked or event["logical_clock_ms"] > clock_ms:
                blocked = True
                deferred.append(
                    {
                        "event_id": event["event_id"],
                        "logical_clock_ms": event["logical_clock_ms"],
                        "reason": (
                            "EVENT_CLOCK_AHEAD"
                            if event["logical_clock_ms"] > clock_ms
                            else "PRECEDING_EVENT_DEFERRED"
                        ),
                        "seq": event["seq"],
                        "source": stream.source,
                    }
                )
                continue
            eligible.append(event)
    eligible.sort(
        key=lambda item: (
            item["logical_clock_ms"],
            -int(item.get("_source_priority", 0)),
            item["source"],
            item["seq"],
        )
    )
    return eligible, deferred


def _event_generation(event: dict[str, Any], default: int) -> int:
    value = event.get("generation", default)
    if not is_int(value) or value < 1:
        raise error(
            "INVALID_EVENT",
            "event generation must be a positive integer",
            event_id=event["event_id"],
            field="generation",
            value=value,
        )
    return value


def _event_effective(event: dict[str, Any], default: int | None) -> int:
    value = event.get("effective_at_ms", default)
    if not is_int(value) or value < 0:
        raise error(
            "INVALID_EFFECTIVE_AT",
            "event effective_at_ms must be an integer millisecond timestamp",
            event_id=event["event_id"],
            field="effective_at_ms",
            value=value,
        )
    return value


def _event_expires(event: dict[str, Any], default: int | None, effective: int) -> int | None:
    value = event.get("expires_at_ms", default)
    if value is None:
        return None
    if not is_int(value) or value < 0:
        raise error(
            "INVALID_EVENT",
            "event expires_at_ms must be an integer millisecond timestamp",
            event_id=event["event_id"],
            field="expires_at_ms",
            value=value,
        )
    if value <= effective:
        raise error(
            "INVALID_EVENT",
            "event expires_at_ms must be greater than effective_at_ms",
            event_id=event["event_id"],
            effective_at_ms=effective,
            expires_at_ms=value,
        )
    return value


def _event_upstreams(event: dict[str, Any], default: list[str] | None) -> list[str]:
    raw = event.get("upstreams", default or [])
    if not isinstance(raw, list):
        raise error(
            "INVALID_EVENT",
            "event upstreams must be an array",
            event_id=event["event_id"],
            field="upstreams",
        )
    result: list[str] = []
    for index, value in enumerate(raw):
        name = validate_name(value, f"upstreams[{index}]")
        if name not in result:
            result.append(name)
    return sorted(result)


def _latest_by_target(candidates: list[Candidate]) -> dict[tuple[str, str], Candidate]:
    result: dict[tuple[str, str], Candidate] = {}
    for candidate in candidates:
        current = result.get(candidate.target)
        if current is None or candidate.timeline_key > current.timeline_key:
            result[candidate.target] = candidate
    return result


def _validate_event_dependencies(conn: sqlite3.Connection, candidate: Candidate) -> None:
    candidates = list_candidates(conn, accepted_only=True)
    declared = {item.target for item in candidates}
    declared.add(candidate.target)
    latest = _latest_by_target(candidates)
    latest[candidate.target] = candidate
    graph: dict[tuple[str, str], list[tuple[str, str]]] = {
        target: [] for target in sorted(declared)
    }
    for target, item in latest.items():
        if item.tombstone:
            continue
        for upstream in item.upstreams:
            upstream_target = (upstream, target[1])
            if upstream_target not in declared:
                raise error(
                    "MISSING_UPSTREAM",
                    f"upstream {upstream!r} does not exist in environment {target[1]!r}",
                    service=target[0],
                    environment=target[1],
                    upstream=upstream,
                )
            graph.setdefault(target, []).append(upstream_target)

    color: dict[tuple[str, str], int] = {}
    stack: list[tuple[str, str]] = []

    def visit(node: tuple[str, str]) -> None:
        color[node] = 1
        stack.append(node)
        for neighbor in sorted(graph.get(node, [])):
            if color.get(neighbor) == 1:
                start = stack.index(neighbor)
                cycle = stack[start:] + [neighbor]
                raise error(
                    "DEPENDENCY_CYCLE",
                    "event dependency graph contains a cycle",
                    cycle=[
                        {"environment": env, "service": service} for service, env in cycle
                    ],
                )
            if color.get(neighbor, 0) == 0:
                visit(neighbor)
        stack.pop()
        color[node] = 2

    for node in sorted(graph):
        if color.get(node, 0) == 0:
            visit(node)


def _reference_candidate(
    conn: sqlite3.Connection,
    *,
    target: tuple[str, str],
    target_version: str,
    expected_digest: str,
) -> Candidate | None:
    matches = [
        item
        for item in list_candidates(conn, target=target, accepted_only=False)
        if item.version == target_version and item.content_digest == expected_digest
    ]
    if not matches:
        return None
    matches.sort(
        key=lambda item: (
            item.generation,
            item.source_priority,
            item.effective_at_ms,
            item.candidate_id,
        ),
        reverse=True,
    )
    return matches[0]


def _event_conflict_winner(
    conn: sqlite3.Connection,
    *,
    candidate: Candidate,
) -> tuple[Candidate | None, str | None]:
    candidates = [
        item
        for item in list_candidates(conn, target=candidate.target, accepted_only=True)
        if item.origin == "event" and item.generation == candidate.generation
    ]
    if not candidates:
        return None, None
    winner = candidates[0]
    for item in candidates[1:]:
        if compare_candidate(winner, item) is None:
            winner = item
    reason = compare_candidate(winner, candidate)
    return winner, reason


def _reject(
    conn: sqlite3.Connection,
    *,
    event: dict[str, Any],
    op_id: str,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event_for_commit = dict(event)
    event_for_commit["rejection"] = {
        "code": code,
        "details": details or {},
        "message": message,
    }
    digest = event.get("content_digest")
    if not isinstance(digest, str) or not digest:
        digest = sha256_value(event)
    event_for_commit["content_digest"] = digest
    insert_event_commit(
        conn,
        source=event["source"],
        seq=event["seq"],
        event=event_for_commit,
        status="rejected",
        reason=code,
        candidate_id=None,
        op_id=op_id,
    )
    return {
        "candidate_id": None,
        "code": code,
        "details": details or {},
        "event_id": event.get("event_id"),
        "first_rejection_reason": code,
        "message": message,
        "seq": event["seq"],
        "source": event["source"],
        "status": "rejected",
    }


def process_event(
    conn: sqlite3.Connection,
    *,
    package: Package,
    event: dict[str, Any],
    op_id: str,
) -> dict[str, Any]:
    """Resolve and commit one event inside the caller's SQLite transaction."""

    source = event["source"]
    seq = event["seq"]
    existing = get_event_commit(conn, source, seq)
    if existing is not None:
        if (
            existing["event_id"] == event.get("event_id")
            and existing["content_digest"] == event.get("content_digest")
        ):
            return {
                "candidate_id": existing.get("candidate_id"),
                "event_id": existing["event_id"],
                "seq": seq,
                "source": source,
                "status": "duplicate",
            }
        raise error(
            "EVENT_SEQ_CONFLICT",
            "one event sequence number has conflicting content",
            source=source,
            seq=seq,
            committed_event_id=existing["event_id"],
            incoming_event_id=event.get("event_id"),
        )

    try:
        return _process_event_validated(conn, package=package, event=event, op_id=op_id)
    except EngineError as exc:
        if exc.code in {
            "EVENT_SEQUENCE_GAP",
            "EVENT_SEQ_CONFLICT",
            "EVENT_SEQUENCE_INVALID",
        }:
            raise
        return _reject(
            conn,
            event=event,
            op_id=op_id,
            code=exc.code,
            message=exc.message,
            details=exc.details,
        )


def _process_event_validated(
    conn: sqlite3.Connection,
    *,
    package: Package,
    event: dict[str, Any],
    op_id: str,
) -> dict[str, Any]:
    event_id = event["event_id"]
    service = validate_name(event.get("service"), "event.service")
    environment = validate_name(
        event.get("environment"), "event.environment", environment=True
    )
    target = (service, environment)
    target_version = event.get("target_version")
    if not isinstance(target_version, str) or not target_version.strip():
        raise error(
            "INVALID_EVENT",
            "event target_version must be a non-empty string",
            event_id=event_id,
            field="target_version",
        )
    expected_digest = event.get("content_digest")
    if not isinstance(expected_digest, str) or not digest_token(expected_digest):
        raise error(
            "INVALID_EVENT",
            "event content_digest must be a sha256 token",
            event_id=event_id,
            field="content_digest",
        )
    expected_digest = digest_token(expected_digest)

    base_targets = {
        item.target for item in list_candidates(conn, accepted_only=False) if item.origin == "base"
    }
    if target not in base_targets:
        raise error(
            "EVENT_TARGET_UNKNOWN",
            f"event references undeclared service/environment: {service}/{environment}",
            service=service,
            environment=environment,
            event_id=event_id,
        )

    reference = _reference_candidate(
        conn,
        target=target,
        target_version=target_version,
        expected_digest=expected_digest,
    )
    head = max_generation(conn, service, environment)
    generation = _event_generation(
        event, reference.generation if reference is not None else head + 1
    )
    tombstone_value = event.get("tombstone")
    if tombstone_value is not None and not isinstance(tombstone_value, bool):
        raise error(
            "INVALID_EVENT",
            "event tombstone must be boolean",
            event_id=event_id,
            field="tombstone",
        )
    explicit_tombstone = bool(tombstone_value) if tombstone_value is not None else None
    if explicit_tombstone is None and reference is not None:
        tombstone = reference.tombstone
    else:
        tombstone = bool(explicit_tombstone)

    if "params" in event:
        params = validate_flat_params(event["params"], "event.params")
    elif reference is not None:
        params = dict(reference.params)
    elif tombstone:
        params = {}
    else:
        raise error(
            "EVENT_CONTENT_NOT_FOUND",
            "event has no embedded params and no matching referenced candidate",
            event_id=event_id,
            service=service,
            environment=environment,
            target_version=target_version,
            content_digest=expected_digest,
        )
    if tombstone:
        params = dict(params)
        if "upstreams" in event and event["upstreams"]:
            raise error(
                "INVALID_EVENT",
                "tombstone events must not declare upstreams",
                event_id=event_id,
            )
        upstreams: list[str] = []
    else:
        upstreams = _event_upstreams(
            event, reference.upstreams if reference is not None else []
        )
    effective_default = reference.effective_at_ms if reference is not None else None
    effective_at = _event_effective(event, effective_default)
    expires_at = _event_expires(
        event,
        None if tombstone else (reference.expires_at_ms if reference is not None else None),
        effective_at,
    )

    computed_digest = content_digest(
        service=service,
        environment=environment,
        generation=generation,
        version=target_version,
        params=params,
        upstreams=upstreams,
        tombstone=tombstone,
        expires_at_ms=expires_at,
    )
    if computed_digest != expected_digest:
        raise error(
            "CONTENT_DIGEST_MISMATCH",
            "event content digest does not match resolved candidate",
            event_id=event_id,
            expected=expected_digest,
            actual=computed_digest,
        )

    source_priority = package.source_priorities.get(event["source"], 0)
    candidate = Candidate(
        candidate_id="",
        service=service,
        environment=environment,
        logical_clock_ms=event["logical_clock_ms"],
        generation=generation,
        source_format="event",
        source_priority=source_priority,
        version=target_version,
        content_digest=computed_digest,
        effective_at_ms=effective_at,
        expires_at_ms=expires_at,
        params=params,
        upstreams=upstreams,
        tombstone=tombstone,
        original_time={"effective_at_ms": effective_at, "kind": "absolute_epoch_ms"},
        decision={
            "basis": "event_conflict",
            "rule": "max(generation,version,source_priority,effective_at,event_id)",
        },
        source_ref={"event_file": event.get("_file"), "seq": seq_for_event(event)},
        origin="event",
        event_id=event_id,
        event_source=event["source"],
    )
    candidate.candidate_id = candidate_id(candidate)

    if generation < head:
        raise error(
            "LOWER_GENERATION",
            "event generation would move the service backwards",
            event_id=event_id,
            service=service,
            environment=environment,
            committed_generation=head,
            event_generation=generation,
        )

    accepted_candidates = list_candidates(conn, target=target, accepted_only=True)
    max_version = max(
        (version_key(item.version) for item in accepted_candidates),
        default=(),
    )
    if generation > head and version_key(target_version) < max_version:
        raise error(
            "LOWER_VERSION",
            "event target version would move the service backwards",
            event_id=event_id,
            service=service,
            environment=environment,
            event_version=target_version,
        )

    if generation == head:
        winner, reason = _event_conflict_winner(conn, candidate=candidate)
        if reason is not None:
            raise error(
                reason,
                "event loses deterministic conflict adjudication",
                event_id=event_id,
                service=service,
                environment=environment,
                generation=generation,
                winner_event_id=winner.event_id if winner is not None else None,
            )
    _validate_event_dependencies(conn, candidate)

    insert_config_version(conn, candidate, op_id=op_id, accepted=True)
    superseded: list[Candidate] = []
    if generation == head:
        for item in list_candidates(
            conn,
            target=target,
            accepted_only=True,
            origin="event",
        ):
            if item.generation != generation or item.candidate_id == candidate.candidate_id:
                continue
            old_reason = compare_candidate(candidate, item)
            if old_reason is not None:
                set_candidate_acceptance(conn, item.candidate_id, False)
                if item.event_source and item.event_id:
                    old_commit = get_event_commit(
                        conn, item.event_source, int(item.source_ref.get("seq", 0))
                    )
                    if old_commit is not None:
                        update_event_status(
                            conn,
                            source=item.event_source,
                            seq=int(item.source_ref.get("seq", 0)),
                            status="rejected",
                            reason=old_reason,
                        )
                superseded.append(item)

    event_for_commit = dict(event)
    event_for_commit["content_digest"] = computed_digest
    event_for_commit["candidate_id"] = candidate.candidate_id
    insert_event_commit(
        conn,
        source=event["source"],
        seq=event["seq"],
        event=event_for_commit,
        status="accepted",
        reason=None,
        candidate_id=candidate.candidate_id,
        op_id=op_id,
    )
    return {
        "candidate_id": candidate.candidate_id,
        "event_id": event_id,
        "seq": event["seq"],
        "source": event["source"],
        "status": "accepted",
        "superseded": [
            {
                "candidate_id": item.candidate_id,
                "event_id": item.event_id,
                "source": item.event_source,
            }
            for item in superseded
        ],
    }


def seq_for_event(event: dict[str, Any]) -> int:
    return int(event["seq"])
