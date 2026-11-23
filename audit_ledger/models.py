from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


QUOTA_ACTIONS = ("reject", "archive", "allow_overflow")
TERMINAL_STATES = (
    "written",
    "archived",
    "rejected",
    "manual",
    "abandoned",
)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_digest(value: Any) -> str:
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        raw = canonical_json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def payload_bytes(payload: Any) -> bytes:
    return canonical_json(payload).encode("utf-8")


def logical_window(logical_ts_ms: int, window_ms: int) -> tuple[str, int, int]:
    start = (logical_ts_ms // window_ms) * window_ms
    end = start + window_ms
    return f"day-{logical_ts_ms // window_ms}", start, end


def retry_delay_ms(
    idempotency_key: str,
    attempt_number: int,
    retry_base_ms: int,
    retry_cap_ms: int,
) -> int:
    exponent = min(max(0, attempt_number - 1), 32)
    exponential = retry_base_ms * (2**exponent)
    capped = min(retry_cap_ms, exponential)
    jitter_width = max(1, capped // 4)
    material = f"{idempotency_key}|{attempt_number}".encode("utf-8")
    jitter = int.from_bytes(hashlib.sha256(material).digest()[:4], "big")
    return min(retry_cap_ms, capped + (jitter % (jitter_width + 1)))


@dataclass(frozen=True)
class LedgerItem:
    tenant: str
    window_id: str
    window_start_ms: int
    window_end_ms: int
    used_bytes: int
    archived_bytes: int
    written_count: int
    archived_count: int
    overflow_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "window_id": self.window_id,
            "window_start_ms": self.window_start_ms,
            "window_end_ms": self.window_end_ms,
            "used_bytes": self.used_bytes,
            "archived_bytes": self.archived_bytes,
            "written_count": self.written_count,
            "archived_count": self.archived_count,
            "overflow_count": self.overflow_count,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "LedgerItem":
        return cls(
            tenant=str(value["tenant"]),
            window_id=str(value["window_id"]),
            window_start_ms=int(value["window_start_ms"]),
            window_end_ms=int(value["window_end_ms"]),
            used_bytes=int(value["used_bytes"]),
            archived_bytes=int(value["archived_bytes"]),
            written_count=int(value["written_count"]),
            archived_count=int(value["archived_count"]),
            overflow_count=int(value["overflow_count"]),
        )


@dataclass(frozen=True)
class Policy:
    policy_version: int
    allowed_categories: tuple[str, ...]
    max_events_per_round: int
    logical_time_budget_ms: int
    clock_step_ms: int
    retry_base_ms: int
    retry_cap_ms: int
    max_attempts: int
    max_clock_regression_ms: int
    window_ms: int = 86_400_000

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "allowed_categories": list(self.allowed_categories),
            "max_events_per_round": self.max_events_per_round,
            "logical_time_budget_ms": self.logical_time_budget_ms,
            "clock_step_ms": self.clock_step_ms,
            "retry_base_ms": self.retry_base_ms,
            "retry_cap_ms": self.retry_cap_ms,
            "max_attempts": self.max_attempts,
            "max_clock_regression_ms": self.max_clock_regression_ms,
            "window_ms": self.window_ms,
        }

    @property
    def digest(self) -> str:
        return sha256_digest(self.to_dict())


@dataclass(frozen=True)
class QuotaDeclaration:
    tenant: str
    daily_byte_quota: int
    max_event_bytes: int
    quota_action: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "daily_byte_quota": self.daily_byte_quota,
            "max_event_bytes": self.max_event_bytes,
            "quota_action": self.quota_action,
        }


@dataclass(frozen=True)
class QuotaConfig:
    quota_version: int
    declarations: tuple[QuotaDeclaration, ...]

    def by_tenant(self) -> dict[str, QuotaDeclaration]:
        return {item.tenant: item for item in self.declarations}

    def to_dict(self) -> dict[str, Any]:
        return {
            "quota_version": self.quota_version,
            "declarations": [
                item.to_dict()
                for item in sorted(self.declarations, key=lambda item: item.tenant)
            ],
        }

    @property
    def digest(self) -> str:
        return sha256_digest(self.to_dict())


@dataclass(frozen=True)
class AuditEvent:
    event_seq: int
    tenant: str
    category: str
    payload: Any
    payload_digest: str
    logical_ts_ms: int
    idempotency_key: str
    event_version: int = 1
    stream_version: int = 1

    @property
    def payload_json(self) -> str:
        return canonical_json(self.payload)

    @property
    def byte_size(self) -> int:
        return len(payload_bytes(self.payload))

    @property
    def key(self) -> str:
        return f"{self.event_seq}:{self.event_version}"

    def identity(self) -> dict[str, Any]:
        return {
            "event_seq": self.event_seq,
            "event_version": self.event_version,
            "tenant": self.tenant,
            "category": self.category,
            "payload_digest": self.payload_digest,
            "payload": self.payload,
            "logical_ts_ms": self.logical_ts_ms,
            "idempotency_key": self.idempotency_key,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_seq": self.event_seq,
            "event_version": self.event_version,
            "tenant": self.tenant,
            "category": self.category,
            "payload": self.payload,
            "payload_digest": self.payload_digest,
            "logical_ts_ms": self.logical_ts_ms,
            "idempotency_key": self.idempotency_key,
            "stream_version": self.stream_version,
        }

    @property
    def event_digest(self) -> str:
        return sha256_digest(self.identity())


@dataclass(frozen=True)
class Decision:
    tenant: str
    window_id: str
    used_bytes: int
    archived_bytes: int
    event_bytes: int
    projected_bytes: int
    quota_bytes: int
    max_event_bytes: int
    remaining_bytes: int
    quota_action: str
    verdict: str
    first_constraint: str | None
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "window_id": self.window_id,
            "used_bytes": self.used_bytes,
            "archived_bytes": self.archived_bytes,
            "event_bytes": self.event_bytes,
            "projected_bytes": self.projected_bytes,
            "quota_bytes": self.quota_bytes,
            "max_event_bytes": self.max_event_bytes,
            "remaining_bytes": self.remaining_bytes,
            "quota_action": self.quota_action,
            "verdict": self.verdict,
            "first_constraint": self.first_constraint,
            "error_code": self.error_code,
        }

    @property
    def digest(self) -> str:
        return sha256_digest(self.to_dict())


@dataclass(frozen=True)
class InputBundle:
    events: tuple[AuditEvent, ...]
    quotas: QuotaConfig
    policy: Policy

    @property
    def stream_version(self) -> int:
        return self.events[0].stream_version if self.events else 1
