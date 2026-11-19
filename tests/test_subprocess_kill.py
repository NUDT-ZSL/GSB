"""Requirement 6 (hard kill): recover after the worker process is SIGKILLed."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from conftest import CLOCK_BASE, CLOCK_SEQ, build_plan, make_clock
from recovery_pipeline.clock import LogicalClock
from recovery_pipeline.executor import Orchestrator
from recovery_pipeline.recovery import recover
from recovery_pipeline.store import Store

PLAN_VERSION = 2


def test_subprocess_hard_kill_recovers_identically(tmp_path, samples, target):
    # uninterrupted reference run (separate store)
    clean_store = Store(tmp_path / "clean" / "pipeline.sqlite3")
    plan, _, _ = build_plan(samples)
    clean_store.install_plan(PLAN_VERSION, plan.view(), plan.migration_map)
    ref = Orchestrator(clean_store, make_clock(), target.url)
    ref.run_failover(plan, PLAN_VERSION)
    ref.run_failback(plan, PLAN_VERSION)
    expected = ref.persisted_view(PLAN_VERSION)

    # worker process is hard-killed mid-failover
    db_path = tmp_path / "killed" / "pipeline.sqlite3"
    worker = Path(__file__).parent / "kill_worker.py"
    proc = subprocess.run(
        [sys.executable, str(worker), str(tmp_path / "samples"),
         str(db_path), target.url],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 137, proc.stderr

    # recovery process: resume the injected clock after the persisted watermark
    store = Store(db_path)
    conn = store.connect()
    row = conn.execute("SELECT clock_ms FROM clock_watermark WHERE id = 1") \
        .fetchone()
    conn.close()
    watermark = row["clock_ms"]
    remaining = [v for v in CLOCK_SEQ if v > watermark]
    orch = Orchestrator(store, LogicalClock(remaining), target.url)
    report = recover(store, plan, PLAN_VERSION, orch, clock_ms=CLOCK_BASE)
    assert report["half_committed_replayed"] == [
        {"service": "svc-alpha", "action_id": "alpha-switch"}]

    orch.run_failover(plan, PLAN_VERSION)
    orch.run_failback(plan, PLAN_VERSION)
    actual = orch.persisted_view(PLAN_VERSION)
    assert json.dumps(actual, sort_keys=True) == \
        json.dumps(expected, sort_keys=True)
