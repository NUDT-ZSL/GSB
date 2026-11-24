from __future__ import annotations

import time

from supply_chain_audit.codes import E_PARSE_JSON, E_QUEUE_FULL, E_TIMEOUT
from supply_chain_audit.engine import AuditEngine
from supply_chain_audit.importer import (
    BatchImporter,
    ImportBudget,
    process_snapshot_batch,
    run_wallclock_step_probe,
)


def test_corrupt_snapshot_does_not_stop_other_batches(sample_paths, clock):
    engine = AuditEngine(clock=clock)
    importer = BatchImporter(clock=clock, budget=ImportBudget(concurrency=3), engine=engine)
    report = importer.import_batches(
        [sample_paths["v1"], sample_paths["v2_manifest"], sample_paths["corrupt_batch"]]
    )
    assert report["accepted_count"] == 3
    assert report["failed_count"] == 1
    failed = next(outcome for outcome in report["outcomes"] if outcome["status"] == "failed")
    assert failed["code"] == E_PARSE_JSON
    assert engine.view["stats"]["artifact_count"] >= 14


def test_queue_limit_rejects_without_breaking_accepted_batch(sample_paths, clock):
    importer = BatchImporter(clock=clock, budget=ImportBudget(concurrency=1, queue_limit=1))
    report = importer.import_batches(
        [sample_paths["v1"], sample_paths["v2_manifest"], sample_paths["corrupt_batch"]]
    )
    assert report["accepted_count"] == 1
    assert report["rejected_count"] == 2
    assert report["outcomes"][0]["status"] == "parsed"
    assert all(outcome["code"] == E_QUEUE_FULL for outcome in report["outcomes"][1:])


def test_zero_timeout_uses_wall_clock_boundary(sample_paths, clock):
    outcome = process_snapshot_batch(
        sample_paths["v1"], clock, ImportBudget(timeout_seconds=0.0)
    )
    assert outcome["code"] == E_TIMEOUT
    assert outcome["terminated_early"] is True
    assert outcome["hash_operations"] == 0


def test_real_sleep_probe_measures_monotonic_wall_time():
    def slow_step(_index: int) -> None:
        time.sleep(0.02)

    result = run_wallclock_step_probe(
        slow_step, steps=3, budget=ImportBudget(timeout_seconds=0.005)
    )
    assert result["code"] == E_TIMEOUT
    assert result["completed"] == 1
    assert result["elapsed_seconds"] >= 0.015


def test_hash_operation_budget_is_enforced(sample_paths, clock):
    outcome = process_snapshot_batch(
        {"path": sample_paths["v1"], "base_dir": sample_paths["root"]},
        clock,
        ImportBudget(max_hash_operations=0),
    )
    assert outcome["code"] == "E_HASH_BUDGET"
    assert outcome["terminated_early"] is True


def test_successful_content_hash_counts(sample_paths, clock):
    outcome = process_snapshot_batch(
        {"path": sample_paths["v1"], "base_dir": sample_paths["root"]},
        clock,
        ImportBudget(max_hash_operations=100),
    )
    assert outcome["status"] == "parsed"
    assert outcome["hash_operations"] >= 9
    assert outcome["parse_result"] is not None
