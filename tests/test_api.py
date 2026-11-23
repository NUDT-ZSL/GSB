from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from diagnostic_workbench.api import create_app
from diagnostic_workbench.util import canonical_json_bytes

from .conftest import SAMPLE_PATH


def test_api_endpoints_and_stable_json(tmp_path: Path) -> None:
    app = create_app(tmp_path / "api-state")
    client = TestClient(app)

    load_response = client.post(
        "/load",
        json={"sample_path": str(SAMPLE_PATH)},
    )
    assert load_response.status_code == 200
    assert load_response.content == canonical_json_bytes(load_response.json())
    assert load_response.json()["status"] == "loaded"

    status_response = client.get("/status")
    assert status_response.status_code == 200
    assert status_response.content == canonical_json_bytes(status_response.json())
    assert status_response.json()["loaded"] is True

    switch_response = client.post(
        "/switch",
        json={"round_id": 1, "logical_time_ms": 130},
    )
    assert switch_response.status_code == 200
    assert switch_response.content == canonical_json_bytes(switch_response.json())
    switch_body = switch_response.json()
    assert switch_body["status"] == "committed"
    assert switch_body["pointer_version"] == 2

    verify_response = client.post("/verify", json={"round_id": 1})
    assert verify_response.status_code == 200
    assert verify_response.content == canonical_json_bytes(verify_response.json())
    assert verify_response.json()["verified"] is True

    pinned = client.get("/query/auth", params={"pointer_version": 1})
    assert pinned.status_code == 200
    assert pinned.json()["primary"] == "auth-core-a"
    assert pinned.json()["frozen"] is False
    current = client.get("/query/auth")
    assert current.json()["primary"] == "auth-core-b"
    assert current.json()["frozen"] is True

    resume_response = client.post("/resume", json={})
    assert resume_response.status_code == 200
    assert resume_response.json()["resumed_rounds"] == []

    ledger_response = client.get("/ledger", params={"round_id": 1})
    assert ledger_response.status_code == 200
    assert ledger_response.json()["count"] == 4


def test_api_business_error_envelope(tmp_path: Path) -> None:
    app = create_app(tmp_path / "error-state")
    client = TestClient(app)
    sample = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    response = client.post(
        "/load",
        json={
            "sample": sample,
            "expected_sample_digest": "f" * 64,
        },
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "SAMPLE_DIGEST_MISMATCH"
    assert response.content == canonical_json_bytes(body)

    not_loaded = TestClient(create_app(tmp_path / "empty-state")).post(
        "/switch",
        json={"round_id": 1, "logical_time_ms": 1},
    )
    assert not_loaded.status_code == 404
    assert not_loaded.json()["error"]["code"] == "NOT_LOADED"

