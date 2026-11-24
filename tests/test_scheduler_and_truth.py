from __future__ import annotations

import json
from pathlib import Path

from memguard.codes import E_BUDGET, E_QUEUE_FULL, E_TIMEOUT, E_TRUTH_MISMATCH
from memguard.engine import RunOptions, run

from conftest import decision_snapshot, write_jsonl


def _codes(report: dict) -> set[str]:
    return {
        code
        for arena in report["scheduler"]["arenas"]
        for code in arena["degradation_codes"]
    }


def test_parallel_and_serial_decisions_are_identical(sample: Path, run_sample) -> None:
    serial, _ = run_sample(workers=1, max_inflight_bytes=10_000_000)
    parallel, _ = run_sample(workers=4, max_inflight_bytes=10_000_000)
    tiny_budget, _ = run_sample(workers=4, max_inflight_bytes=1)
    tiny_budget_serial, _ = run_sample(workers=1, max_inflight_bytes=1)

    assert decision_snapshot(serial.report) == decision_snapshot(parallel.report)
    assert decision_snapshot(serial.report) == decision_snapshot(tiny_budget.report)
    assert decision_snapshot(tiny_budget.report) == decision_snapshot(
        tiny_budget_serial.report
    )
    assert (
        serial.report["batch"]["decision_digest"]
        == parallel.report["batch"]["decision_digest"]
        == tiny_budget.report["batch"]["decision_digest"]
    )
    assert E_BUDGET in _codes(tiny_budget.report)
    assert serial.report["truth"]["ok"] is True
    assert tiny_budget.report["truth"]["ok"] is True
    assert len(serial.report["accesses"]) == 15


def test_budget_queue_and_timeout_are_explicit_degradations(sample: Path, run_sample) -> None:
    budget, _ = run_sample(workers=2, max_inflight_bytes=1)
    queue, _ = run_sample(workers=1, max_inflight_bytes=10_000_000, max_pending=1)
    timeout, _ = run_sample(
        workers=2,
        max_inflight_bytes=10_000_000,
        arena_timeout_ms=1e-12,
    )
    assert E_BUDGET in _codes(budget.report)
    assert E_QUEUE_FULL in _codes(queue.report)
    assert E_TIMEOUT in _codes(timeout.report)
    assert len(timeout.report["accesses"]) == 15
    assert timeout.report["batch"]["status"] == "partial"
    assert timeout.exit_code == 1


def test_truth_marker_mismatch_is_reported(sample: Path, tmp_path: Path, run_sample) -> None:
    baseline, _ = run_sample(name="truth-base")
    document = json.loads((sample / "sync_markers.json").read_text(encoding="utf-8"))
    marker = next(
        item for item in document["markers"] if item["access_seq"] == 3
    )
    marker["expected_business_code"] = "OOB_OUTSIDE_ANCESTORS"
    bad_markers = tmp_path / "bad_markers.json"
    bad_markers.write_text(json.dumps(document), encoding="utf-8")

    from memguard.engine import RunOptions, run

    result = run(
        RunOptions(
            region_paths=[sample / "regions.json"],
            trace_paths=[sample / "traces.jsonl"],
            database_path=tmp_path / "bad-truth.sqlite",
            report_path=tmp_path / "bad-truth.json",
            truth_marker_paths=[bad_markers],
            max_workers=2,
        )
    )
    assert result.report["truth"]["ok"] is False
    assert result.report["truth"]["business_code"] == E_TRUTH_MISMATCH
    assert result.report["truth"]["mismatched"] >= 1
    assert result.exit_code == 1
    assert baseline.report["accesses"] == result.report["accesses"]


def test_report_is_atomic_json(tmp_path: Path, run_sample) -> None:
    _, report_path = run_sample()
    parsed = json.loads(report_path.read_text(encoding="utf-8"))
    assert parsed["schema_version"] == 1
    assert not list(report_path.parent.glob(".%s.*.tmp" % report_path.name))


def test_oob_findings_alone_exit_zero(tmp_path: Path) -> None:
    regions = tmp_path / "regions.json"
    regions.write_text(
        json.dumps(
            {
                "regions": [
                    {
                        "region_id": "r",
                        "arena_id": "a",
                        "arena_size": 1000,
                        "start_offset": 0,
                        "end_offset": 100,
                        "lifetime_start_ms": 0,
                        "lifetime_end_ms": 1000,
                        "generation": 1,
                        "parent_region_id": None,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    traces = tmp_path / "traces.jsonl"
    common = {
        "region_id": "r",
        "access_kind": "read",
        "arena_size": 1000,
        "timestamp_ms": 10,
    }
    write_jsonl(
        traces,
        [
            {
                "trace_id": "inside",
                "access_seq": 1,
                "base_offset": 0,
                "access_size": 10,
                **common,
            },
            {
                "trace_id": "outside",
                "access_seq": 2,
                "base_offset": 90,
                "access_size": 30,
                **common,
            },
        ],
    )
    result = run(
        RunOptions(
            region_paths=[regions],
            trace_paths=[traces],
            database_path=tmp_path / "ledger.sqlite",
            report_path=tmp_path / "report.json",
            max_workers=2,
        )
    )
    assert result.exit_code == 0
    assert result.report["batch"]["status"] == "violations"
    assert result.report["batch"]["access_counts"]["out_of_bounds"] == 1
