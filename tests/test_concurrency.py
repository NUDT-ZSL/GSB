"""Requirement 8: multi-instance concurrency, idempotency, isolation."""

from __future__ import annotations

import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from conftest import build_plan, make_clock
from recovery_pipeline.executor import Orchestrator

PLAN_VERSION = 2


def test_concurrent_commit_same_action_once(store, samples, target):
    plan, _, _ = build_plan(samples)
    store.install_plan(PLAN_VERSION, plan.view(), plan.migration_map)
    action = {"action_id": "alpha-stop", "op": "stop", "generation": 1}

    def worker(_):
        orch = Orchestrator(store, make_clock(), target.url)
        return orch._commit_action(PLAN_VERSION, "svc-alpha", action, 1000)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(worker, range(6)))
    assert results.count("committed") == 1
    assert results.count("already_committed") == 5

    view = Orchestrator(store, make_clock(), target.url) \
        .persisted_view(PLAN_VERSION)
    assert len(view["watermarks"]) == 1
    with urllib.request.urlopen(target.url + "/stats", timeout=5) as resp:
        stats = json.loads(resp.read().decode())
    assert stats["apply_count"] == 1  # applied remotely exactly once


def test_concurrent_orchestrators_disjoint_services(store, samples, target):
    plan, _, _ = build_plan(samples)
    store.install_plan(PLAN_VERSION, plan.view(), plan.migration_map)

    def worker(action_id):
        orch = Orchestrator(store, make_clock(), target.url)
        action = {"action_id": action_id, "op": "stop", "generation": 1}
        return orch._commit_action(PLAN_VERSION, "svc-alpha", action, 1000)

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(worker,
                                ["alpha-stop", "alpha-switch", "alpha-start"]))
    assert results == ["committed"] * 3
    orch = Orchestrator(store, make_clock(), target.url)
    view = orch.persisted_view(PLAN_VERSION)
    assert len(view["watermarks"]) == 3
