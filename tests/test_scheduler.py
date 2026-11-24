"""Business rule 4: deterministic dual-budget scheduling."""

from __future__ import annotations

import threading

import pytest

from terrain_engine import errors
from terrain_engine.model import TileSpec
from terrain_engine.render import sample_buffer_bytes
from terrain_engine.scheduler import (LogicalClock, execute_plans,
                                      plan_rounds)


def replace_task(task, **changes):
    from dataclasses import replace
    return replace(task, **changes)


def test_rounds_split_when_time_budget_exceeded(task):
    tight = replace_task(task, time_budget_ms=40.0, max_concurrency=8)
    clock = LogicalClock()
    plans = plan_rounds(tight, list(tight.tiles), clock)
    assert len(plans) >= 2
    assert all(plan.logical_elapsed_ms <= 40.0 + 1e-9 for plan in plans)
    # Deterministic original tile order is preserved across rounds.
    flat = [tile for plan in plans for tile in plan.admitted]
    assert flat == [tile.tile_id for tile in task.tiles]


def test_memory_budget_caps_inflight_bytes(task):
    one_tile = sample_buffer_bytes(task.tiles[0], task.samples)
    tight = replace_task(
        task,
        memory_budget_bytes=one_tile * 2 + 10,
        max_concurrency=8,
        time_budget_ms=10_000.0)
    plans = plan_rounds(tight, list(tight.tiles), LogicalClock())
    assert all(plan.peak_inflight_bytes <= tight.memory_budget_bytes
               for plan in plans)
    assert plans[0].admitted  # progress is always made


def test_admission_order_reproducible_under_any_concurrency(task):
    sequences = []
    reference = None
    for concurrency in (1, 2, 4, 9):
        configured = replace_task(
            task, max_concurrency=concurrency, time_budget_ms=70.0)
        plans = plan_rounds(configured, list(configured.tiles),
                            LogicalClock())
        sequence = [
            tile for plan in plans for tile in plan.admitted]
        shapes = [len(plan.admitted) for plan in plans]
        sequences.append(sequence)
        if reference is None:
            reference = (sequence, shapes)
        else:
            assert sequence == reference[0]
            assert shapes == reference[1]
    assert all(sequence == sequences[0] for sequence in sequences)


def test_injected_clock_costs_drive_rounds(task):
    overrides = {task.tiles[0].tile_id: 90.0}
    tight = replace_task(task, time_budget_ms=100.0, max_concurrency=8)
    plans = plan_rounds(tight, list(tight.tiles),
                        LogicalClock(overrides))
    assert plans[0].admitted == [task.tiles[0].tile_id]
    assert plans[0].logical_elapsed_ms == pytest.approx(90.0)


def test_single_tile_exceeding_memory_budget_fails(task):
    oversized = replace_task(
        task, memory_budget_bytes=1024, max_concurrency=4)
    with pytest.raises(errors.EngineError) as info:
        plan_rounds(oversized, list(oversized.tiles), LogicalClock())
    assert info.value.code == errors.E_BUDGET


def test_execute_plans_runs_every_tile_once(task):
    seen = []
    lock = threading.Lock()

    def worker(tile: TileSpec) -> None:
        with lock:
            seen.append(tile.tile_id)

    plans = plan_rounds(task, list(task.tiles), LogicalClock())
    report = execute_plans(
        task, plans, {tile.tile_id: tile for tile in task.tiles}, worker)
    assert sorted(seen) == sorted(tile.tile_id for tile in task.tiles)
    assert all(result.ok for result in report.results)


def test_worker_error_is_contained_per_tile(task):
    def worker(tile: TileSpec) -> None:
        if tile.tile_id == task.tiles[2].tile_id:
            raise errors.fail(errors.E_HEIGHT_COUNT, "boom",
                              tile=tile.tile_id)

    plans = plan_rounds(task, list(task.tiles), LogicalClock())
    report = execute_plans(
        task, plans, {tile.tile_id: tile for tile in task.tiles}, worker)
    failed = [result for result in report.results if not result.ok]
    assert len(failed) == 1
    assert failed[0].error_code == errors.E_HEIGHT_COUNT
    assert sum(result.ok for result in report.results) == len(task.tiles) - 1

