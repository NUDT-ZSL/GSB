from __future__ import annotations

import time
from typing import Any

import httpx

from app.db import Database


def create_task(client: httpx.Client, **overrides: Any) -> str:
    payload = {
        "name": "test",
        "wave": "baseline",
        "concurrency": 2,
        "delay_profile": "none",
        "fault_profile": "none",
        "render_timeout_s": 3,
        "max_render_observations": 80,
        "max_retries": 3,
        "backoff_base_ms": 20,
        "backoff_max_ms": 200,
        "high_latency_ms": 500,
        "max_session_replays": 5,
        "request_timeout_s": 8,
    }
    payload.update(overrides)
    response = client.post("/api/tasks", json=payload)
    response.raise_for_status()
    return response.json()["task_id"]


def wait_task(client: httpx.Client, task_id: str, timeout: float = 60) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = client.get(f"/api/tasks/{task_id}").json()
        if detail["task"]["status"] not in {"created", "running"}:
            return detail
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} timeout")


def test_dynamic_render_versions_increments_and_replay(server):
    with httpx.Client(base_url=server.base_url, timeout=20) as client:
        baseline_id = create_task(
            client,
            name="baseline",
            wave="baseline",
            concurrency=3,
            delay_profile="slow",
            fault_profile="mixed",
        )
        baseline = wait_task(client, baseline_id)
        assert baseline["task"]["status"] == "completed"
        assert baseline["progress"]["completed_items"] == 6

        snapshots = client.get(f"/api/tasks/{baseline_id}/snapshots").json()["snapshots"]
        for page in (1, 2, 3):
            page_snaps = [item for item in snapshots if item["page_no"] == page]
            assert len(page_snaps) >= 2
            assert page_snaps[-1]["accepted"] == 1
            assert page_snaps[-1]["stable_count"] >= 2
            assert page_snaps[-1]["container_present"] == 1

        assert baseline["task"]["summary"]["session_replay_count"] >= 1
        raws = Database(server.db_path)
        with raws.connection() as conn:
            expired_raws = conn.execute(
                "SELECT COUNT(*) FROM raw_responses WHERE body LIKE '%E_SESSION_EXPIRED%'"
            ).fetchone()[0]
        assert expired_raws == 0

        incremental_id = create_task(
            client,
            name="incremental",
            wave="incremental",
            concurrency=5,
            delay_profile="bursty",
            fault_profile="mixed",
        )
        incremental = wait_task(client, incremental_id)
        assert incremental["task"]["status"] == "completed"
        assert incremental["task"]["summary"]["new_record_count"] == 4

        chain_001 = client.get("/api/business/AUD-2026-001").json()
        assert [item["parser_version"] for item in chain_001["records"]] == ["v1", "v1"]
        assert chain_001["records"][0]["schema_evidence"]["selected_parser"] == "v1"
        chain_002 = client.get("/api/business/AUD-2026-002").json()
        assert chain_002["records"][0]["parser_version"] == "v2"

        unknown = client.get("/api/business/AUD-2026-005").json()["records"][0]
        assert unknown["unresolved"] is True
        assert unknown["schema_evidence"]["selected_parser"] == "raw-fallback"
        assert set(unknown["fields"]) == {"legacy_code", "legacy_note", "legacy_owner"}
        missing = client.get("/api/business/AUD-2026-006").json()["records"][0]
        assert missing["schema_version"] == "__missing__"
        assert missing["unresolved"] is True

        edges = client.get(f"/api/tasks/{incremental_id}/differences").json()["version_edges"]
        by_business = {edge["business_id"]: edge for edge in edges}
        assert by_business["AUD-2026-001"]["added_fields"] == ["riskLevel"]
        assert set(by_business["AUD-2026-001"]["missing_fields"]) == {
            "amount",
            "notes",
            "owner",
            "title",
        }
        assert by_business["AUD-2026-002"]["added_fields"] == ["reviewer"]
        assert by_business["AUD-2026-003"]["added_fields"] == []

        summary = Database(server.db_path).global_consistency_summary()
        assert summary["business_count"] == 7
        assert summary["record_count"] == 10
        assert summary["edge_count"] == 3

        for record in chain_001["records"]:
            raw = client.get(f"/api/raw/{record['raw_response_id']}").json()["raw"]
            assert raw["kind"] == "detail"
            assert f"data-field=\"status\"" in raw["body"]


