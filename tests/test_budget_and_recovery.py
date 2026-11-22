from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from conftest import ROOT, SAMPLES


def _run(data_dir: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    command = [sys.executable, "-m", "multipart_store", "--data-dir", str(data_dir), *args]
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        command,
        cwd=ROOT,
        env=merged_env,
        text=True,
        capture_output=True,
    )


def test_round_budget_abandons_only_unstarted_events(loaded_budget):
    store, _ = loaded_budget
    first = store.upload_round()
    assert first["budget_units"] == 3
    assert first["budget_exhausted"] is True
    assert first["abandoned_budget_event_ids"] == [4, 5]
    assert first["succeeded_count"] == 3
    assert all(event["started"] for event in first["events"])
    status = store.status()
    session = status["sessions"][0]
    assert session["visible_version"] == 3
    assert session["disposition_counts"]["abandoned_budget"] == 2
    events = {event["event_id"]: event for event in status["events"]}
    assert events[4]["status"] == "pending"
    assert events[4]["started"] == 0

    second = store.upload_round()
    assert second["abandoned_budget_event_ids"] == []
    assert second["succeeded_count"] == 2
    assert store.status()["sessions"][0]["visible_version"] == 5


def test_completion_recovery_after_prepare_crash(tmp_path: Path):
    data_dir = tmp_path / "store"
    assert _run(data_dir, "load", str(SAMPLES / "budget.json")).returncode == 0
    assert _run(data_dir, "upload").returncode == 0
    assert _run(data_dir, "upload").returncode == 0

    crashed = _run(
        data_dir,
        "complete",
        "--session-id",
        "sess-budget",
        env={"MULTIPART_FAULT_POINT": "completion_after_prepare"},
    )
    assert crashed.returncode == 75

    from multipart_store.service import MultipartStore

    store = MultipartStore(data_dir)
    status = store.status()
    assert status["sessions"][0]["phase"] == "pending_completion"
    assert status["completion_attempts"][0]["status"] == "prepared"

    resumed = store.resume(cleanup=True)
    assert resumed["actions"][0]["action"] == "replay_completion"
    status = store.status()
    assert status["sessions"][0]["phase"] == "completed"
    assert status["completion_attempts"][0]["status"] == "committed"
    assert store.verify("sess-budget")["valid"] is True


def test_completion_recovery_after_materialize_crash(tmp_path: Path):
    data_dir = tmp_path / "store"
    assert _run(data_dir, "load", str(SAMPLES / "budget.json")).returncode == 0
    assert _run(data_dir, "upload").returncode == 0
    assert _run(data_dir, "upload").returncode == 0
    crashed = _run(
        data_dir,
        "complete",
        "--session-id",
        "sess-budget",
        env={"MULTIPART_FAULT_POINT": "completion_after_materialize"},
    )
    assert crashed.returncode == 75

    from multipart_store.service import MultipartStore

    store = MultipartStore(data_dir)
    attempt = store.status()["completion_attempts"][0]
    assert attempt["status"] == "materialized"
    assert Path(store.db.resolve_reference(attempt["final_path"])).exists()
    store.resume()
    assert store.status()["sessions"][0]["phase"] == "completed"
    assert store.verify("sess-budget")["valid"] is True


def test_part_crash_leaves_recoverable_orphan_then_replays(tmp_path: Path):
    data_dir = tmp_path / "store"
    assert _run(data_dir, "load", str(SAMPLES / "budget.json")).returncode == 0
    crashed = _run(
        data_dir,
        "upload",
        env={"MULTIPART_FAULT_POINT": "part_after_rename"},
    )
    assert crashed.returncode == 75

    from multipart_store.service import MultipartStore

    store = MultipartStore(data_dir)
    status = store.status()
    assert status["events"][0]["status"] == "started"
    assert len(status["orphan_parts"]) == 1
    orphan_path = Path(status["orphan_parts"][0]["absolute_path"])
    assert orphan_path.exists()

    resumed = store.resume(cleanup=True)
    assert resumed["cleanup"]["count"] == 1
    assert not orphan_path.exists()
    assert store.status()["events"][0]["status"] == "pending"

    first = store.upload_round()
    second = store.upload_round()
    assert first["succeeded_count"] == 3
    assert second["succeeded_count"] == 2
    assert store.complete("sess-budget")["status"] == "completed"


def test_cleanup_of_aborted_and_superseded_parts_is_logged(uploaded_primary):
    store, _ = uploaded_primary
    store.complete("sess-main")
    before = store.status()
    assert before["orphan_parts"]
    reasons = {item["reason"] for item in before["orphan_parts"]}
    assert "aborted_session_unreferenced" in reasons
    assert "superseded_version_unreferenced" in reasons

    resumed = store.resume(cleanup=True)
    assert resumed["cleanup"]["count"] == len(before["orphan_parts"])
    after = store.status()
    assert after["orphan_parts"] == []
    assert len(after["gc_log"]) == resumed["cleanup"]["count"]
    assert {item["reason"] for item in after["gc_log"]} == reasons
    assert store.verify("sess-main")["valid"] is True
