from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from h2diag.api import create_app


def test_api_end_to_end(
    tmp_path: Path, main_scenario: dict, main_patch: dict
) -> None:
    app = create_app(tmp_path / "api.db")
    with TestClient(app) as client:
        run = client.post("/run", json={"scenario": main_scenario, "max_frames": 12})
        assert run.status_code == 200
        assert run.json()["processed"] == 12
        patch = client.post(
            "/patch",
            json={"connection_id": "demo-connection", "patch": main_patch},
        )
        assert patch.status_code == 200
        assert patch.json()["version"] == 2
        resumed = client.post("/resume", json={"connection_id": "demo-connection"})
        assert resumed.status_code == 200
        assert resumed.json()["processed"] == 6
        status = client.get("/status", params={"connection_id": "demo-connection"})
        assert status.status_code == 200
        assert status.json()["committed_seq"] == 18
        verify = client.post("/verify", json={"connection_id": "demo-connection"})
        assert verify.status_code == 200
        assert verify.json()["ok"] is True
        health = client.get("/health")
        assert health.json()["database"]["journal_mode"] == "wal"
        validate = client.post("/validate/scenario", json=main_scenario)
        assert validate.status_code == 200


def test_api_returns_distinct_business_error_codes(
    tmp_path: Path, main_scenario: dict, main_patch: dict
) -> None:
    app = create_app(tmp_path / "api-errors.db")
    bad_scenario = json.loads(json.dumps(main_scenario))
    bad_scenario["streams"][0]["initiator"] = "server"
    bad_patch = json.loads(json.dumps(main_patch))
    bad_patch["content_digest"] = "sha256:" + "0" * 64
    with TestClient(app) as client:
        scenario_response = client.post("/validate/scenario", json=bad_scenario)
        patch_response = client.post("/validate/patch", json=bad_patch)
        assert scenario_response.status_code == 422
        assert patch_response.status_code == 422
        assert scenario_response.json()["error"]["code"] == "STREAM_PARITY_001"
        assert patch_response.json()["error"]["code"] == "PATCH_DIGEST_001"
        assert (
            scenario_response.json()["error"]["code"]
            != patch_response.json()["error"]["code"]
        )
