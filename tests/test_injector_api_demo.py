from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from lineage_migrator.api import create_app
from lineage_migrator.injector import run_all_scenarios

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_all_anomaly_scenarios_match_and_rollback(tmp_path, sample):
    results = run_all_scenarios(
        sample,
        PROJECT_ROOT / "samples" / "anomalies.json",
        tmp_path / "runs",
    )
    assert len(results) == 6
    sequence = [item["actual_code"] for item in results]
    assert sequence == [
        "E_RULE_ORDER",
        "E_RULE_ORDER",
        "E_CYCLE",
        "E_TARGET_MISSING",
        "E_STALE_SOURCE",
        "E_VERSION_REGRESS",
    ]
    assert all(item["matched"] for item in results)
    assert all(item["rolled_back_cleanly"] for item in results)


def test_api_full_flow(tmp_path, sample, monkeypatch):
    monkeypatch.setenv("LINEAGE_DB_PATH", str(tmp_path / "api.db"))
    app = create_app()
    client = TestClient(app)

    assert client.get("/health").json()["initialized"] is False
    loaded = client.post("/sample/load").json()
    assert loaded["edge_count"] == 9

    replayed = client.post("/versions/replay").json()
    assert replayed["last_version"] == 5

    versions = client.get("/versions").json()["versions"]
    assert [item["version"] for item in versions] == [0, 1, 2, 3, 4, 5]

    snapshot = client.get("/snapshots/3").json()
    assert snapshot["version"] == 3

    diff = client.post("/diff", json={"from_version": 0, "to_version": 5}).json()
    assert diff["summary"]["endpoint_changed"] >= 1

    rolled = client.post("/rollback", json={"target_version": 3}).json()
    assert rolled["last_version"] == 3

    bad_payload = {
        "version": 4,
        "rules": [
            {
                "rule_id": "ghost",
                "rule_type": "rename",
                "source_ref": "orders.ghost",
                "target_ref": "orders.ghost2",
            }
        ],
    }
    response = client.post("/versions/apply", json=bad_payload)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "E_TARGET_MISSING"

    scenarios = client.get("/anomalies").json()["scenarios"]
    assert len(scenarios) == 6
    run_all = client.post("/anomalies/run-all").json()["results"]
    assert all(item["matched"] for item in run_all)


def test_demo_script_is_deterministic():
    env_path = PROJECT_ROOT / ".venv" / "bin" / "python"
    python = str(env_path) if env_path.exists() else sys.executable
    first = subprocess.run(
        [python, "scripts/run_demo.py"], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True
    )
    result_a = json.loads((PROJECT_ROOT / "artifacts" / "demo_result.json").read_text(encoding="utf-8"))
    second = subprocess.run(
        [python, "scripts/run_demo.py"], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True
    )
    result_b = json.loads((PROJECT_ROOT / "artifacts" / "demo_result.json").read_text(encoding="utf-8"))
    assert "OVERALL: PASS" in first.stdout
    assert "OVERALL: PASS" in second.stdout
    comparable_a = {key: value for key, value in result_a.items() if key != "replay_runs"}
    comparable_b = {key: value for key, value in result_b.items() if key != "replay_runs"}
    assert comparable_a == comparable_b
