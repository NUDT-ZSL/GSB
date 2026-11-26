from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cold_diag.api import app
from cold_diag.sample_data import generate_sample


def test_health_and_diagnose_endpoint(tmp_path: Path) -> None:
    data_root = generate_sample(tmp_path / "data")["root"]
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        response = client.post(
            "/diagnose",
            json={"data_dir": data_root, "db_path": str(tmp_path / "api.sqlite")},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["processed_request_seq"] == 8
        assert any(
            marker["sensitive"] for marker in payload["precision_markers"]
        )
        assert len(payload["canonical_state_sha256"]) == 64
