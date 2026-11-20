from __future__ import annotations

import json

from tunnel_kernel.arbiter import decide_algorithm
from tunnel_kernel.config import LinkProfile


def test_small_load_selects_direct(bundle) -> None:
    peer = bundle.peers["udp-alpha"]
    decision = decide_algorithm(
        peer=peer,
        policy=bundle.policy,
        link=bundle.links[peer.name],
        payload_bytes=96,
    )
    assert decision.algorithm == "direct"
    assert decision.fragment_count == 1
    assert "aggregate" in decision.rejected
    assert decision.trigger == "wire_load_bytes <= fragment_threshold_bytes"


def test_large_load_selects_aggregate(bundle) -> None:
    peer = bundle.peers["udp-alpha"]
    decision = decide_algorithm(
        peer=peer,
        policy=bundle.policy,
        link=bundle.links[peer.name],
        payload_bytes=1024,
    )
    assert decision.algorithm == "aggregate"
    assert decision.fragment_count == 3
    assert decision.aggregation_window == 2
    assert "direct" in decision.rejected


def test_bandwidth_limit_can_reject_aggregation(bundle) -> None:
    peer = bundle.peers["udp-alpha"]
    tiny_link = LinkProfile(
        rtt_ms=50.0,
        loss_rate=0.0,
        bandwidth_mbps=0.001,
        jitter_ms=0.0,
    )
    decision = decide_algorithm(
        peer=peer,
        policy=bundle.policy,
        link=tiny_link,
        payload_bytes=1024,
    )
    assert decision.algorithm == "direct"
    assert decision.trigger == "aggregation_window_insufficient"
    assert "aggregate" in decision.rejected


def test_sample_points_and_replay_are_stable(bundle) -> None:
    peer = bundle.peers["udp-alpha"]
    first = decide_algorithm(
        peer=peer,
        policy=bundle.policy,
        link=bundle.links[peer.name],
        payload_bytes=640,
    ).as_dict()
    second = decide_algorithm(
        peer=peer,
        policy=bundle.policy,
        link=bundle.links[peer.name],
        payload_bytes=640,
    ).as_dict()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    points = [item["sample_payload_bytes"] for item in first["sample_decisions"]]
    assert points == list(bundle.policy.sample_points)
    assert first["observations"]["config_sample_points"] == points
    assert {
        "bandwidth_bytes_per_ms",
        "distribution_p50",
        "distribution_p95",
        "max_concurrency",
        "mtu",
    }.issubset(first["observations"])
