from __future__ import annotations

from pathlib import Path

from quota_ledger.web import create_app


ROOT = Path(__file__).resolve().parents[1]


def test_flask_console_and_demo_api(tmp_path: Path) -> None:
    app = create_app(
        db_path=tmp_path / "web.db",
        samples_dir=ROOT / "samples",
    )
    client = app.test_client()
    assert client.get("/").status_code == 200
    status = client.get("/api/status").get_json()
    assert status["ok"] is True
    assert len(status["tenants"]) == 3
    assert client.get("/api/verify").status_code == 200

    demo = client.post("/api/demo/run", json={})
    assert demo.status_code == 200
    payload = demo.get_json()
    assert payload["ok"] is True
    assert payload["verify"]["ok"] is True
    assert payload["status"]["precision_sensitive"] is True
    assert any(
        item["application"]["status"] == "RELEASED"
        for item in payload["resume_at_200"]["due_releases"]
    )
