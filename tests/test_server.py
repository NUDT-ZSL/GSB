from __future__ import annotations

from georope.generator import generate_incremental_sample
from georope.server import create_app


def test_health_and_sample_endpoints() -> None:
    app = create_app()
    client = app.test_client()
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.get_json()["status"] == "ready"
    sample = client.get("/api/sample?seed=42")
    assert sample.status_code == 200
    assert len(sample.get_json()["blocks"]) == 12


def test_encode_and_check_endpoints(sample_payload: dict) -> None:
    app = create_app()
    client = app.test_client()
    encode_response = client.post("/api/encode", json=sample_payload)
    assert encode_response.status_code == 200
    encode_body = encode_response.get_json()
    assert encode_body["ok"] is True
    assert encode_body["verification_summary"]["passed"] is True
    check_response = client.post("/api/check", json=sample_payload)
    assert check_response.status_code == 200
    assert check_response.get_json()["passed"] is True


def test_incremental_endpoint() -> None:
    app = create_app()
    response = app.test_client().post(
        "/api/incremental",
        json=generate_incremental_sample(42),
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["incremental_recomputation"]["unaffected_encoding_preserved"] is True
