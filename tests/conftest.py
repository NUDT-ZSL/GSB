from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vpn_diag.echo import EchoServer


@pytest.fixture
def echo_server():
    server = EchoServer("127.0.0.1", 0).start()
    try:
        yield server
    finally:
        server.stop()


def make_sample(
    port: int,
    *,
    name: str = "peer-a",
    lengths: Optional[list[int]] = None,
    policy: Optional[dict[str, Any]] = None,
    profile: Optional[dict[str, Any]] = None,
    events: Optional[list[dict[str, Any]]] = None,
    outer_mtu: int = 1400,
    crypto_overhead: int = 0,
    fragment_threshold: int = 512,
    peer_extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    lengths = lengths if lengths is not None else [16]
    default_policy = {
        "fragment_threshold": fragment_threshold,
        "concurrency_per_peer": 2,
        "round_budget_ms": 100000,
        "retransmit_budget": 2,
        "tolerance_ms": 0.01,
        "clock_step_ms": 1,
        "max_rounds": 20,
        "udp_timeout_ms": 100,
        "checkpoint_every": 1,
    }
    if policy:
        default_policy.update(policy)
    default_profile = {
        "name": "test-profile",
        "rtt_ms": 2,
        "loss_rate": 0,
        "bandwidth_kbps": 1000,
        "jitter_limit": 0,
    }
    if profile:
        default_profile.update(profile)
    peer = {
        "name": name,
        "echo_addr": f"127.0.0.1:{port}",
        "outer_mtu": outer_mtu,
        "crypto_overhead": crypto_overhead,
        "link_weight": 1,
        "inner_payload_estimate_max": max(lengths),
        "payload_lengths": lengths,
        "profile": default_profile["name"],
    }
    if peer_extra:
        peer.update(peer_extra)
    return {
        "schema_version": 1,
        "seed": 1234,
        "profiles": [default_profile],
        "policy": default_policy,
        "peers": [peer],
        "events": events or [],
    }


@pytest.fixture
def sample_factory():
    return make_sample
