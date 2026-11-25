from __future__ import annotations

import sqlite3

from health_machine.verify import verify_store
from health_machine.store import Store
from health_machine.web import create_app


def test_flask_health_and_status(completed_workspace):
    app = create_app(completed_workspace.db)
    client = app.test_client()
    assert client.get("/healthz").get_json()["ok"] is True
    response = client.get("/api/status")
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["instance_count"] == 4


def test_verify_detects_field_level_tampering(completed_workspace):
    db = completed_workspace.db
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE window_summaries SET score_bp=score_bp+1 WHERE instance_id='inst-steady' AND window_index=1"
    )
    conn.commit()
    conn.close()

    report = verify_store(Store(db), "inst-steady", window_index=1)
    assert report["ok"] is False
    paths = [diff["path"] for instance in report["instances"] for diff in instance["diffs"]]
    assert any(path.endswith("window_summaries[inst-steady/1].score_bp") for path in paths)


def test_verify_detects_state_tampering(completed_workspace):
    db = completed_workspace.db
    conn = sqlite3.connect(db)
    conn.execute("UPDATE instance_state SET failure_streak=99 WHERE instance_id='inst-flap'")
    conn.commit()
    conn.close()
    report = verify_store(Store(db), "inst-flap")
    assert report["ok"] is False
    paths = [diff["path"] for instance in report["instances"] for diff in instance["diffs"]]
    assert any("instance_state[inst-flap].failure_streak" in path for path in paths)
