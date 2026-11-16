import asyncio
from dataclasses import replace

import httpx
import pytest

from pipeline.alignment import alignment_report
from pipeline.config import TaskConfig
from pipeline.db import Database
from pipeline.scheduler import CrashInterrupt, run_task
from site_server.app import SiteState, create_app

MAIN_FAULTS = {
    "always_503": ["beta/BIZ-004"],
    "flaky_503": {"alpha/BIZ-002": 2},
    "always_slow": ["beta/BIZ-005"],
    "flaky_slow": {"alpha/BIZ-003": 1},
    "session_trap": ["alpha/BIZ-006"],
    "flaky_session": {"alpha/BIZ-005": 1},
}


def make_client(state):
    app = create_app(state)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://site.test")


@pytest.fixture()
def env(tmp_path):
    state = SiteState()
    state.faults.update(MAIN_FAULTS)
    state.slow_seconds = 0.4
    state.reset_transient()
    db = Database(str(tmp_path / "it.db"))
    client = make_client(state)
    yield state, db, client
    asyncio.run(client.aclose())
    db.close()


def run(coro):
    return asyncio.run(coro)


def test_main_task_counts_and_budgets(env):
    state, db, client = env
    cfg = TaskConfig(task_id="main", sources=["alpha", "beta"], concurrency=4,
                     retry_wait_budget=8, request_timeout=0.2)
    summary = run(run_task(cfg, db, client))
    assert summary["status"] == "partial"
    assert summary["total_discovered"] == 12
    assert summary["confirmed_count"] == 8
    assert summary["unresolved_count"] == 1
    assert summary["failed_count"] == 3
    assert summary["failure_classes"] == {
        "E_RETRY_EXHAUSTED": 1,
        "E_SESSION": 1,
        "E_TIMEOUT": 1,
    }
    budgets = summary["budgets"]
    assert budgets["in_flight_within_budget"]
    assert 2 <= budgets["in_flight_peak"] <= 4
    assert budgets["retry_wait_within_budget"]
    assert summary["session_replays"] >= 3
    assert summary["retries"] >= 5
    assert summary["render_observations"] == 12  # 4 pages x 3 observations


def test_replay_deduplicates(env):
    state, db, client = env
    cfg = TaskConfig(task_id="main", sources=["alpha", "beta"], request_timeout=0.2)
    run(run_task(cfg, db, client))
    before = db.normalized_stats()["rows"]
    replay = replace(cfg, task_id="replay")
    summary = run(run_task(replay, db, client))
    assert summary["duplicate_count"] == 8
    assert summary["normalized_records_for_task"] == 0
    assert db.normalized_stats()["rows"] == before
    assert db.normalized_stats()["duplicate_key_groups"] == 0


def test_crash_and_resume_consistency(env):
    state, db, client = env
    state.faults.update({k: ([] if isinstance(v, list) else {}) for k, v in MAIN_FAULTS.items()})
    state.reset_transient()
    cfg = TaskConfig(task_id="recovery", sources=["alpha"], concurrency=1,
                     retry_wait_budget=2)
    with pytest.raises(CrashInterrupt):
        run(run_task(cfg, db, client, crash_after_confirmed=4))
    cursor, pre_keys = db.load_checkpoint("recovery")
    assert len(pre_keys) == 4
    assert cursor == {"source_index": 0, "page": 2}
    summary = run(run_task(cfg, db, client))
    post_keys = [i["item_key"] for i in db.task_items("recovery") if i["status"] == "confirmed"]
    assert summary["status"] == "completed"
    assert len(post_keys) == 6
    assert sorted(set(pre_keys) & set(post_keys)) == sorted(pre_keys)
    assert summary["confirmed_by_version"] == {"alpha/1": 3, "alpha/2": 3}


def test_render_timeout_records_snapshot(tmp_path):
    state = SiteState()
    state.unstable_pages = ["alpha/1"]
    db = Database(str(tmp_path / "rt.db"))
    client = make_client(state)
    cfg = TaskConfig(task_id="rt", sources=["alpha"], render_max_observations=4,
                     render_interval=0)
    summary = asyncio.run(run_task(cfg, db, client))
    asyncio.run(client.aclose())
    assert summary["page_errors"] == {"/list/alpha?page=1": "E_RENDER_TIMEOUT"}
    snapshots = db.snapshots("rt")
    assert len(snapshots) == 1 and "items-container" in snapshots[0]["body"]
    db.close()


def test_alignment_after_version_switch(env):
    state, db, client = env
    cfg = TaskConfig(task_id="main", sources=["alpha", "beta"], request_timeout=0.2)
    run(run_task(cfg, db, client))
    state.version_overrides = {"alpha/BIZ-001": 2, "beta/BIZ-001": 2}
    probe = TaskConfig(task_id="probe", sources=["alpha", "beta"],
                       biz_filter=["BIZ-001"], request_timeout=0.2)
    run(run_task(probe, db, client))
    report = alignment_report(db, "BIZ-001")
    assert report["versions"] == ["alpha/1", "alpha/2", "beta/1", "beta/2"]
    assert len(report["pairs"]) == 6
    assert report["summary"]["all_required_complete"]
    assert report["summary"]["version_evidence_consistent"]


def test_in_flight_budget_cap_enforced(tmp_path):
    state = SiteState()
    state.faults["always_slow"] = [f"alpha/BIZ-{n:03d}" for n in range(1, 7)]
    state.slow_seconds = 0.3
    state.reset_transient()
    db = Database(str(tmp_path / "cap.db"))
    client = make_client(state)
    cfg = TaskConfig(task_id="cap", sources=["alpha"], concurrency=2,
                     retry_wait_budget=4, request_timeout=5.0)
    summary = asyncio.run(run_task(cfg, db, client))
    asyncio.run(client.aclose())
    assert summary["budgets"]["in_flight_peak"] == 2
    assert summary["budgets"]["in_flight_within_budget"]
    assert summary["confirmed_count"] == 6
    db.close()
