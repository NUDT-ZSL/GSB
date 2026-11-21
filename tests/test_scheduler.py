from __future__ import annotations

import copy
from pathlib import Path

from capbench.evaluator import run_evaluation, select_candidates
from capbench.probe import SimulatedProbe
from capbench.report import build_report
from capbench.scenario import parse_scenario
from capbench.storage import Store


def _execute(raw: dict, tmp_path: Path, name: str) -> tuple[Store, str, dict]:
    scenario = parse_scenario(copy.deepcopy(raw))
    store = Store(tmp_path / f"{name}.sqlite")
    run_id = scenario.default_run_id()
    store.initialize(scenario, run_id=run_id, seed=scenario.seed)
    result = run_evaluation(
        store=store,
        scenario=scenario,
        run_id=run_id,
        seed=scenario.seed,
        probe=SimulatedProbe(),
        workers=4,
    )
    return store, run_id, {"result": result, "report": build_report(store, run_id)}


def test_capacity_knees_and_early_termination(
    demo_raw: dict,
    tmp_path: Path,
) -> None:
    _, _, payload = _execute(demo_raw, tmp_path, "normal")
    report = payload["report"]
    assert payload["result"]["status"] == "completed"
    tenants = {item["tenant"]: item for item in report["tenants"]}
    alpha = tenants["alpha"]["capacity_conclusion"]
    beta = tenants["beta"]["capacity_conclusion"]
    gamma = tenants["gamma"]["capacity_conclusion"]
    assert (alpha["status"], alpha["capacity_concurrency"]) == ("capacity_found", 15)
    assert alpha["aborted_at"]["gradient_index"] == 4
    assert alpha["aborted_at"]["window_index"] == 0
    assert alpha["aborted_at"]["early_terminated"] is True
    assert (beta["status"], beta["capacity_concurrency"]) == ("capacity_found", 30)
    assert beta["degraded_capacity"] is True
    assert (gamma["status"], gamma["capacity_concurrency"]) == ("capacity_found", 8)
    assert gamma["aborted_at"]["gradient_index"] == 3
    assert gamma["aborted_at"]["window_index"] == 1
    assert report["diagnostics"]["error_counts"]["target_service_interrupted"] > 0
    alpha_g4 = next(
        item for item in tenants["alpha"]["gradients"] if item["gradient_index"] == 4
    )
    assert alpha_g4["state"] == "terminated"
    assert [window["state"] for window in alpha_g4["windows"]] == [
        "committed",
        "skipped_early_termination",
        "skipped_early_termination",
    ]


def test_budget_exhaustion_is_not_a_timeout(
    demo_raw: dict,
    tmp_path: Path,
) -> None:
    raw = copy.deepcopy(demo_raw)
    raw["global_time_budget_ms"] = 4000
    store, run_id, payload = _execute(raw, tmp_path, "budget")
    report = payload["report"]
    assert report["run"]["budget_used_ms"] == 4000
    assert report["run"]["budget_used_ms"] <= report["run"]["global_time_budget_ms"]
    assert report["diagnostics"]["unit_outcome_counts"]["budget_unstarted"] > 0
    assert payload["result"]["status"] == "completed_budget_exhausted"
    assert store.meta(run_id)["budget_exhausted"] == 1


def test_disabled_early_termination_runs_full_abort_gradient(
    demo_raw: dict,
    tmp_path: Path,
) -> None:
    raw = copy.deepcopy(demo_raw)
    raw["plans"][0]["allow_early_termination"] = False
    raw["global_time_budget_ms"] = 30000
    _, _, payload = _execute(raw, tmp_path, "no-early")
    tenant = next(
        item for item in payload["report"]["tenants"] if item["tenant"] == "alpha"
    )
    assert tenant["capacity_conclusion"]["capacity_concurrency"] == 15
    g4 = next(item for item in tenant["gradients"] if item["gradient_index"] == 4)
    assert g4["state"] == "completed"
    assert g4["verdict"] == "abort"
    assert [window["state"] for window in g4["windows"]] == [
        "committed",
        "committed",
        "committed",
    ]


def test_later_gradient_does_not_rewrite_earlier_abort(
    demo_raw: dict,
    tmp_path: Path,
) -> None:
    raw = copy.deepcopy(demo_raw)
    alpha = raw["services"][0]["tenants"][0]
    alpha["profile"]["fault_windows"] = [{"gradient_index": 1, "window_index": 0}]
    raw["plans"][0]["allow_early_termination"] = False
    raw["global_time_budget_ms"] = 30000
    _, _, payload = _execute(raw, tmp_path, "decoupled")
    tenant = next(
        item for item in payload["report"]["tenants"] if item["tenant"] == "alpha"
    )
    conclusion = tenant["capacity_conclusion"]
    assert conclusion["status"] == "no_capacity"
    assert conclusion["capacity_concurrency"] is None
    later_started = [
        item
        for item in tenant["gradients"]
        if item["gradient_index"] > 1 and item["committed_windows"] > 0
    ]
    assert later_started
    assert conclusion["decoupled_from_later_gradients"] is True


def test_per_tenant_parallel_gradient_limit_drives_multiple_candidates(
    demo_raw: dict,
    tmp_path: Path,
) -> None:
    raw = copy.deepcopy(demo_raw)
    raw["services"][0]["tenants"][0]["max_parallel_gradients"] = 2
    scenario = parse_scenario(raw)
    store = Store(tmp_path / "parallel.sqlite")
    run_id = scenario.default_run_id()
    store.initialize(scenario, run_id=run_id, seed=scenario.seed)
    candidates = select_candidates(store, scenario, run_id)
    alpha_candidates = [
        item for item in candidates if item.tenant == "alpha"
    ]
    assert {item.gradient.index for item in alpha_candidates} == {1, 2}

