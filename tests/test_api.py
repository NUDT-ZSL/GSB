from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_read_endpoints_and_business_errors() -> None:
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        requests_payload = client.get("/api/requests").json()
        assert requests_payload["count"] == 16
        request_id = requests_payload["items"][0]["request_id"]

        clips = client.get("/api/athletes/A001/clips")
        assert clips.status_code == 200
        assert clips.json()["count"] == 24

        detail = client.get(f"/api/requests/{request_id}")
        assert detail.status_code == 200
        assert detail.json()["result"]["candidate_count"] == 12

        missing = client.get("/api/athletes/DOES-NOT-EXIST")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "ATHLETE_NOT_FOUND"

        missing_request = client.get("/api/requests/DOES-NOT-EXIST")
        assert missing_request.status_code == 404
        assert missing_request.json()["error"]["code"] == "REQUEST_NOT_FOUND"

        invalid_weights = client.post(
            f"/api/requests/{request_id}/rank",
            json={"weights": {"quality": 0.2, "risk": 0.2, "profile": 0.2}},
        )
        assert invalid_weights.status_code == 422
        assert invalid_weights.json()["error"]["code"] == "INVALID_WEIGHTS"

        out_of_range = client.post(
            f"/api/requests/{request_id}/rank",
            json={"weights": {"quality": 1.2, "risk": -0.2, "profile": 0.0}},
        )
        assert out_of_range.status_code == 422
        assert out_of_range.json()["error"]["code"] == "INVALID_WEIGHTS"

        verification = client.get(f"/api/requests/{request_id}/verification")
        assert verification.status_code == 200
        assert verification.json()["passed"] is True

        extra_field = client.post(
            f"/api/requests/{request_id}/rank",
            json={"weights": {"quality": 0.4, "risk": 0.3, "profile": 0.3, "extra": 1}},
        )
        assert extra_field.status_code == 422
        assert extra_field.json()["error"]["code"] == "INVALID_REQUEST"


def test_ui_is_served_locally() -> None:
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "运动动作精排台" in response.text
        assert client.get("/static/app.js").status_code == 200