def test_conflict_and_revision_idempotency(server):
    with httpx.Client(base_url=server.base_url, timeout=20) as client:
        conflict_id = create_task(
            client,
            name="conflict",
            wave="conflict",
            concurrency=2,
            delay_profile="none",
            fault_profile="none",
        )
        conflict = wait_task(client, conflict_id)
        assert conflict["task"]["status"] == "partial"
        assert conflict["task"]["task_code"] == "E_CONFLICT"
        assert conflict["task"]["summary"]["task_failure_count"] == 0
        assert conflict["task"]["summary"]["item_failure_count"] == 1
        assert conflict["task"]["summary"]["conflict_count"] == 1
        diff = client.get(f"/api/tasks/{conflict_id}/differences").json()
        assert len(diff["new_records"]) == 1
        assert len(diff["conflicts"]) == 1
        assert diff["item_failures"][0]["business_code"] == "E_CONFLICT"

        db = Database(server.db_path)
        with db.connection() as conn:
            rows = conn.execute(
                "SELECT COUNT(*) FROM normalized_records WHERE business_id='AUD-2026-101'"
            ).fetchone()[0]
            observations = conn.execute(
                "SELECT COUNT(*) FROM record_observations WHERE business_id='AUD-2026-101'"
            ).fetchone()[0]
        assert rows == 1
        assert observations == 2

        first = create_task(client, name="dup-1", wave="baseline", concurrency=3)
        assert wait_task(client, first)["task"]["status"] == "completed"
        second = create_task(
            client, name="dup-2", wave="baseline", concurrency=4, delay_profile="bursty"
        )
        second_detail = wait_task(client, second)
        assert second_detail["task"]["status"] == "completed"
        assert second_detail["task"]["summary"]["new_record_count"] == 0
        assert second_detail["task"]["summary"]["duplicate_record_count"] == 6
        summary = db.global_consistency_summary()
        assert summary["business_count"] == 7
        assert summary["record_count"] == 7


def test_render_timeout_keeps_reviewable_snapshots(server):
    with httpx.Client(base_url=server.base_url, timeout=20) as client:
        task_id = create_task(
            client,
            name="render-timeout",
            wave="baseline",
            fault_profile="render_timeout",
            render_timeout_s=0.3,
            max_render_observations=30,
        )
        detail = wait_task(client, task_id)
        assert detail["task"]["status"] == "recoverable"
        assert detail["task"]["task_code"] == "E_RENDER_TIMEOUT"
        snapshots = detail["task"]["summary"]["render_observations"]
        assert snapshots >= 2
        rows = client.get(f"/api/tasks/{task_id}/snapshots").json()["snapshots"]
        assert len(rows) >= 2
        assert all(row["container_present"] == 0 for row in rows)
        assert all(row["accepted"] == 0 for row in rows)
        assert rows[0]["fragment_excerpt"]


def test_retry_exhaustion_is_item_level_not_task_level(server):
    with httpx.Client(base_url=server.base_url, timeout=20) as client:
        task_id = create_task(
            client,
            name="503",
            wave="baseline",
            concurrency=3,
            fault_profile="always_503",
            max_retries=1,
            backoff_base_ms=5,
            backoff_max_ms=10,
        )
        detail = wait_task(client, task_id)
        assert detail["task"]["status"] == "partial"
        assert detail["task"]["task_code"] == "E_RETRY_EXHAUSTED"
        assert detail["task"]["summary"]["task_failure_count"] == 0
        assert detail["task"]["summary"]["item_failure_count"] == 6
        assert detail["task"]["summary"]["failures_by_code"]["E_RETRY_EXHAUSTED"] == 6
        assert all(item["attempts"] == 2 for item in detail["items"])
        events = client.get(f"/api/tasks/{task_id}/events").json()["events"]
        assert any(event["error_code"] == "E_UPSTREAM_503" for event in events)


def test_high_latency_backoff_and_session_replay_limit(server):
    with httpx.Client(base_url=server.base_url, timeout=25) as client:
        latency_id = create_task(
            client,
            name="latency",
            wave="baseline",
            concurrency=2,
            delay_profile="spike",
            fault_profile="none",
            high_latency_ms=100,
            max_retries=4,
            backoff_base_ms=10,
            backoff_max_ms=80,
        )
        latency = wait_task(client, latency_id)
        assert latency["task"]["status"] == "completed"
        events = client.get(f"/api/tasks/{latency_id}/events").json()["events"]
        assert any(event["error_code"] == "E_HIGH_LATENCY" for event in events)

        session_id = create_task(
            client,
            name="session-limit",
            wave="baseline",
            concurrency=1,
            delay_profile="none",
            fault_profile="session_expiry",
            max_session_replays=0,
        )
        session = wait_task(client, session_id)
        assert session["task"]["status"] == "recoverable"
        assert session["task"]["task_code"] == "E_SESSION"
        assert session["task"]["summary"]["recoverable"] is True
        with Database(server.db_path).connection() as conn:
            expired_raws = conn.execute(
                "SELECT COUNT(*) FROM raw_responses WHERE body LIKE '%E_SESSION_EXPIRED%'"
            ).fetchone()[0]
        assert expired_raws == 0


def test_bad_cursor_is_recoverable(server):
    with httpx.Client(base_url=server.base_url, timeout=20) as client:
        task_id = create_task(
            client,
            name="bad-cursor",
            wave="baseline",
            fault_profile="bad_cursor",
            render_timeout_s=2,
        )
        detail = wait_task(client, task_id)
        assert detail["task"]["status"] == "recoverable"
        assert detail["task"]["task_code"] == "E_CURSOR_INVALID"
        snapshots = client.get(f"/api/tasks/{task_id}/snapshots").json()["snapshots"]
        assert len(snapshots) >= 2
        assert any(row["accepted"] == 1 for row in snapshots)
