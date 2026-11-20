from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import pytest

from dbpool.model import Catalog, Endpoint, PoolConfig, Trace
from dbpool.storage import canonical_json
from dbpool.validation import (
    combined_digest,
    load_catalog,
    validate_catalog,
)


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def catalog_with(
    base: Catalog,
    *,
    config: PoolConfig | None = None,
    endpoints: tuple[Endpoint, ...] | None = None,
    trace: Trace | None = None,
) -> Catalog:
    config = config or base.config
    endpoints = endpoints or base.endpoints
    trace = trace or base.trace
    mapping = validate_catalog(config, endpoints, trace)
    endpoint_digest = _digest(
        {
            "version": 1,
            "endpoints": [endpoint.canonical() for endpoint in endpoints],
        }
    )
    config_digest = _digest(config.canonical())
    trace_digest = _digest(trace.canonical())
    return Catalog(
        config=config,
        endpoints=endpoints,
        trace=trace,
        tenant_pool_labels=mapping,
        endpoint_digest=endpoint_digest,
        config_digest=config_digest,
        trace_digest=trace_digest,
        sample_digest=combined_digest(
            config_digest,
            endpoint_digest,
            trace_digest,
        ),
    )


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def run_cli(
    *arguments: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "dbpool", *arguments]
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=process_env,
        check=False,
    )


def cli_json(
    *arguments: str,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    result = run_cli(*arguments, env=env)
    if result.returncode != 0:
        raise AssertionError(
            f"CLI failed ({result.returncode}): {result.stderr}\n{result.stdout}"
        )
    return json.loads(result.stdout)


@pytest.fixture(scope="session")
def sample_catalog() -> Catalog:
    return load_catalog(
        SAMPLES / "endpoints.json",
        SAMPLES / "pool_config.json",
        SAMPLES / "trace.json",
    )


@pytest.fixture()
def sim_catalog(sample_catalog: Catalog) -> Catalog:
    endpoints = tuple(
        replace(
            endpoint,
            address="tcp://127.0.0.1:1",
            host="127.0.0.1",
            port=1,
        )
        for endpoint in sample_catalog.endpoints
    )
    return catalog_with(sample_catalog, endpoints=endpoints)


@pytest.fixture(scope="session")
def echo_service() -> Any:
    ports = [free_port(), free_port()]
    ready = ROOT / ".pytest-echo-ready.json"
    if ready.exists():
        ready.unlink()
    process = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "tools" / "echo_server.py"),
            "--listen",
            f"127.0.0.1:{ports[0]}",
            "--listen",
            f"127.0.0.1:{ports[1]}",
            "--ready-file",
            str(ready),
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if ready.exists():
            break
        if process.poll() is not None:
            raise RuntimeError(process.stderr.read())
        time.sleep(0.05)
    else:
        process.kill()
        raise RuntimeError("echo service did not start")
    try:
        yield ports
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        ready.unlink(missing_ok=True)


@pytest.fixture()
def live_catalog(sample_catalog: Catalog, echo_service: list[int]) -> Catalog:
    endpoints = []
    for endpoint, port in zip(sample_catalog.endpoints, echo_service):
        endpoints.append(
            replace(
                endpoint,
                address=f"tcp://127.0.0.1:{port}",
                host="127.0.0.1",
                port=port,
            )
        )
    return catalog_with(sample_catalog, endpoints=tuple(endpoints))


@pytest.fixture()
def malformed_service() -> Any:
    port = free_port()
    ready = ROOT / f".pytest-malformed-{port}.json"
    process = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "tools" / "echo_server.py"),
            "--listen",
            f"127.0.0.1:{port}",
            "--mode",
            "malformed",
            "--ready-file",
            str(ready),
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if ready.exists():
            break
        if process.poll() is not None:
            raise RuntimeError(process.stderr.read())
        time.sleep(0.05)
    try:
        yield port
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        ready.unlink(missing_ok=True)
