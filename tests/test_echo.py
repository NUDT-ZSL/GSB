from __future__ import annotations

import time
from dataclasses import replace

from tunnel_kernel.codec import derive_message_id, encode_aggregate, sha256_hex
from tunnel_kernel.config import synthetic_payload
from tunnel_kernel.planner import build_plan
from tunnel_kernel.transport import make_transport, query_echo

from conftest import peer_for_ports


def test_udp_echo_reassembles_and_reports_digest(bundle, echo_service) -> None:
    peer = peer_for_ports(
        bundle.peers["udp-alpha"],
        {
            "udp_port": echo_service.bound_udp_port,
            "tcp_port": echo_service.bound_tcp_port,
        },
    )
    payload = synthetic_payload(peer.name, 1024, 4)
    plan = build_plan(
        peer=peer,
        payload=payload,
        policy=bundle.policy,
        link=bundle.links[peer.name],
    )
    transport = make_transport(peer, bundle.policy)
    try:
        response = transport.send(
            encode_aggregate(plan.fragments[:2], mtu=peer.outer_mtu)
        )
        assert response is not None
        assert response["complete"] is False
        assert response["confirmed_sequences"] == [0, 1]
        response = transport.send(plan.fragments[2].raw)
        assert response is not None
        assert response["complete"] is True
        assert response["digest"] == sha256_hex(payload)
        assert response["fragment_count"] == 3
    finally:
        transport.close()

    query = query_echo(
        peer,
        bundle.policy,
        message_id=derive_message_id(plan.tunnel_id, plan.payload_digest),
    )
    assert query is not None
    assert query["complete"] is True
    assert query["digest"] == sha256_hex(payload)


def test_tcp_echo_roundtrip(bundle, echo_service) -> None:
    peer = peer_for_ports(
        bundle.peers["tcp-beta"],
        {
            "udp_port": echo_service.bound_udp_port,
            "tcp_port": echo_service.bound_tcp_port,
        },
    )
    payload = synthetic_payload(peer.name, 768, 8)
    plan = build_plan(
        peer=peer,
        payload=payload,
        policy=bundle.policy,
        link=bundle.links[peer.name],
    )
    transport = make_transport(peer, bundle.policy)
    try:
        responses = [transport.send(packet.raw) for packet in plan.fragments]
    finally:
        transport.close()
    assert responses[-1] is not None
    assert responses[-1]["complete"] is True
    assert responses[-1]["digest"] == sha256_hex(payload)


def test_dropped_datagram_returns_after_real_wait(bundle) -> None:
    from tunnel_kernel.echo_service import LoopbackEchoService

    service = LoopbackEchoService(
        udp_port=0,
        tcp_port=0,
        drop_first_datagrams=1,
    )
    ports = service.start()
    try:
        peer = peer_for_ports(bundle.peers["udp-alpha"], ports)
        policy = replace(bundle.policy, io_timeout_ms=70)
        payload = synthetic_payload(peer.name, 96, 1)
        plan = build_plan(
            peer=peer,
            payload=payload,
            policy=policy,
            link=bundle.links[peer.name],
        )
        transport = make_transport(peer, policy)
        try:
            started = time.monotonic()
            first = transport.send(plan.fragments[0].raw)
            elapsed = time.monotonic() - started
            assert first is None
            assert elapsed >= 0.05
            second = transport.send(plan.fragments[0].raw)
            assert second is not None
            assert second["complete"] is True
        finally:
            transport.close()
    finally:
        service.stop()
