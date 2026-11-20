from __future__ import annotations

import multiprocessing
import sqlite3

from tunnel_kernel.errors import E_SEQUENCE_INFLIGHT, E_TUNNEL_LOCKED, BusinessError
from tunnel_kernel.store import Store

from conftest import prepare_tunnel


def _reserve_worker(db_path: str, tunnel_id: str, barrier, queue) -> None:
    store = Store(db_path)
    barrier.wait()
    try:
        reservation = store.reserve_fragment(tunnel_id, 0, now_ms=0)
        queue.put(("ok", reservation["attempt"]))
    except BusinessError as exc:
        queue.put(("error", exc.code))
    except Exception as exc:  # pragma: no cover - diagnostic path
        queue.put(("exception", repr(exc)))


def _lock_worker(db_path: str, tunnel_id: str, queue) -> None:
    store = Store(db_path)
    try:
        with store.tunnel_session_lock(tunnel_id):
            queue.put(("ok", 0))
    except BusinessError as exc:
        queue.put(("error", exc.code))


def test_concurrent_reservation_allows_exactly_one_sender(bundle, tmp_path) -> None:
    peer = bundle.peers["udp-alpha"]
    db_path = tmp_path / "one-sender.sqlite3"
    store = Store(db_path)
    store.initialize()
    plan, _ = prepare_tunnel(
        store=store,
        peer=peer,
        link=bundle.links[peer.name],
        policy=bundle.policy,
        payload_size=1024,
        seed=1,
    )
    barrier = multiprocessing.Barrier(2)
    queue = multiprocessing.Queue()
    processes = [
        multiprocessing.Process(
            target=_reserve_worker,
            args=(str(db_path), plan.tunnel_id, barrier, queue),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    results = [queue.get(timeout=2) for _ in range(2)]
    assert sorted(result[0] for result in results) == ["error", "ok"]
    assert [result[1] for result in results if result[0] == "error"] == [
        E_SEQUENCE_INFLIGHT
    ]
    state = store.get_state(plan.tunnel_id)
    assert state["inflight_ranges"] == [[0, 0]]


def test_duplicate_ack_is_idempotent(bundle, tmp_path) -> None:
    peer = bundle.peers["udp-alpha"]
    store = Store(tmp_path / "ack.sqlite3")
    store.initialize()
    plan, _ = prepare_tunnel(
        store=store,
        peer=peer,
        link=bundle.links[peer.name],
        policy=bundle.policy,
        payload_size=640,
        seed=2,
    )
    with store.tunnel_session_lock(plan.tunnel_id):
        for sequence in range(len(plan.fragments)):
            store.reserve_fragment(plan.tunnel_id, sequence, now_ms=0)
        first = store.apply_ack(
            plan.tunnel_id,
            acked_sequences=[0],
            summary={"complete": False},
            now_ms=4,
        )
        second = store.apply_ack(
            plan.tunnel_id,
            acked_sequences=[0],
            summary={"complete": False},
            now_ms=8,
        )
    assert first["confirmed_bytes"] == len(plan.fragments[0].payload)
    assert second["confirmed_bytes"] == first["confirmed_bytes"]
    assert second["confirmed_through"] == first["confirmed_through"]
    assert second["retransmit_count"] == 0
    connection = sqlite3.connect(store.db_path)
    try:
        (count,) = connection.execute(
            "SELECT COUNT(*) FROM fragment_acks WHERE tunnel_id = ?",
            (plan.tunnel_id,),
        ).fetchone()
    finally:
        connection.close()
    assert count == 1


def test_readers_only_observe_committed_state(bundle, tmp_path) -> None:
    peer = bundle.peers["udp-alpha"]
    store = Store(tmp_path / "isolation.sqlite3")
    store.initialize()
    plan, _ = prepare_tunnel(
        store=store,
        peer=peer,
        link=bundle.links[peer.name],
        policy=bundle.policy,
        payload_size=640,
        seed=3,
    )
    writer = sqlite3.connect(store.db_path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        """
        UPDATE fragments SET status = 'inflight', attempts = 1
        WHERE tunnel_id = ? AND seq = 0
        """,
        (plan.tunnel_id,),
    )
    try:
        state = store.get_state(plan.tunnel_id)
        assert state["inflight_ranges"] == []
        assert state["pending_ranges"] == [[0, 1]]
    finally:
        writer.rollback()
        writer.close()


def test_session_lock_blocks_second_process(bundle, tmp_path) -> None:
    peer = bundle.peers["udp-alpha"]
    db_path = tmp_path / "session-lock.sqlite3"
    store = Store(db_path)
    store.initialize()
    plan, _ = prepare_tunnel(
        store=store,
        peer=peer,
        link=bundle.links[peer.name],
        policy=bundle.policy,
        payload_size=640,
        seed=4,
    )
    queue = multiprocessing.Queue()
    with store.tunnel_session_lock(plan.tunnel_id):
        process = multiprocessing.Process(
            target=_lock_worker,
            args=(str(db_path), plan.tunnel_id, queue),
        )
        process.start()
        process.join(timeout=10)
        assert process.exitcode == 0
        assert queue.get(timeout=2) == ("error", E_TUNNEL_LOCKED)
