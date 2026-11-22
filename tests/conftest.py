from __future__ import annotations

import socket
from typing import Any

import pytest

from mesh_drift.canon import json_digest


def reserve_ports(count: int) -> list[int]:
    sockets = []
    ports = []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
            ports.append(sock.getsockname()[1])
    finally:
        for sock in sockets:
            sock.close()
    return ports


def route(rule_id: str, path: str, label: str, weight: int) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "path": path,
        "version_label": label,
        "weight": weight,
    }


def make_expected(
    services: list[dict[str, Any]],
    *,
    clock: int = 1000,
    snapshot_id: str = "expected-test",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "logical_clock_ms": clock,
        "services": services,
    }


def make_service(
    service: str,
    routes: list[dict[str, Any]],
    instances: list[dict[str, Any]],
) -> dict[str, Any]:
    return {"service": service, "routes": routes, "instances": instances}


def make_instance(
    port: int,
    *,
    instance_id: str,
    version: int = 7,
    label: str = "v1",
    digest: str | None = None,
    routes: list[dict[str, Any]] | None = None,
    cost: int = 100,
) -> dict[str, Any]:
    return {
        "instance_id": instance_id,
        "endpoint": f"http://127.0.0.1:{port}/report",
        "version": version,
        "digest": digest or json_digest(routes or []),
        "version_label": label,
        "pull_cost_logic_ms": cost,
    }


def make_policy(
    *,
    tolerance: int = 5000,
    strict: bool = True,
    concurrency: int = 2,
    budget: int = 1000,
    cooldown: int = 2500,
    timeout: int = 700,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "policy_id": "policy-test",
        "tolerance_window_ms": tolerance,
        "strict_digest": strict,
        "max_concurrency": concurrency,
        "round_budget_ms": budget,
        "dedup_cooldown_ms": cooldown,
        "instance_timeout_ms": timeout,
        "max_report_bytes": 65536,
    }


@pytest.fixture
def one_instance_setup() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    routes = [
        route("r-stable", "/pay", "v1", 90),
        route("r-canary", "/pay", "v2", 10),
    ]
    ports = reserve_ports(1)
    instance = make_instance(
        ports[0],
        instance_id="svc-a",
        version=7,
        label="v1",
        routes=routes,
    )
    expected = make_expected(
        [make_service("checkout", routes, [instance])],
        clock=1000,
    )
    return expected, [instance]

