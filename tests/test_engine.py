from __future__ import annotations

import json
import threading
from dataclasses import replace

import pytest

from tunnel_kernel.config import synthetic_payload
from tunnel_kernel.engine import ForwardEngine
from tunnel_kernel.errors import E_ABORTED, E_RETRY_EXHAUSTED, BusinessError
from tunnel_kernel.planner import build_plan
from tunnel_kernel.store import Store

from conftest import peer_for_ports, prepare_tunnel


def _ports(service) -> dict[str, int]:
    return {
        "udp_port": service.bound_udp_port,
        "tcp_port": service.bound_tcp_port,
    }


def test_direct_and_aggregate_algorithms_end_to_end(
    bundle, echo_service, tmp_path
) -> None:
    peer = peer_for_ports(bundle.peers["udp-alpha"], _ports(echo_service))

    direct_store = Store(tmp_path / "direct.sqlite3")
    direct_store.initialize()
    direct_plan, _ = prepare_tunnel(
        store=direct_store,
        peer=peer,
        link=bundle.links[peer.name],
        policy=bundle.policy,
        payload_size=384,
        seed=1,
    )
    assert direct_plan.decision.algorithm == "direct"
    direct_result = ForwardEngine(
        store=direct_store,
        peer=peer,
        link=bundle.links[peer.name],
        policy=bundle.policy,
    ).forward(direct_plan.tunnel_id)
    assert direct_result["status"] == "complete"
    assert direct_result["algorithm"] == "direct"
    assert all(
        event["aggregated"] is False
        for event in direct_result["events"]
        if event["type"] == "send"
    )

    aggregate_store = Store(tmp_path / "aggregate.sqlite3")
    aggregate_store.initialize()
    aggregate_plan, _ = prepare_tunnel(
        store=aggregate_store,
        peer=peer,
        link=bundle.links[peer.name],
        policy=bundle.policy,
        payload_size=1024,
        seed=2,
    )
    assert aggregate_plan.decision.algorithm == "aggregate"
    aggregate_result = ForwardEngine(
        store=aggregate_store,
        peer=peer,
        link=bundle.links[peer.name],
        policy=bundle.policy,
    ).forward(aggregate_plan.tunnel_id)
    assert aggregate_result["status"] == "complete"
    assert aggregate_result["algorithm"] == "aggregate"
    assert any(
        event["aggregated"] is True
        for event in aggregate_result["events"]
        if event["type"] == "send"
    )
    assert aggregate_result["reassembly_digest"] == aggregate_plan.payload_digest


def test_mtu_downshift_replans_tail_but_retries_original_bytes(
    bundle, tmp_path
) -> None:
    from tunnel_kernel.echo_service import LoopbackEchoService

    service = LoopbackEchoService(
        udp_port=0,
        tcp_port=0,
        drop_first_datagrams=1,
    )
    ports = service.start()
    try:
        peer = peer_for_ports(bundle.peers["udp-alpha"], ports)
        store = Store(tmp_path / "shift.sqlite3")
        store.initialize()
        plan, _ = prepare_tunnel(
            store=store,
            peer=peer,
            link=bundle.links[peer.name],
            policy=bundle.policy,
            payload_size=1024,
            seed=7,
        )
        result = ForwardEngine(
            store=store,
            peer=peer,
            link=bundle.links[peer.name],
            policy=bundle.policy,
            scenario_changes=bundle.scenarios["degrade-udp"].changes,
        ).forward(plan.tunnel_id)

        assert result["status"] == "complete"
        assert result["retransmit_count"] == 2
        state = store.get_state(plan.tunnel_id)
        assert state["current_mtu"] == 500
        assert state["confirmed_through"] == 2
        assert state["confirmed_bytes"] == 1024

        mtu_events = [
            event for event in result["events"] if event["type"] == "mtu_change"
        ]
        assert len(mtu_events) == 1
        assert mtu_events[0]["to_mtu"] == 500
        assert mtu_events[0]["frontier"] == 848
        assert mtu_events[0]["replanned_sequences"] == [2]

        send_events = [
            event for event in result["events"] if event["type"] == "send"
        ]
        retried = [
            event
            for event in send_events
            if event["attempts"] == [2]
        ]
        assert [event["sequences"] for event in retried] == [[0], [1]]
        assert [event["wire_bytes"] for event in retried] == [512, 512]
        original_digests = {
            packet.sequence: packet.digest for packet in plan.fragments
        }
        rows = store.get_fragment_rows(plan.tunnel_id)
        for row in rows[:2]:
            assert row["packet_digest"] == original_digests[row["seq"]]
    finally:
        service.stop()


