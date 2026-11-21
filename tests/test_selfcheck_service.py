from __future__ import annotations

from pathlib import Path

from driftmon.service import MonitoringService
from driftmon.web import create_app


def test_service_demo_and_selfcheck(tmp_path: Path) -> None:
    service = MonitoringService(tmp_path, initial_cursor=2)
    initial = service.reset(clear=True)
    assert initial["last_run"]["incremental"]["coverage"]["affected_covered"] is True
    advanced = service.advance()
    assert len(advanced.recomputed_products) == 5
    assert len(advanced.reused_products) == 10
    first_alert = service._alerts().load_current()
    assert first_alert is not None
    trace = service.trace_alert(first_alert["alert_id"])
    assert trace["matches"] is True
    check = service.selfcheck()
    assert check["ok"] is True
    assert all(item["passed"] for item in check["checks"])
    assert len(check["latest_affected"]) == 5
    assert len(check["latest_recomputed"]) == 5
    assert len(check["latest_reused"]) == 10


def test_web_dashboard_and_error_codes(tmp_path: Path) -> None:
    app = create_app(tmp_path / "app", initial_cursor=2)
    client = app.test_client()
    page = client.get("/")
    assert page.status_code == 200
    assert "DriftMon" in page.get_data(as_text=True)
    state = client.get("/api/state")
    assert state.status_code == 200
    assert state.get_json()["state"]["cursor"] == 2

    invalid_window = client.post("/api/window", json={"window_size": 0})
    assert invalid_window.status_code == 400
    error = invalid_window.get_json()["error"]
    assert error["code"] == "E_CONFIG_INVALID"
    assert error["field"] == "window_size"
    errors = client.get("/api/state").get_json()["recent_errors"]
    assert errors[-1]["code"] == "E_CONFIG_INVALID"

    advanced = client.post("/api/advance", json={})
    assert advanced.status_code == 200
    payload = advanced.get_json()
    assert payload["run"]["cursor"] == 3
    assert payload["run"]["incremental"]["coverage"]["affected_covered"] is True
    test_key = payload["run"]["results"][0]["test_key"]
    explained = client.get(f"/api/cache/{test_key}")
    assert explained.status_code == 200
    assert explained.get_json()["key_components"]["artifact_type"] == "feature_test"

