"""Immutable domain models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PlatformProfile:
    platform: str
    decimals: int
    clock_quantum_ms: int


@dataclass(frozen=True)
class CanonicalFingerprint:
    device_id: str
    platform: str
    decimals: int
    raw_components: tuple[int, ...]
    canonical_components: tuple[int, ...]
    component_error_bounds: tuple[int, ...]
    alignment_step: int
    canonical_key: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "platform": self.platform,
            "decimals": self.decimals,
            "raw_components": list(self.raw_components),
            "canonical_components": list(self.canonical_components),
            "component_error_bounds": list(self.component_error_bounds),
            "alignment_step": self.alignment_step,
            "canonical_key": self.canonical_key,
        }


@dataclass(frozen=True)
class FingerprintDeclaration:
    device_id: str
    platform: str
    fingerprint: CanonicalFingerprint


@dataclass(frozen=True)
class TicketDeclaration:
    ticket_id: str
    device_id: str
    generation: int
    renewal_limit: int
    issued_at_ms: int
    expires_at_ms: int
    usage_count: int
    parent_ticket_id: str | None


@dataclass(frozen=True)
class RenewalRequest:
    request_id: str
    ticket_id: str
    device_id: str
    observed_at_ms: int
    batch_id: int
    fingerprint: CanonicalFingerprint
    successor_ticket_id: str | None


@dataclass(frozen=True)
class CountQuota:
    version: str
    window_seconds: int
    window_anchor_seconds: int
    max_renewals_per_window: int


@dataclass(frozen=True)
class IntervalQuota:
    version: str
    min_interval_ms: int


@dataclass(frozen=True)
class RevocationRequest:
    request_id: str
    target_type: str
    target_id: str
    observed_at_ms: int

