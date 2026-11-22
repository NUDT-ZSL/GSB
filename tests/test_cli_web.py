from __future__ import annotations

import json

from ckptlab.web import create_app

from conftest import ROOT, manager_for, plan_v1, run_cli, shard_policy


def test_cli_status_is_stable_sorted_json(tmp_path):
    manager = manager_for(tmp_path)
    manager.save(plan_v1(), shard_policy())
    common = (
        "--db",
        str(tmp_path / "ledger.sqlite3"),
        "--store",
        str(tmp_path / "store"),
    )
    first = run_cli("status", *common)
    second = run_cli("status", *common)
    assert first.returncode == 0
    assert first.stdout == second.stdout
    payload = json.loads(first.stdout)
    assert payload["recovery_decision"]["decision"] == "visible_committed"
    assert list(payload["summary"]) == sorted(payload["summary"])


def test_web_workbench_read_only_endpoints(tmp_path):
    manager = manager_for(tmp_path)
    saved = manager.save(plan_v1(), shard_policy())
    app = create_app(tmp_path / "ledger.sqlite3", tmp_path / "store")
    client = app.test_client()

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.get_json()["read_only"] is True

    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.get_json()["pointer"]["version"] == 1

    verify = client.get("/api/verify")
    assert verify.status_code == 200
    assert verify.get_json()["manifest_digest"] == saved["manifest_digest"]

    restore = client.get("/api/restore")
    assert restore.status_code == 200
    assert restore.get_json()["restored"] is True
    assert client.get("/").status_code == 200


def test_web_rejects_non_loopback_host(tmp_path):
    process = run_cli(
        "serve",
        "--db",
        str(tmp_path / "ledger.sqlite3"),
        "--store",
        str(tmp_path / "store"),
        "--host",
        "0.0.0.0",
    )
    assert process.returncode == 1
    assert json.loads(process.stderr)["error"]["code"] == "WEB_HOST_NOT_LOOPBACK"
