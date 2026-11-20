"""Deterministic scale-adaptive direct/aggregate forwarding arbitration."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .codec import AGGREGATE_HEADER_SIZE, ENTRY_LENGTH_STRUCT, HEADER_SIZE
from .config import LinkProfile, Peer, Policy


@dataclass(frozen=True, slots=True)
class AlgorithmDecision:
    algorithm: str
    trigger: str
    rejected: dict[str, str]
    observations: dict[str, Any]
    aggregation_window: int
    fragment_count: int
    sample_decisions: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "aggregation_window": self.aggregation_window,
            "algorithm": self.algorithm,
            "fragment_count": self.fragment_count,
            "observations": self.observations,
            "rejected": dict(sorted(self.rejected.items())),
            "sample_decisions": list(self.sample_decisions),
            "trigger": self.trigger,
        }


def _percentile(values: tuple[int, ...], percentile: float) -> float:
    array = np.asarray(values, dtype=np.float64)
    return round(float(np.percentile(array, percentile, method="linear")), 6)


def _wire_load(payload_bytes: int, peer: Peer, policy: Policy) -> int:
    return payload_bytes + HEADER_SIZE + peer.crypto_overhead(policy.crypto_overhead_bytes)


def fragment_count_for(
    payload_bytes: int,
    *,
    peer: Peer,
    policy: Policy,
    mtu: int | None = None,
) -> int:
    effective_mtu = mtu if mtu is not None else peer.outer_mtu
    budget = min(effective_mtu, policy.fragment_threshold_bytes)
    capacity = budget - HEADER_SIZE - peer.crypto_overhead(policy.crypto_overhead_bytes)
    if capacity <= 0:
        return 0
    if _wire_load(payload_bytes, peer, policy) <= budget:
        return 1
    return math.ceil(payload_bytes / capacity)


def _aggregation_window(
    *,
    payload_bytes: int,
    fragment_count: int,
    peer: Peer,
    policy: Policy,
    link: LinkProfile,
    mtu: int,
) -> int:
    if fragment_count <= 1:
        return 1
    budget = min(mtu, policy.fragment_threshold_bytes)
    capacity = budget - HEADER_SIZE - peer.crypto_overhead(policy.crypto_overhead_bytes)
    if capacity <= 0:
        return 1
    average_payload = min(capacity, max(1, math.ceil(payload_bytes / fragment_count)))
    average_packet = (
        HEADER_SIZE
        + peer.crypto_overhead(policy.crypto_overhead_bytes)
        + average_payload
    )
    per_envelope = max(
        1,
        (mtu - AGGREGATE_HEADER_SIZE)
        // (average_packet + ENTRY_LENGTH_STRUCT.size),
    )
    bytes_per_ms = link.bandwidth_mbps * 1_000_000.0 / 8.0 / 1_000.0
    bytes_per_round = max(average_packet, bytes_per_ms * policy.round_budget_ms)
    by_bandwidth = max(1, int(bytes_per_round // average_packet))
    return max(1, min(policy.max_concurrency, per_envelope, by_bandwidth, fragment_count))


def _decision_for(
    payload_bytes: int,
    *,
    peer: Peer,
    policy: Policy,
    link: LinkProfile,
    mtu: int,
) -> dict[str, Any]:
    wire_load = _wire_load(payload_bytes, peer, policy)
    fragments = fragment_count_for(payload_bytes, peer=peer, policy=policy, mtu=mtu)
    window = _aggregation_window(
        payload_bytes=payload_bytes,
        fragment_count=fragments,
        peer=peer,
        policy=policy,
        link=link,
        mtu=mtu,
    )
    direct_rounds = math.ceil(fragments / policy.max_concurrency) if fragments else 0
    aggregate_rounds = math.ceil(fragments / window) if fragments and window else 0

    if wire_load <= policy.fragment_threshold_bytes:
        algorithm = "direct"
        trigger = "wire_load_bytes <= fragment_threshold_bytes"
        rejected = {
            "aggregate": "single-packet load does not amortize aggregation overhead"
        }
    elif window < 2:
        algorithm = "direct"
        trigger = "aggregation_window_insufficient"
        rejected = {
            "aggregate": "bandwidth/MTU/concurrency permit only one fragment per window"
        }
    else:
        algorithm = "aggregate"
        trigger = "wire_load_bytes > fragment_threshold_bytes and aggregation_window >= 2"
        rejected = {
            "direct": (
                "wire_load_bytes exceeds fragment_threshold_bytes; "
                f"estimated rounds direct={direct_rounds} "
                f"aggregate={aggregate_rounds}"
            )
        }
    return {
        "aggregate_rounds": aggregate_rounds,
        "aggregation_window": window,
        "algorithm": algorithm,
        "direct_rounds": direct_rounds,
        "fragment_count": fragments,
        "payload_bytes": payload_bytes,
        "rejected": rejected,
        "trigger": trigger,
        "wire_load_bytes": wire_load,
    }


def decide_algorithm(
    *,
    peer: Peer,
    policy: Policy,
    link: LinkProfile,
    payload_bytes: int | None = None,
    mtu: int | None = None,
) -> AlgorithmDecision:
    """Choose direct or aggregate sending using only explicit observations."""

    effective_mtu = mtu if mtu is not None else peer.outer_mtu
    if payload_bytes is None:
        payload_bytes = int(round(_percentile(peer.payload_lengths, 95)))
    if payload_bytes <= 0:
        payload_bytes = 1

    primary = _decision_for(
        payload_bytes,
        peer=peer,
        policy=policy,
        link=link,
        mtu=effective_mtu,
    )
    samples = tuple(
        _decision_for(
            point,
            peer=peer,
            policy=policy,
            link=link,
            mtu=effective_mtu,
        )
        for point in policy.sample_points
    )
    bytes_per_ms = link.bandwidth_mbps * 1_000_000.0 / 8.0 / 1_000.0
    observations = {
        "bandwidth_bytes_per_ms": round(bytes_per_ms, 6),
        "bandwidth_mbps": link.bandwidth_mbps,
        "config_sample_points": list(policy.sample_points),
        "distribution_max": max(peer.payload_lengths),
        "distribution_p50": _percentile(peer.payload_lengths, 50),
        "distribution_p95": _percentile(peer.payload_lengths, 95),
        "encrypted": peer.encrypted,
        "fragment_capacity_bytes": (
            min(effective_mtu, policy.fragment_threshold_bytes)
            - HEADER_SIZE
            - peer.crypto_overhead(policy.crypto_overhead_bytes)
        ),
        "fragment_threshold_bytes": policy.fragment_threshold_bytes,
        "link_jitter_ms": link.jitter_ms,
        "link_loss_rate": link.loss_rate,
        "link_rtt_ms": link.rtt_ms,
        "max_concurrency": policy.max_concurrency,
        "mtu": effective_mtu,
        "payload_bytes": payload_bytes,
        "round_budget_ms": policy.round_budget_ms,
    }
    return AlgorithmDecision(
        algorithm=primary["algorithm"],
        trigger=primary["trigger"],
        rejected=primary["rejected"],
        observations=observations,
        aggregation_window=primary["aggregation_window"],
        fragment_count=primary["fragment_count"],
        sample_decisions=tuple(
            {
                "aggregation_window": decision["aggregation_window"],
                "algorithm": decision["algorithm"],
                "fragment_count": decision["fragment_count"],
                "sample_payload_bytes": decision["payload_bytes"],
                "trigger": decision["trigger"],
                "wire_load_bytes": decision["wire_load_bytes"],
            }
            for decision in samples
        ),
    )
