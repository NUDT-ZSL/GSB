from __future__ import annotations

import json
from pathlib import Path

from alertlab.web import create_app

from conftest import ROOT


def test_web_load_patch_status_verify(tmp_path: Path) -> None:
    app = create_app(ROOT, database_path=tmp_path / "web.sqlite3")
    app.config["TESTING"] = True
    client = app.test_client()
    assert client.get("/").status_code == 200
    assert client.get("/health").get_json()["bind"] == "127.0.0.1"
    load = client.post("/load", json={})
    assert load.status_code == 200
    assert load.get_json()["status"]["summary"]["cluster_count"] == 6
    status_one = client.get("/status")
    status_two = client.get("/status")
    assert status_one.data == status_two.data
    assert status_one.headers["Content-Type"].startswith("application/json")
    patch = client.post(
        "/patch",
        json={"patch_file": "samples/patches/dependency_patch.json"},
    )
    assert patch.status_code == 200
    assert patch.get_json()["applied"] is True
    verify = client.post("/verify", json={})
    assert verify.status_code == 200
    assert verify.get_json()["ok"] is True


def test_web_business_errors_have_http_status_and_code(tmp_path: Path) -> None:
    app = create_app(ROOT, database_path=tmp_path / "web-errors.sqlite3")
    app.config["TESTING"] = True
    client = app.test_client()
    response = client.post(
        "/patch",
        json={
            "patch_id": "unknown",
            "logical_clock_ms": 1_700_002_000_000,
            "changes": [{"rule_name": "Nope", "set": {"silence_labels": []}}],
        },
    )
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "E_NOT_FOUND"
