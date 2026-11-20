import pytest

from graymesh.controlplane import ControlPlane
from graymesh.dataplane import DataPlane

from tests.conftest import bootstrap


def make_objects(f, port):
    return [
        f["member"]("m1", port, 1),
        f["group"]("g1", ["m1"]),
        f["route"]("r1", "g1", gray_percent=100),
        f["virtual_service"]("vs1", ["r1"]),
    ]


@pytest.fixture
def wired(store, clock, object_factory):
    control = ControlPlane(store, clock)
    dataplane = DataPlane(store, clock, timeout_ms=300)
    control.dataplane = dataplane
    yield control, dataplane, object_factory
    dataplane.close()


def send(dataplane, seq=1, path="/"):
    return dataplane.handle({"seq": seq, "key": "u", "path": path, "headers": {}})


def test_connection_refused(wired, object_factory):
    control, dataplane, f = wired
    bootstrap(control, make_objects(f, 1))  # port 1: nothing listens
    chain = send(dataplane)
    assert chain["outcome"] == "error"
    assert chain["error_code"] == "E_UPSTREAM_CONN_REFUSED"
    chain2 = send(dataplane, seq=2)
    assert chain2["error_code"] == "E_UPSTREAM_CONN_REFUSED"


def test_timeout_budget(wired, upstream):
    control, dataplane, f = wired
    server = upstream([{"prefix": "/", "status": 200, "delay_ms": 1500}])
    bootstrap(control, make_objects(f, server.port))
    chain = send(dataplane)
    assert chain["error_code"] == "E_UPSTREAM_TIMEOUT"


def test_malformed_response(wired, upstream):
    control, dataplane, f = wired
    server = upstream([{"prefix": "/", "mode": "malformed"}])
    bootstrap(control, make_objects(f, server.port))
    chain = send(dataplane)
    assert chain["error_code"] == "E_UPSTREAM_MALFORMED"


def test_disconnect(wired, upstream):
    control, dataplane, f = wired
    server = upstream([{"prefix": "/", "mode": "disconnect"}])
    bootstrap(control, make_objects(f, server.port))
    chain = send(dataplane)
    assert chain["error_code"] == "E_UPSTREAM_DISCONNECT"


def test_error_chain_keeps_diagnostic_fields(wired, upstream):
    control, dataplane, f = wired
    server = upstream([{"prefix": "/", "mode": "disconnect"}])
    bootstrap(control, make_objects(f, server.port))
    chain = send(dataplane)
    for field in ("request_seq", "version", "rules_digest", "route_id",
                  "member_id", "error_code", "error_detail", "started_ms",
                  "finished_ms"):
        assert field in chain, field


def test_single_failure_does_not_affect_peers(wired, upstream):
    control, dataplane, f = wired
    server = upstream([
        {"prefix": "/drop", "mode": "disconnect"},
        {"prefix": "/", "status": 200},
    ])
    bootstrap(control, make_objects(f, server.port))
    bad = send(dataplane, seq=1, path="/drop")
    good = send(dataplane, seq=2, path="/ok")
    assert bad["error_code"] == "E_UPSTREAM_DISCONNECT"
    assert good["outcome"] == "ok"
    assert good["status_code"] == 200
    assert good["version"] == bad["version"]
