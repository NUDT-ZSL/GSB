from __future__ import annotations

from slo_workbench.engine import Engine
from slo_workbench.loader import read_bundle
from slo_workbench.web import create_app


def _client(workspace, sample_dir):
    _, _, clock = read_bundle(sample_dir)
    Engine(workspace).run(clock)
    app = create_app(str(workspace.root))
    app.testing = True
    return app.test_client()


def test_health_and_status(loaded_workspace, sample_dir):
    client = _client(loaded_workspace, sample_dir)
    assert client.get("/health").get_json() == {"status": "ok"}
    status = client.get("/status").get_json()
    assert {item["slo_name"] for item in status["slos"]} == {
        "checkout-availability",
        "search-latency",
    }


def test_window_and_burn_endpoints(loaded_workspace, sample_dir):
    client = _client(loaded_workspace, sample_dir)
    response = client.get("/window/checkout-availability/3?verify=1")
    payload = response.get_json()
    assert payload["verification"]["status"] == "match"
    assert payload["report"]["window_no"] == 3

    burn = client.get("/burn/checkout-availability?verify=1").get_json()
    assert burn["verification"]["status"] == "match"


def test_verify_endpoint_reports_matches(loaded_workspace, sample_dir):
    client = _client(loaded_workspace, sample_dir)
    payload = client.get("/verify").get_json()
    assert payload["results"]
    assert all(item["status"] in ("match", "skipped_empty") for item in payload["results"])


def test_unknown_slo_returns_business_error(loaded_workspace, sample_dir):
    client = _client(loaded_workspace, sample_dir)
    response = client.get("/window/nope/1")
    assert response.status_code == 404
    assert response.get_json()["code"] == "E_NOT_FOUND"


def test_resume_endpoint(loaded_workspace, sample_dir):
    client = _client(loaded_workspace, sample_dir)
    response = client.post("/resume")
    assert response.status_code == 200
    assert "recovered" in response.get_json()
