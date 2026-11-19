"""Requirement 6: forced-kill recovery must match uninterrupted runs."""

from __future__ import annotations

import json
import urllib.request

import pytest

from conftest import CLOCK_BASE, build_plan, make_clock
from recovery_pipeline.executor import Orchestrator, SimulatedCrash
from recovery_pipeline.recovery import recover
from recovery_pipeline.store import Store

PLAN_VERSION = 2


def _http_get(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.read().decode("utf-8")


def _apply_count(target_url):
    return json.loads(_http_get(target_url + "/stats"))["apply_count"]


def _run_uninterrupted(tmp_path, samples, target_url):
    store = Store(tmp_path / "clean" / "pipeline.sqlite3")
    plan, _, _ = build_plan(samples)
    store.install_plan(PLAN_VERSION, plan.view(), plan.migration_map)
    orch = Orchestrator(store, make_clock(), target_url)
    orch.run_failover(plan, PLAN_VERSION)
    orch.run_failback(plan, PLAN_VERSION)
    return orch.persisted_view(PLAN_VERSION)


def test_crash_mid_failover_recovers_identically(tmp_path, samples, target):
    expected = _run_uninterrupted(tmp_path, samples, target.url)

    store = Store(tmp_path / "crashed" / "pipeline.sqlite3")
    plan, _, _ = build_plan(samples)
    store.install_plan(PLAN_VERSION, plan.view(), plan.migration_map)

    def killer(phase, ctx):
        if phase == "after_intent" and ctx["action_id"] == "alpha-switch":
            raise SimulatedCrash("killed after intent, before dispatch")

    clock = make_clock()
    orch = Orchestrator(store, clock, target.url, crash_hook=killer)
    with pytest.raises(SimulatedCrash):
        orch.run_failover(plan, PLAN_VERSION)

    # New process: fresh orchestrator, decisions only from persisted state.
    orch2 = Orchestrator(store, clock, target.url)
    report = recover(store, plan, PLAN_VERSION, orch2, clock_ms=CLOCK_BASE)
    assert report["half_committed_replayed"] == [
        {"service": "svc-alpha", "action_id": "alpha-switch"}]
    missing_ids = {a["action_id"] for a in report["missing_replayed"]}
    assert "alpha-start" in missing_ids
    assert len(missing_ids) == 1  # only the in-flight service is replayed

    orch2.run_failover(plan, PLAN_VERSION)
    orch2.run_failback(plan, PLAN_VERSION)
    actual = orch2.persisted_view(PLAN_VERSION)
    assert json.dumps(actual, sort_keys=True) == \
        json.dumps(expected, sort_keys=True)
    # across both runs each of the 6 distinct actions was applied exactly once
    assert _apply_count(target.url) == 6


def test_crash_after_dispatch_recovers_without_duplicate_apply(
        tmp_path, samples, target):
    store = Store(tmp_path / "crash2" / "pipeline.sqlite3")
    plan, _, _ = build_plan(samples)
    store.install_plan(PLAN_VERSION, plan.view(), plan.migration_map)

    def killer(phase, ctx):
        if phase == "after_dispatch" and ctx["action_id"] == "alpha-stop":
            raise SimulatedCrash("killed after dispatch, before commit")

    clock = make_clock()
    orch = Orchestrator(store, clock, target.url, crash_hook=killer)
    with pytest.raises(SimulatedCrash):
        orch.run_failover(plan, PLAN_VERSION)

    orch2 = Orchestrator(store, clock, target.url)
    report = recover(store, plan, PLAN_VERSION, orch2, clock_ms=CLOCK_BASE)
    assert report["half_committed_replayed"] == [
        {"service": "svc-alpha", "action_id": "alpha-stop"}]
    orch2.run_failover(plan, PLAN_VERSION)
    # alpha-stop was dispatched twice but applied once (idempotent target)
    assert _apply_count(target.url) == 6


def test_crash_mid_failback_resumes_from_persisted_rung(
        tmp_path, samples, target):
    expected = _run_uninterrupted(tmp_path, samples, target.url)

    store = Store(tmp_path / "crash3" / "pipeline.sqlite3")
    plan, _, _ = build_plan(samples)
    store.install_plan(PLAN_VERSION, plan.view(), plan.migration_map)
    clock = make_clock()
    orch = Orchestrator(store, clock, target.url)
    orch.run_failover(plan, PLAN_VERSION)

    original_advance = orch._advance_rung

    def crashing_advance(plan_version, service, rung, pct, clock_ms):
        original_advance(plan_version, service, rung, pct, clock_ms)
        if service == "svc-delta" and rung == 2:
            raise SimulatedCrash("killed at ladder rung 2")

    orch._advance_rung = crashing_advance
    with pytest.raises(SimulatedCrash):
        orch.run_failback(plan, PLAN_VERSION)

    orch2 = Orchestrator(store, clock, target.url)
    recover(store, plan, PLAN_VERSION, orch2, clock_ms=CLOCK_BASE)
    orch2.run_failback(plan, PLAN_VERSION)
    actual = orch2.persisted_view(PLAN_VERSION)
    assert json.dumps(actual, sort_keys=True) == \
        json.dumps(expected, sort_keys=True)
    # committed ladder rungs were not double-counted
    delta_advances = [d for d in actual["decisions"]
                      if d["service"] == "svc-delta"
                      and d["decision"] == "ADVANCE"]
    assert len(delta_advances) == 4
