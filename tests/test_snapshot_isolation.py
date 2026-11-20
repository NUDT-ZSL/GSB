"""Read isolation: an in-flight request keeps its start-of-request snapshot;
readers only ever observe one complete committed version."""

import threading
import time

from graymesh.clock import LogicalClock
from graymesh.controlplane import ControlPlane
from graymesh.dataplane import DataPlane
from graymesh.store import RuleStore

from tests.conftest import bootstrap


def make_objects(f, port, weight):
    return [
        f["member"]("m1", port, weight),
        f["group"]("g1", ["m1"]),
        f["route"]("r1", "g1", gray_percent=100),
        f["virtual_service"]("vs1", ["r1"]),
    ]


def test_inflight_request_keeps_start_snapshot(tmp_path, object_factory, upstream):
    server = upstream([{"prefix": "/", "status": 200, "delay_ms": 300}])
    store = RuleStore(str(tmp_path / "db.sqlite"))
    clock = LogicalClock()
    dataplane = DataPlane(store, clock, timeout_ms=2000)
    control = ControlPlane(store, clock, dataplane)
    f = object_factory
    bootstrap(control, make_objects(f, server.port, 1))

    chains = {}

    def send():
        chains["req"] = dataplane.handle(
            {"seq": 1, "key": "u1", "path": "/", "headers": {}})

    thread = threading.Thread(target=send)
    thread.start()
    time.sleep(0.05)  # request is in-flight against v1

    control.submit_patch({"patch_id": "p-mid", "expected_version": 1, "ops": [
        {"op": "replace", "kind": "upstream_member", "id": "m1",
         "object": f["member"]("m1", server.port, 99)}]})
    assert store.current_version() == 2
    thread.join()
    dataplane.close()

    chain = chains["req"]
    assert chain["version"] == 1            # bound to the start version
    assert chain["member_pick"]["total"] == 1  # v1 weight, not the v2 weight 99
    assert chain["outcome"] == "ok"

    # the stored chain is visible under (seq, version=1) only
    decisions = store.list_decisions()
    assert [(d["request_seq"], d["version"]) for d in decisions] == [(1, 1)]


def test_readers_see_only_complete_versions(tmp_path, object_factory):
    store = RuleStore(str(tmp_path / "db.sqlite"))
    control = ControlPlane(store, LogicalClock())
    bootstrap(control, make_objects(object_factory, 9001, 1))

    anomalies = []

    def reader():
        import sqlite3
        for _ in range(200):
            snap = store.load_snapshot()
            # invariant: the observed version is complete and committed,
            # never a half-written mix of old and new rules
            with sqlite3.connect(store.db_path) as conn:
                committed = conn.execute(
                    "SELECT committed FROM versions WHERE version=?",
                    (snap.version,)).fetchone()
                count = conn.execute(
                    "SELECT COUNT(*) FROM objects WHERE version=?",
                    (snap.version,)).fetchone()[0]
            if not committed or committed[0] != 1 or count != 4:
                anomalies.append((snap.version, committed, count))
            time.sleep(0.0005)

    def writer():
        for i in range(2, 8):
            control.submit_patch({"patch_id": "p-%d" % i, "expected_version": None,
                                  "ops": [{"op": "replace", "kind": "upstream_member",
                                           "id": "m1",
                                           "object": object_factory["member"](
                                               "m1", 9001, i)}]})

    readers = [threading.Thread(target=reader) for _ in range(3)]
    for t in readers:
        t.start()
    writer()
    for t in readers:
        t.join()

    assert not anomalies
    assert store.current_version() == 7
