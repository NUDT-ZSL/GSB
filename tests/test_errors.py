from __future__ import annotations

import copy

from georope.server import create_app


def test_service_returns_no_encoding_on_unit_ambiguity(sample_payload: dict) -> None:
    app = create_app()
    client = app.test_client()
    payload = copy.deepcopy(sample_payload)
    payload["blocks"][0].pop("origin_unit")
    response = client.post("/api/encode", json=payload)
    assert response.status_code == 422
    body = response.get_json()
    assert body["ok"] is False
    assert body["error"]["code"] == "COORD_UNIT_AMBIGUOUS"
    assert "encoded_fields" not in body


def test_server_returns_typed_error_for_overlap(sample_payload: dict) -> None:
    payload = copy.deepcopy(sample_payload)
    payload["blocks"][4]["channel_groups"][0]["indices"] = [0, 1, 2, 3, 4]
    app = create_app()
    response = app.test_client().post("/api/check", json=payload)
    assert response.status_code == 422
    body = response.get_json()
    assert body["error"]["code"] == "CHANNEL_GROUP_OVERLAP"
    assert body["error"]["locations"][0]["block_id"] == "v2-tile-r0c1"


def test_content_type_error_has_business_code() -> None:
    app = create_app()
    response = app.test_client().post("/api/encode", data="not-json")
    assert response.status_code == 415
    assert response.get_json()["error"]["code"] == "CONTENT_TYPE_INVALID"
