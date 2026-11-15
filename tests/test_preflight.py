from __future__ import annotations

import time

import pytest

from tactical_engine.errors import EngineError
from tactical_engine import preflight as preflight_module
from tactical_engine.preflight import preflight_unit


def test_parallel_and_serial_preflight_match(sample_scenario):
    unit = sample_scenario.units[0]
    parallel = preflight_unit(sample_scenario, unit, concurrency=4)
    serial = preflight_unit(sample_scenario, unit, concurrency=1)
    assert parallel.merge_order == serial.merge_order
    assert parallel.selected_candidate_id == serial.selected_candidate_id
    assert parallel.selected_path == serial.selected_path
    assert [result.core_dict() for result in parallel.results] == [
        result.core_dict() for result in serial.results
    ]
    assert parallel.concurrency == min(4, len(unit.candidates))
    assert serial.concurrency == 1


def test_memory_budget_is_enforced(sample_scenario):
    unit = sample_scenario.units[0]
    with pytest.raises(EngineError) as caught:
        preflight_unit(sample_scenario, unit, memory_budget_bytes=64)
    assert caught.value.code == "E_MEMORY_LIMIT"


def test_results_are_merged_in_candidate_order(sample_scenario):
    unit = sample_scenario.units[0]
    report = preflight_unit(sample_scenario, unit, concurrency=3)
    expected_order = [candidate.id for candidate in unit.candidates]
    assert report.merge_order == tuple(expected_order)
    assert [result.candidate_id for result in report.results] == expected_order


def test_preflight_failure_is_reported_with_candidate_context(
    sample_scenario,
    monkeypatch,
):
    unit = sample_scenario.units[0]
    failing_cell = unit.candidates[1].cell
    original_plan = preflight_module.Planner.plan

    def failing_plan(self, start, goal, budget_fixed, **kwargs):
        if goal == failing_cell:
            raise RuntimeError("injected candidate failure")
        return original_plan(self, start, goal, budget_fixed, **kwargs)

    monkeypatch.setattr(preflight_module.Planner, "plan", failing_plan)
    with pytest.raises(EngineError) as caught:
        preflight_unit(sample_scenario, unit, concurrency=3)
    assert caught.value.code == "E_PREFLIGHT_FAILED"
    assert caught.value.details["candidate_id"] == unit.candidates[1].id


def test_first_error_uses_candidate_order(sample_scenario, monkeypatch):
    unit = sample_scenario.units[0]
    first_cell = unit.candidates[0].cell
    last_cell = unit.candidates[2].cell

    def failing_plan(self, start, goal, budget_fixed, **kwargs):
        if goal == last_cell:
            raise RuntimeError("later candidate fails first")
        if goal == first_cell:
            time.sleep(0.02)
            raise RuntimeError("earlier candidate fails later")
        return None

    monkeypatch.setattr(preflight_module.Planner, "plan", failing_plan)
    with pytest.raises(EngineError) as caught:
        preflight_unit(sample_scenario, unit, concurrency=3)
    assert caught.value.code == "E_PREFLIGHT_FAILED"
    assert caught.value.details["candidate_id"] == unit.candidates[0].id
