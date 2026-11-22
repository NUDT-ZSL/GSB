from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cdn_edge_workbench.engine import WorkbenchEngine
from cdn_edge_workbench.errors import BusinessError, ErrorCode, SimulatedCrash
from cdn_edge_workbench.persistence import StateStore


EXPECTED_CODES = {
    "E1201_ORIGIN_STALE_304",
    "E1202_ORIGIN_UNCHANGED_200",
    "E1204_ORIGIN_REQUEST_TIMEOUT",
    "E1205_ORIGIN_MALFORMED_HEADERS",
    "E1301_INJECTED_CONTENT_TAMPER",
    "E1302_INJECTED_ETAG_CONFLICT",
    "E1303_INJECTED_CLOCK_ROLLBACK",
}


def file_hashes(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def test_full_round_classifies_injections(
    tmp_path: Path,
    samples_dir: Path,
) -> None:
    before = file_hashes(samples_dir)
    state = tmp_path / "state"
    engine = WorkbenchEngine(samples_dir=samples_dir, state_dir=state)
    report = engine.run(run_id="round-1")
    assert report["status"] == "committed"
    assert report["index_version"] == 1
    assert set(report["error_code_counts"]) == EXPECTED_CODES
    assert report["summary"]["audits"] == 7
    assert report["summary"]["replaced"] == 2
    assert report["summary"]["refreshed_304"] == 1
    assert report["summary"]["evicted"] == 2
    assert report["summary"]["algorithm_sampled"] == 5
    assert report["summary"]["algorithm_exact"] == 1
    assert report["summary"]["algorithm_issues"] == 0
    assert report["summary"]["snapshot_candidates"] == 25
    assert report["summary"]["snapshot_arbitrations"] == 12
    assert file_hashes(samples_dir) == before

    audits = {(audit["stage"], audit["injection_seq"]): audit for audit in report["audits"]}
    for audit in audits.values():
        assert {"path", "node", "stage", "injection_seq", "cache_generation", "code"} <= set(audit)
        assert audit["cache_generation"] is not None
    assert audits[("parse", 1)]["code"] == "E1301_INJECTED_CONTENT_TAMPER"
    assert audits[("merge", 2)]["code"] == "E1302_INJECTED_ETAG_CONFLICT"
    assert audits[("freshness", 3)]["code"] == "E1303_INJECTED_CLOCK_ROLLBACK"
    assert audits[("origin", 4)]["code"] == "E1201_ORIGIN_STALE_304"
    assert audits[("origin", 5)]["details"]["retry_exhausted"] is True
    assert audits[("origin", 6)]["code"] == "E1205_ORIGIN_MALFORMED_HEADERS"
    assert audits[("origin", 7)]["code"] == "E1202_ORIGIN_UNCHANGED_200"

    revalidations = {
        (item["node"], item["path"]): item for item in report["revalidations"]
    }
    assert revalidations[("edge-a", "/feeds/weather.json")]["status"] == "replaced"
    assert revalidations[("edge-a", "/feeds/weather.json")]["new_cache_generation"] == 3
    assert revalidations[("edge-b", "/data/report.json")]["outcome"] == "valid_304"
    assert revalidations[("edge-b", "/data/report.json")]["cache_generation"] == 2
    assert revalidations[("edge-b", "/assets/app.js")]["retry_exhausted"] is True

    entries = {(entry["node"], entry["path"]): entry for entry in report["entries"]}
    assert entries[("edge-a", "/feeds/weather.json")]["cache_generation"] == 3
    assert entries[("edge-b", "/data/report.json")]["cache_generation"] == 2
    assert entries[("edge-b", "/feeds/news.json")]["cache_generation"] == 3
    assert "/images/logo.png" not in {entry["path"] for entry in report["entries"]}
    assert "/data/catalog.json" not in {entry["path"] for entry in report["entries"]}

    modes = [(item["scope"], item["target"], item["mode"]) for item in report["algorithm_selections"]]
    assert ("node", "edge-a", "sampled_digest_buckets") in modes
    assert ("node", "edge-b", "sampled_digest_buckets") in modes
    assert ("node", "edge-c", "exact_per_entry") in modes
    sampled = next(
        item
        for item in report["algorithm_selections"]
        if item["scope"] == "node" and item["target"] == "edge-a"
    )
    assert 0.0 < sampled["estimated_false_negative_upper_bound"] < 1.0
    assert sampled["observation_source"]

    store = StateStore(state)
    snapshot = store.read_committed_snapshot()
    assert snapshot["version"] == 1
    assert snapshot["entry_count"] == len(snapshot["entries"])
    assert len(snapshot["audits"]) == 7
    assert store.audit_records(stage="origin", path="/assets/app.js", node="edge-a")


def test_same_run_id_is_idempotent(
    tmp_path: Path,
    samples_dir: Path,
) -> None:
    state = tmp_path / "state"
    engine = WorkbenchEngine(samples_dir=samples_dir, state_dir=state)
    first = engine.run(run_id="idempotent")
    second = engine.run(run_id="idempotent")
    assert first["index_version"] == 1
    assert second["status"] == "idempotent_existing_commit"
    assert second["index_version"] == 1
    snapshot = StateStore(state).inspect()
    assert snapshot["version"] == 1
    assert snapshot["replacement_ledger_count"] == 2
    assert len(snapshot["audits"]) == 7
    assert len(snapshot["runs"]) == 1


def test_clock_rollback_keeps_index(
    tmp_path: Path,
    samples_dir: Path,
) -> None:
    state = tmp_path / "state"
    engine = WorkbenchEngine(samples_dir=samples_dir, state_dir=state)
    engine.run(logical_clock=1200, run_id="clock-base")
    store = StateStore(state)
    before = store.read_committed_snapshot()
    with pytest.raises(BusinessError) as error:
        engine.run(logical_clock=1190, run_id="clock-rollback")
    assert error.value.code is ErrorCode.CLOCK_ROLLBACK
    after = store.read_committed_snapshot()
    assert after["version"] == before["version"]
    assert after["entries"] == before["entries"]
    assert after["audits"] == before["audits"]


def test_index_version_rollback_and_run_id_conflict(
    tmp_path: Path,
    samples_dir: Path,
) -> None:
    state = tmp_path / "state"
    engine = WorkbenchEngine(samples_dir=samples_dir, state_dir=state)
    engine.run(run_id="base-run")
    store = StateStore(state)
    with pytest.raises(BusinessError) as rollback:
        store.record_plan(
            run_id="rollback-plan",
            base_version=1,
            planned_version=1,
            plan={"kind": "rollback"},
        )
    assert rollback.value.code is ErrorCode.INDEX_VERSION_ROLLBACK
    store.record_plan(
        run_id="conflict-run",
        base_version=1,
        planned_version=2,
        plan={"kind": "first"},
    )
    with pytest.raises(BusinessError) as conflict:
        store.record_plan(
            run_id="conflict-run",
            base_version=1,
            planned_version=2,
            plan={"kind": "different"},
        )
    assert conflict.value.code is ErrorCode.RUN_ID_CONFLICT


def test_prepared_crash_is_hidden_then_recovered(
    tmp_path: Path,
    samples_dir: Path,
) -> None:
    state = tmp_path / "state"
    engine = WorkbenchEngine(samples_dir=samples_dir, state_dir=state)
    with pytest.raises(SimulatedCrash):
        engine.run(run_id="crash", fault_point="after_prepare")
    store = StateStore(state)
    committed = store.read_committed_snapshot()
    assert committed["version"] == 0
    assert committed["entries"] == []
    assert committed["audits"] == []
    assert len(store.audit_records(committed=False)) == 7
    recovery = engine.recover()
    assert recovery["committed_version"] == 1
    assert recovery["entry_count"] == 8
    assert recovery["audit_count"] == 7
    assert any(
        action["action"] == "detected_uncommitted_audit"
        for action in recovery["actions"]
    )
    assert any(
        action["action"] == "replayed_prepared_run"
        for action in recovery["actions"]
    )
    final = store.inspect()
    assert final["uncommitted_audit_count"] == 0
    assert final["replacement_ledger_count"] == 2


def test_during_apply_rollback_then_recover(
    tmp_path: Path,
    samples_dir: Path,
) -> None:
    state = tmp_path / "state"
    engine = WorkbenchEngine(samples_dir=samples_dir, state_dir=state)
    with pytest.raises(SimulatedCrash):
        engine.run(run_id="during", fault_point="during_apply")
    store = StateStore(state)
    assert store.current_version() == 0
    assert len(store.audit_records(committed=False)) == 7
    recovery = engine.recover()
    assert recovery["committed_version"] == 1
    assert recovery["entry_count"] == 8


def test_two_processes_commit_once(
    tmp_path: Path,
    samples_dir: Path,
) -> None:
    state = tmp_path / "state"
    env = dict(os.environ)
    src = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = src + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    command = [
        sys.executable,
        "-m",
        "cdn_edge_workbench",
        "run",
        "--samples",
        str(samples_dir),
        "--state",
        str(state),
        "--run-id",
        "parallel",
        "--compact",
    ]
    first = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    second = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    first_out, first_err = first.communicate(timeout=30)
    second_out, second_err = second.communicate(timeout=30)
    assert first.returncode == 0, first_err
    assert second.returncode == 0, second_err
    statuses = {json.loads(first_out)["status"], json.loads(second_out)["status"]}
    assert statuses <= {"committed", "idempotent_existing_commit"}
    snapshot = StateStore(state).inspect()
    assert snapshot["version"] == 1
    assert snapshot["replacement_ledger_count"] == 2
    assert len(snapshot["audits"]) == 7
    assert len(snapshot["runs"]) == 1

