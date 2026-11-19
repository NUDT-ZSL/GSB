from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from werkzeug.serving import make_server

from gateway.config import content_digest
from gateway.service import GatewayService, compute_request_digest
from sample_upstream import app as upstream_app

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def load_json(name: str) -> Any:
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


def sample_config(version: int = 1) -> dict[str, Any]:
    return load_json("config_v1.json" if version == 1 else "config_v2.json")


def principals() -> dict[str, dict[str, str]]:
    return load_json("principals.json")


def auth_headers(principal: dict[str, str]) -> dict[str, str]:
    headers = {"X-Tenant-Id": principal["tenant"]}
    if principal["kind"] == "api_key":
        headers["X-API-Key"] = principal["value"]
    else:
        headers["Authorization"] = f"Bearer {principal['value']}"
    return headers


def call_gateway(
    service: GatewayService,
    *,
    principal: dict[str, str] | None,
    request_id: str,
    request_seq: int,
    logical_time_ms: int,
    path: str = "/v1/orders",
    method: str = "GET",
    query_string: str = "",
    body: bytes = b"",
    host: str = "api.local:18080",
    expected_config_version: int | None = None,
    extra_headers: dict[str, str] | None = None,
):
    headers = {} if principal is None else auth_headers(principal)
    tenant_hint = "" if principal is None else principal["tenant"]
    if extra_headers:
        headers.update(extra_headers)
        tenant_hint = headers.get("X-Tenant-Id", tenant_hint)
    digest = compute_request_digest(
        host=host,
        method=method,
        path=path,
        query_string=query_string,
        body=body,
        request_id=request_id,
        request_seq=request_seq,
        logical_time_ms=logical_time_ms,
        tenant_hint=tenant_hint,
        expected_config_version=expected_config_version,
    )
    headers.update(
        {
            "X-Request-Id": request_id,
            "X-Request-Seq": str(request_seq),
            "X-Logical-Time-Ms": str(logical_time_ms),
            "X-Request-Digest": digest,
        }
    )
    if expected_config_version is not None:
        headers["X-Config-Version"] = str(expected_config_version)
    return service.handle_request(
        method=method,
        host=host,
        path=path,
        query_string=query_string,
        headers=headers,
        body=body,
    )


@pytest.fixture(scope="session")
def upstream_server():
    server = make_server("127.0.0.1", 18081, upstream_app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:18081"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def service(tmp_path: Path, upstream_server: str) -> GatewayService:
    return GatewayService(
        tmp_path / "gateway.sqlite",
        initial_config_path=SAMPLES / "config_v1.json",
    )


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "gateway.sqlite"


@pytest.fixture
def app_client(service: GatewayService):
    from gateway.app import create_app

    app = create_app(service=service)
    app.config.update(TESTING=True)
    return app.test_client()


def update_raw(
    service: GatewayService,
    raw: dict[str, Any],
    *,
    expected: int,
    digest: str | None = None,
) -> dict[str, Any]:
    return service.update_config(
        raw,
        expected_current_version=expected,
        provided_digest=content_digest(raw) if digest is None else digest,
        logical_time_ms=0,
    )


def modify_config(version: int = 1) -> dict[str, Any]:
    return copy.deepcopy(sample_config(version))
