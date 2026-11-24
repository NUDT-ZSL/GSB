"""Typed normalized business objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


FACTOR_TYPES = ("TOTP", "WEBAUTHN", "PUSH")


@dataclass(frozen=True)
class DeviceFingerprint:
    components: tuple[int, ...]
    scale: int


@dataclass(frozen=True)
class Factor:
    factor_id: str
    factor_type: str
    shared_secret_digest: str
    bound_device: DeviceFingerprint


@dataclass(frozen=True)
class Registration:
    user_id: str
    password_digest: str
    factors: tuple[Factor, ...]


@dataclass(frozen=True)
class Attempt:
    attempt_no: int
    user_id: str
    factor_id: str
    totp_code: int
    device_components: tuple[int, ...]
    fingerprint_scale: int | None
    risk_score_millis: int
    submitted_at_ms: int
    timestamp_error_us: int
    source_index: int


@dataclass(frozen=True)
class PolicyRecord:
    policy_key: str
    factor_id: str
    policy_generation: int
    effective_at_ms: int
    verification_window_ms: int
    ticket_generation: int
    ticket_generation_max: int
    tombstone: bool
    factor_type: str | None
    source: str
    source_index: int
    source_ref: str
    stateless_ticket: bool = False


@dataclass(frozen=True)
class TrustedFactor:
    factor_id: str
    factor_type: str | None
    policy_generation: int
    effective_at_ms: int
    verification_window_ms: int
    ticket_generation: int
    ticket_generation_max: int
    source: str
    window_basis: str
    stateless_ticket: bool = False


@dataclass
class PolicyMeta:
    allowed_factor_types: tuple[str, ...] = FACTOR_TYPES
    canonical_scale: int = 6
    ticket_generation_max: int | None = None
    policy_generation: int | None = None
    effective_at_ms: int | None = None
    canonical_scale_defined: bool = False


@dataclass
class PolicyView:
    policy_key: str
    as_of_ms: int
    policy_generation: int
    policy_version: str
    layout_version: str
    trusted_factors: dict[str, TrustedFactor]
    allowed_factor_types: tuple[str, ...]
    canonical_scale: int
    source_counts: dict[str, int]
    issues: list[Any] = field(default_factory=list)
    considered_records: int = 0

    @property
    def trusted_factor_ids(self) -> list[str]:
        return sorted(self.trusted_factors)

    def window_for(self, factor_id: str) -> int:
        return self.trusted_factors[factor_id].verification_window_ms


@dataclass
class DecisionResult:
    attempt_no: int | None
    status: str
    approved: bool
    reasons: list[str]
    policy_version: str
    policy_generation: int
    canonical_fp_key: str | None = None
    fingerprint: dict[str, Any] | None = None
    otp: dict[str, Any] | None = None
    ticket: dict[str, Any] | None = None
    replay: dict[str, Any] | None = None
    latency_ms: float = 0.0
    ledger_watermark: int | None = None
    preview: bool = False
    issues: list[dict[str, Any]] = field(default_factory=list)
    user_id: str | None = None
    factor_id: str | None = None
    source_index: int | None = None
    submitted_at_ms: int | None = None
    risk_score_millis: int | None = None

    @property
    def adjudicated(self) -> bool:
        return self.status in {"APPROVED", "REJECTED", "INDETERMINATE"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_no": self.attempt_no,
            "source_index": self.source_index,
            "submitted_at_ms": self.submitted_at_ms,
            "risk_score_millis": self.risk_score_millis,
            "user_id": self.user_id,
            "factor_id": self.factor_id,
            "status": self.status,
            "approved": self.approved,
            "adjudicated": self.adjudicated,
            "reasons": list(self.reasons),
            "policy_version": self.policy_version,
            "policy_generation": self.policy_generation,
            "canonical_fp_key": self.canonical_fp_key,
            "fingerprint": self.fingerprint,
            "otp": self.otp,
            "ticket": self.ticket,
            "replay": self.replay,
            "latency_ms": round(self.latency_ms, 6),
            "ledger_watermark": self.ledger_watermark,
            "preview": self.preview,
            "issues": list(self.issues),
        }


@dataclass
class BatchReport:
    batch_seq: int
    policy: PolicyView
    results: list[DecisionResult]
    parse_issues: list[dict[str, Any]]
    policy_issues: list[dict[str, Any]]
    policy_replaced: bool
    ledger_rebuilt: bool
    rebuild_reason: str | None
    previous_policy_version: str | None
    checkpoint: dict[str, Any] | None
    started_wall_ms: int
    finished_wall_ms: int

    def counts(self) -> dict[str, int]:
        status_counts: dict[str, int] = {}
        reason_counts: dict[str, int] = {}
        for result in self.results:
            status_counts[result.status] = status_counts.get(result.status, 0) + 1
            for reason in result.reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        approved = status_counts.get("APPROVED", 0)
        rejected = status_counts.get("REJECTED", 0)
        indeterminate = status_counts.get("INDETERMINATE", 0)
        return {
            "results": len(self.results),
            "total_records": len(self.results) + len(self.parse_issues),
            "approved": approved,
            "rejected": rejected,
            "indeterminate": indeterminate,
            "adjudicated": approved + rejected + indeterminate,
            "timeout": status_counts.get("TIMEOUT", 0),
            "queue_full": status_counts.get("QUEUE_FULL", 0),
            "prevalidation_rejected": status_counts.get("PREVALIDATION_REJECTED", 0),
            "parse_rejected": len(self.parse_issues),
            "policy_rejected": len(self.policy_issues),
            "reason_counts": reason_counts,
        }

