from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coord_workbench.api import create_app


@pytest.fixture()
def client(sample_dir: Path, workdir: Path) -> TestClient:
    app = create_app(workdir=str(workdir), sample_dir=str(sample_dir))
    return TestClient(app)


def test_verify_endpoint_consistent(client: TestClient) -> None:
    r = client.get("/api/verify")
    assert r.status_code == 200
    body = r.json()
    assert body["consistent"] is True
    assert body["events"] == 18


def test_merge_then_state(client: TestClient) -> None:
    r = client.post("/api/merge")
    assert r.status_code == 200
    schedule = r.json()["schedule"]
    assert all(rec["outcome"] == "COMMITTED" for rec in schedule)

    state = client.get("/api/state").json()
    assert set(state["leaders"]) == {"domain-alpha", "domain-beta", "domain-default"}
    assert all(gen >= 1 for gen in state["lease_generations"].values())
    assert state["field_stats"]["nodes"] == 8
    assert state["buffer_status"]["pending_seqs"] == []
    # stable ordering
    assert list(state["leaders"]) == sorted(state["leaders"])


def test_topology_endpoint(client: TestClient) -> None:
    topo = client.get("/api/topology").json()
    assert topo["injected_clock_ms"] == 120_000
    assert len(topo["views"]) == 7
    assert topo["tombstones"][0]["node"] == "node-g"


def test_recover_endpoint_no_crash(client: TestClient) -> None:
    client.post("/api/merge")
    r = client.post("/api/recover")
    assert r.status_code == 200
    rec = r.json()["recovery"]
    assert rec["half_committed_found"] == []
    assert rec["page_alignment"]["aligned"] is True
    assert all(c["ok"] for c in rec["chains"])


def test_policy_commit_and_regress(client: TestClient) -> None:
    r = client.post("/api/policy/commit", json={
        "version": 1, "snapshot": {"rev": 1}, "changed_nodes": ["node-b"], "clock_ms": 10})
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 1
    assert body["cache_views"]["recompute"]

    r2 = client.post("/api/policy/commit", json={
        "version": 1, "snapshot": {"rev": 2}, "changed_nodes": [], "clock_ms": 20})
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "E_VERSION_REGRESS"

    status = client.get("/api/policy/status").json()
    assert status["current_version"] == 1
