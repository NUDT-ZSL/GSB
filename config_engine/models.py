"""Immutable domain models and deterministic comparison rules."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .canonical import canonical_json, sha256_value


@dataclass
class Candidate:
    candidate_id: str
    service: str
    environment: str
    logical_clock_ms: int
    generation: int
    source_format: str
    source_priority: int
    version: str
    content_digest: str
    effective_at_ms: int
    expires_at_ms: int | None
    params: dict[str, Any]
    upstreams: list[str]
    tombstone: bool
    original_time: dict[str, Any]
    decision: dict[str, Any] = field(default_factory=dict)
    source_ref: dict[str, Any] = field(default_factory=dict)
    origin: str = "base"
    event_id: str | None = None
    event_source: str | None = None
    accepted: bool = True

    @property
    def target(self) -> tuple[str, str]:
        return (self.service, self.environment)

    @property
    def timeline_key(self) -> tuple[Any, ...]:
        return (
            self.generation,
            self.logical_clock_ms,
            self.effective_at_ms,
            self.source_priority,
            version_key(self.version),
            self.content_digest,
        )

    @property
    def proposal_key(self) -> tuple[Any, ...]:
        """Cross-format proposal arbitration key (larger wins)."""

        return (
            self.logical_clock_ms,
            self.generation,
            self.source_priority,
            self.effective_at_ms,
            version_key(self.version),
            self.content_digest,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "accepted": self.accepted,
            "content_digest": self.content_digest,
            "decision": self.decision,
            "effective_at_ms": self.effective_at_ms,
            "environment": self.environment,
            "event_id": self.event_id,
            "event_source": self.event_source,
            "expires_at_ms": self.expires_at_ms,
            "generation": self.generation,
            "logical_clock_ms": self.logical_clock_ms,
            "origin": self.origin,
            "original_time": self.original_time,
            "params": self.params,
            "service": self.service,
            "source_format": self.source_format,
            "source_priority": self.source_priority,
            "source_ref": self.source_ref,
            "tombstone": self.tombstone,
            "upstreams": self.upstreams,
            "version": self.version,
        }


def content_payload(
    *,
    service: str,
    environment: str,
    generation: int,
    version: str,
    params: dict[str, Any],
    upstreams: list[str],
    tombstone: bool,
    expires_at_ms: int | None,
) -> dict[str, Any]:
    return {
        "environment": environment,
        "expires_at_ms": expires_at_ms,
        "generation": generation,
        "params": params,
        "service": service,
        "tombstone": tombstone,
        "upstreams": upstreams,
        "version": version,
    }


def content_digest(**kwargs: Any) -> str:
    return sha256_value(content_payload(**kwargs))


_VERSION_SPLIT = re.compile(r"([0-9]+|[^0-9]+)")


def version_key(version: str) -> tuple[tuple[int, Any], ...]:
    """Natural, deterministic ordering for dotted versions and arbitrary strings."""

    tokens: list[tuple[int, Any]] = []
    for token in _VERSION_SPLIT.findall(version.lower()):
        if token.isdigit():
            tokens.append((1, int(token)))
        else:
            tokens.append((0, token))
    return tuple(tokens)


def candidate_rank(candidate: Candidate) -> tuple[Any, ...]:
    return candidate.proposal_key


def compare_candidate(winner: Candidate, contender: Candidate) -> str | None:
    """Return the contender's first rejection reason, or None if it wins."""

    if contender.generation < winner.generation:
        return "LOWER_GENERATION"
    if contender.generation > winner.generation:
        return None
    contender_vk = version_key(contender.version)
    winner_vk = version_key(winner.version)
    if contender_vk < winner_vk:
        return "LOWER_VERSION"
    if contender_vk > winner_vk:
        return None
    if contender.source_priority < winner.source_priority:
        return "LOWER_SOURCE_PRIORITY"
    if contender.source_priority > winner.source_priority:
        return None
    if contender.effective_at_ms < winner.effective_at_ms:
        return "EARLIER_EFFECTIVE_AT"
    if contender.effective_at_ms > winner.effective_at_ms:
        return None
    tie_self = (contender.event_id or contender.candidate_id, contender.content_digest)
    tie_winner = (winner.event_id or winner.candidate_id, winner.content_digest)
    if tie_self < tie_winner:
        return "DETERMINISTIC_TIEBREAK"
    return None


def candidate_fingerprint(candidate: Candidate) -> str:
    return sha256_value(
        {
            "content_digest": candidate.content_digest,
            "environment": candidate.environment,
            "generation": candidate.generation,
            "service": candidate.service,
            "source_format": candidate.source_format,
            "source_ref": candidate.source_ref,
            "version": candidate.version,
        }
    )


def candidate_id(candidate: Candidate) -> str:
    return sha256_value(
        {
            "content_digest": candidate.content_digest,
            "effective_at_ms": candidate.effective_at_ms,
            "environment": candidate.environment,
            "event_id": candidate.event_id,
            "generation": candidate.generation,
            "logical_clock_ms": candidate.logical_clock_ms,
            "service": candidate.service,
            "source_format": candidate.source_format,
            "source_priority": candidate.source_priority,
            "source_ref": candidate.source_ref,
            "version": candidate.version,
        }
    )


def candidate_from_dict(value: dict[str, Any]) -> Candidate:
    return Candidate(
        candidate_id=value["candidate_id"],
        service=value["service"],
        environment=value["environment"],
        logical_clock_ms=value["logical_clock_ms"],
        generation=value["generation"],
        source_format=value["source_format"],
        source_priority=value["source_priority"],
        version=value["version"],
        content_digest=value["content_digest"],
        effective_at_ms=value["effective_at_ms"],
        expires_at_ms=value.get("expires_at_ms"),
        params=value["params"],
        upstreams=value["upstreams"],
        tombstone=value["tombstone"],
        original_time=value["original_time"],
        decision=value.get("decision", {}),
        source_ref=value.get("source_ref", {}),
        origin=value.get("origin", "base"),
        event_id=value.get("event_id"),
        event_source=value.get("event_source"),
        accepted=bool(value.get("accepted", True)),
    )


def canonical_candidate(candidate: Candidate) -> str:
    return canonical_json(candidate.to_dict())

