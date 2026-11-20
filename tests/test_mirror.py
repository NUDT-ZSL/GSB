import threading
import time

from graymesh.clock import LogicalClock
from graymesh.controlplane import ControlPlane
from graymesh.dataplane import DataPlane
from graymesh.store import RuleStore

from tests.conftest import bootstrap


def make_objects(f, prod_port, shadow_port, mirror_percent=100, is_async=True):
    return [
        f["member"]("m1", prod_port, 1),
        f["member"]("s1", shadow_port, 1),
        f["group"]("g1", ["m1"]),
        f["group"]("g-shadow", ["s1"]),
        f["mirror_policy"]("mp1", "g-shadow", percent=mirror_percent,
                           is_async=is_async),
        f["route"]("r1", "g1", gray_percent=100, mirror="mp1"),
        f["virtual_service"]("vs1", ["r1"]),
    ]


def test_mirror_failure_does_not_affect_main(store, clock, object_factory, upstream):
    prod = upstream([{"prefix": "/", "status": 200, "body": "prod"}])
    shadow = upstream([{"prefix": "/", "mode": "disconnect"}])
    control = ControlPlane(store, clock)
    dataplane = DataPlane(store, clock, timeout_ms=400)
    control.dataplane = dataplane
    bootstrap(control, make_objects(object_factory, prod.port, shadow.port))

    chain = dataplane.handle({"seq": 1, "key": "u1", "path": "/ok", "headers": {}})
    dataplane.flush_mirrors()
    dataplane.close()

    assert chain["outcome"] == "ok"
    assert chain["status_code"] == 200
    assert "error_code" not in chain
    mirrors = store.list_mirrors()
    assert len(mirrors) == 1
    assert mirrors[0]["state"] == "failed"
    assert mirrors[0]["error_code"] == "E_UPSTREAM_DISCONNECT"
    assert mirrors[0]["version"] == 1
    assert mirrors[0]["shadow_member_id"] == "s1"


def test_mirror_sampling_deterministic(store, clock, object_factory, upstream):
    prod = upstream([{"prefix": "/", "status": 200}])
    shadow = upstream([{"prefix": "/", "status": 200}])
    control = ControlPlane(store, clock)
    dataplane = DataPlane(store, clock, timeout_ms=400)
    control.dataplane = dataplane
    bootstrap(control, make_objects(object_factory, prod.port, shadow.port,
                                    mirror_percent=50))
    sampled = 0
    for i in range(1, 41):
        chain = dataplane.handle({"seq": i, "key": "k%d" % i, "path": "/", "headers": {}})
        if chain["mirror"]["sampled"]:
            sampled += 1
    dataplane.flush_mirrors()
    dataplane.close()
    mirrors = store.list_mirrors()
    assert len(mirrors) == sampled
    assert 0 < sampled < 40
    assert all(m["state"] == "completed" for m in mirrors)
    assert all(m["response_dropped"] for m in mirrors)


def test_inflight_mirror_uses_start_snapshot(tmp_path, object_factory, upstream):
    """A rule update during an in-flight mirror must not redirect it to an
    invalidated shadow upstream."""
    prod = upstream([{"prefix": "/", "status": 200}])
    shadow_old = upstream([{"prefix": "/", "status": 200, "delay_ms": 300}])
    shadow_new = upstream([{"prefix": "/", "status": 200}])

    store = RuleStore(str(tmp_path / "db.sqlite"))
    clock = LogicalClock()
    dataplane = DataPlane(store, clock, timeout_ms=2000)
    control = ControlPlane(store, clock, dataplane)
    f = object_factory
    bootstrap(control, make_objects(f, prod.port, shadow_old.port))

    chain = dataplane.handle({"seq": 1, "key": "u1", "path": "/", "headers": {}})
    assert chain["mirror"]["sampled"]
    time.sleep(0.05)  # mirror is in-flight against shadow_old now

    control.submit_patch({"patch_id": "p-shadow-swap", "expected_version": 1, "ops": [
        {"op": "add", "kind": "upstream_member", "id": "s2",
         "object": f["member"]("s2", shadow_new.port, 1)},
        {"op": "add", "kind": "upstream_group", "id": "g-shadow-2",
         "object": f["group"]("g-shadow-2", ["s2"])},
        {"op": "replace", "kind": "mirror_policy", "id": "mp1",
         "object": f["mirror_policy"]("mp1", "g-shadow-2")},
        {"op": "remove", "kind": "gray_route", "id": "r1"},
        {"op": "add", "kind": "gray_route", "id": "r1",
         "object": f["route"]("r1", "g1", gray_percent=100, mirror="mp1")},
        {"op": "remove", "kind": "upstream_group", "id": "g-shadow"},
        {"op": "remove", "kind": "upstream_member", "id": "s1"},
    ]})
    dataplane.flush_mirrors()
    dataplane.close()

    mirrors = store.list_mirrors()
    assert len(mirrors) == 1
    mirror = mirrors[0]
    assert mirror["state"] == "completed"
    assert mirror["version"] == 1
    assert mirror["shadow_member_id"] == "s1"
    assert shadow_old.request_count == 1
    assert shadow_new.request_count == 0


def test_sync_mirror_records_completion(store, clock, object_factory, upstream):
    prod = upstream([{"prefix": "/", "status": 200}])
    shadow = upstream([{"prefix": "/", "status": 200}])
    control = ControlPlane(store, clock)
    dataplane = DataPlane(store, clock, timeout_ms=400)
    control.dataplane = dataplane
    bootstrap(control, make_objects(object_factory, prod.port, shadow.port,
                                    is_async=False))
    chain = dataplane.handle({"seq": 1, "key": "u1", "path": "/", "headers": {}})
    dataplane.close()
    assert chain["mirror"]["sampled"]
    mirrors = store.list_mirrors()
    assert len(mirrors) == 1 and mirrors[0]["state"] == "completed"
    assert mirrors[0]["request_seq"] == 1


def test_concurrent_mirrors_recorded_once(tmp_path, object_factory, upstream):
    prod = upstream([{"prefix": "/", "status": 200}])
    shadow = upstream([{"prefix": "/", "status": 200}])
    store = RuleStore(str(tmp_path / "db.sqlite"))
    clock = LogicalClock()
    dataplane = DataPlane(store, clock, timeout_ms=400)
    control = ControlPlane(store, clock, dataplane)
    bootstrap(control, make_objects(object_factory, prod.port, shadow.port))

    errors = []

    def send(seq):
        try:
            dataplane.handle({"seq": seq, "key": "u%d" % seq, "path": "/",
                              "headers": {}})
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=send, args=(i,)) for i in range(1, 21)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    dataplane.flush_mirrors()
    dataplane.close()

    assert not errors
    mirrors = store.list_mirrors()
    assert len(mirrors) == 20
    assert len({m["mirror_seq"] for m in mirrors}) == 20
    assert all(m["state"] == "completed" for m in mirrors)
    decisions = store.list_decisions()
    assert len(decisions) == 20
