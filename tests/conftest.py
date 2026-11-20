import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from graymesh.clock import LogicalClock  # noqa: E402
from graymesh.controlplane import ControlPlane  # noqa: E402
from graymesh.dataplane import DataPlane  # noqa: E402
from graymesh.store import RuleStore  # noqa: E402
from graymesh.upstream import ProgrammableUpstream  # noqa: E402


def member(mid, port, weight=1):
    return {"kind": "upstream_member", "id": mid,
            "spec": {"host": "127.0.0.1", "port": port, "weight": weight}}


def group(gid, member_ids, **extra):
    spec = {"member_ids": member_ids, "lb": "weighted"}
    spec.update(extra)
    return {"kind": "upstream_group", "id": gid, "spec": spec}


def route(rid, gid, weight=1, gray_percent=100, matches=None, mirror=None):
    return {"kind": "gray_route", "id": rid,
            "spec": {"upstream_group_id": gid, "weight": weight,
                     "gray_percent": gray_percent, "matches": matches or [],
                     "mirror_policy_id": mirror}}


def mirror_policy(pid, shadow_gid, percent=100, is_async=True, drop=True):
    return {"kind": "mirror_policy", "id": pid,
            "spec": {"shadow_upstream_group_id": shadow_gid, "percent": percent,
                     "async": is_async, "drop_response": drop}}


def virtual_service(vid, route_ids):
    return {"kind": "virtual_service", "id": vid,
            "spec": {"name": vid, "route_ids": route_ids}}


@pytest.fixture
def object_factory():
    return {"member": member, "group": group, "route": route,
            "mirror_policy": mirror_policy, "virtual_service": virtual_service}


@pytest.fixture
def store(tmp_path):
    return RuleStore(str(tmp_path / "test.db"))


@pytest.fixture
def clock():
    return LogicalClock(start_ms=1_000_000, step_ms=7)


@pytest.fixture
def upstream(tmp_path):
    """Start a programmable upstream on an ephemeral port."""
    started = []

    def _start(routes, port=0):
        server = ProgrammableUpstream("127.0.0.1", port, routes, name="test")
        server.start()
        started.append(server)
        return server

    yield _start
    for server in started:
        server.stop()


@pytest.fixture
def plane(store, clock):
    dataplane = DataPlane(store, clock, timeout_ms=400)
    control = ControlPlane(store, clock, dataplane)
    yield control, dataplane
    dataplane.close()


def bootstrap(control, objects, patch_id="bootstrap"):
    patch = {"patch_id": patch_id, "expected_version": 0,
             "ops": [{"op": "add", "kind": o["kind"], "id": o["id"], "object": o}
                     for o in objects]}
    return control.submit_patch(patch)