def test_retry_budget_exhaustion_enters_failed_state(bundle, tmp_path) -> None:
    class NullTransport:
        def __init__(self) -> None:
            self.sent: list[bytes] = []

        def send(self, datagram: bytes):
            self.sent.append(datagram)
            return None

        def close(self) -> None:
            return None

    peer = bundle.peers["udp-alpha"]
    policy = replace(
        bundle.policy,
        io_timeout_ms=1,
        retransmit_backoff_ms=1,
        max_backoff_ms=1,
    )
    store = Store(tmp_path / "failed.sqlite3")
    store.initialize()
    plan, _ = prepare_tunnel(
        store=store,
        peer=peer,
        link=bundle.links[peer.name],
        policy=policy,
        payload_size=640,
        seed=3,
    )
    transport = NullTransport()
    engine = ForwardEngine(
        store=store,
        peer=peer,
        link=bundle.links[peer.name],
        policy=policy,
        transport=transport,
    )
    with pytest.raises(BusinessError) as caught:
        engine.forward(plan.tunnel_id)
    assert caught.value.code == E_RETRY_EXHAUSTED
    state = store.get_state(plan.tunnel_id)
    assert state["status"] == "failed"
    assert state["failure_code"] == E_RETRY_EXHAUSTED
    assert state["confirmed_through"] == -1
    assert state["confirmed_bytes"] == 0
    assert state["retransmit_count"] == 2 * policy.retransmit_budget
    assert len(transport.sent) == policy.retransmit_budget + 1


def test_resume_recovers_inflight_without_recounting_confirmed(
    bundle, echo_service, tmp_path
) -> None:
    peer = peer_for_ports(bundle.peers["udp-alpha"], _ports(echo_service))
    store = Store(tmp_path / "resume.sqlite3")
    store.initialize()
    plan, _ = prepare_tunnel(
        store=store,
        peer=peer,
        link=bundle.links[peer.name],
        policy=bundle.policy,
        payload_size=1024,
        seed=4,
    )
    with store.tunnel_session_lock(plan.tunnel_id):
        store.reserve_fragment(plan.tunnel_id, 0, now_ms=0)
    assert store.get_state(plan.tunnel_id)["inflight_ranges"] == [[0, 0]]

    result = ForwardEngine(
        store=store,
        peer=peer,
        link=bundle.links[peer.name],
        policy=bundle.policy,
    ).forward(plan.tunnel_id, resume=True)
    state = store.get_state(plan.tunnel_id)
    assert result["status"] == "complete"
    assert state["recovery_count"] == 1
    assert state["retransmit_count"] == 1
    assert state["confirmed_bytes"] == 1024
    assert state["inflight_ranges"] == []


def test_active_abort_leaves_recoverable_state(bundle, tmp_path) -> None:
    class AbortTransport:
        def __init__(self, event: threading.Event) -> None:
            self.event = event

        def send(self, datagram: bytes):
            self.event.set()
            return None

        def close(self) -> None:
            return None

    peer = bundle.peers["udp-alpha"]
    policy = replace(bundle.policy, io_timeout_ms=1, retransmit_backoff_ms=1)
    store = Store(tmp_path / "abort.sqlite3")
    store.initialize()
    plan, _ = prepare_tunnel(
        store=store,
        peer=peer,
        link=bundle.links[peer.name],
        policy=policy,
        payload_size=640,
        seed=5,
    )
    event = threading.Event()
    engine = ForwardEngine(
        store=store,
        peer=peer,
        link=bundle.links[peer.name],
        policy=policy,
        transport=AbortTransport(event),
        abort_event=event,
    )
    with pytest.raises(BusinessError) as caught:
        engine.forward(plan.tunnel_id)
    assert caught.value.code == E_ABORTED
    state = store.get_state(plan.tunnel_id)
    assert state["status"] == "in_progress"
    assert state["confirmed_through"] == -1
    assert state["pending_ranges"]


def test_replay_with_fresh_receiver_has_identical_trace(bundle, tmp_path) -> None:
    from tunnel_kernel.echo_service import LoopbackEchoService

    traces: list[dict] = []
    for run in range(2):
        service = LoopbackEchoService(udp_port=0, tcp_port=0)
        ports = service.start()
        try:
            peer = peer_for_ports(bundle.peers["udp-alpha"], ports)
            store = Store(tmp_path / f"replay-{run}.sqlite3")
            store.initialize()
            plan, _ = prepare_tunnel(
                store=store,
                peer=peer,
                link=bundle.links[peer.name],
                policy=bundle.policy,
                payload_size=1024,
                seed=6,
            )
            result = ForwardEngine(
                store=store,
                peer=peer,
                link=bundle.links[peer.name],
                policy=bundle.policy,
            ).forward(plan.tunnel_id)
            traces.append(result["events"])
        finally:
            service.stop()
    assert json.dumps(traces[0], sort_keys=True) == json.dumps(
        traces[1], sort_keys=True
    )
