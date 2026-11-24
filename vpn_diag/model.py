"""Typed normalized sample objects."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .fixedpoint import FIXED_SCALE


@dataclass
class LinkProfile:
    name: str
    rtt_q: int
    rtt_f: float
    loss_q: int
    loss_f: float
    bandwidth_q: int
    bandwidth_f: float
    jitter_q: int
    jitter_f: float
    record_index: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "rtt_ms": self.rtt_q / FIXED_SCALE,
            "rtt_reference_ms": self.rtt_f,
            "loss_rate": self.loss_q / FIXED_SCALE,
            "loss_rate_reference": self.loss_f,
            "bandwidth_kbps": self.bandwidth_q / FIXED_SCALE,
            "bandwidth_reference_kbps": self.bandwidth_f,
            "jitter_limit": self.jitter_q / FIXED_SCALE,
            "jitter_reference": self.jitter_f,
            "record_index": self.record_index,
        }


@dataclass
class PeerConfig:
    name: str
    host: str
    port: int
    address: str
    outer_mtu: int
    crypto_overhead: int
    link_weight_q: int
    link_weight_f: float
    estimate_max: int
    payload_lengths: list[int]
    sync_markers: list[str]
    profile_name: str
    record_index: int
    fragment_threshold: Optional[int] = None
    concurrency_limit: Optional[int] = None

    @property
    def endpoint(self) -> tuple[str, int]:
        return self.host, self.port

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["link_weight"] = self.link_weight_q / FIXED_SCALE
        return data


@dataclass
class Policy:
    fragment_threshold: int
    concurrency_per_peer: int
    round_budget_q: int
    round_budget_f: float
    retransmit_budget: int
    tolerance_q: int
    tolerance_f: float
    clock_step_q: int
    clock_step_f: float
    max_rounds: int = 32
    udp_timeout_ms: int = 200
    checkpoint_every: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "fragment_threshold": self.fragment_threshold,
            "concurrency_per_peer": self.concurrency_per_peer,
            "round_budget_ms": self.round_budget_q / FIXED_SCALE,
            "round_budget_reference_ms": self.round_budget_f,
            "retransmit_budget": self.retransmit_budget,
            "tolerance_ms": self.tolerance_q / FIXED_SCALE,
            "tolerance_reference_ms": self.tolerance_f,
            "clock_step_ms": self.clock_step_q / FIXED_SCALE,
            "clock_step_reference_ms": self.clock_step_f,
            "max_rounds": self.max_rounds,
            "udp_timeout_ms": self.udp_timeout_ms,
            "checkpoint_every": self.checkpoint_every,
        }


@dataclass
class EventRecord:
    index: int
    event_type: str
    at_q: int
    at_ms: float
    peer_name: str
    data: dict[str, Any] = field(default_factory=dict)
    profile: Optional[LinkProfile] = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if self.profile is not None:
            result["profile"] = self.profile.to_dict()
        return result


@dataclass
class ParseResult:
    peers: list[PeerConfig]
    profiles: dict[str, LinkProfile]
    policy: Optional[Policy]
    events: list[EventRecord]
    rejections: list[dict[str, Any]]
    schema_version: int = 1
    seed: Optional[int] = None
    clock_origin_ms: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    def accepted_peer_names(self) -> list[str]:
        return [peer.name for peer in self.peers]

    def to_summary(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "seed": self.seed,
            "clock_origin_ms": self.clock_origin_ms,
            "accepted_peers": len(self.peers),
            "accepted_profiles": len(self.profiles),
            "accepted_events": len(self.events),
            "rejected_records": self.rejections,
            "policy": self.policy.to_dict() if self.policy else None,
        }
