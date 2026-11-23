from __future__ import annotations

from pathlib import Path

from scaler.engine import Autoscaler
from scaler.storage import SQLiteStore
from scaler.workbench import create_app


def test_workbench_renders_status_and_recovery_api(
    tmp_path: Path, spec
) -> None:
    store = SQLiteStore(tmp_path / "state.db")
    engine = Autoscaler(store, spec)
    engine.run(rounds=1)
    app = create_app(store=store, autoscaler=engine, spec=spec)
    app.config.update(TESTING=True)
    client = app.test_client()

    page = client.get("/")
    assert page.status_code == 200
    assert b"Elastic Scalpel" in page.data
    assert "副本生命周期".encode() in page.data

    status = client.get("/api/status")
    assert status.status_code == 200
    payload = status.get_json()
    assert payload["ok"] is True
    assert payload["replicas"]
    assert payload["snapshot_versions"]
    assert payload["nodes"]

    recovered = client.post("/api/recover")
    assert recovered.status_code == 200
    assert recovered.get_json()["ok"] is True

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.get_json()["pending_rounds"] == 0
