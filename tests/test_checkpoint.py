from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from config_forensics.checkpoint import CheckpointError, CheckpointManager
from config_forensics.codes import Code
from config_forensics.engine import EngineConfig, ForensicEngine, canonical_verdict
from config_forensics.storage import FileLock, atomic_write_json


class SimulatedHardStop(RuntimeError):
    pass


def _engine(workers: int = 3, algorithms=None) -> ForensicEngine:
    kwargs = {}
    if algorithms is not None:
        kwargs["allowed_algorithms"] = algorithms
    return ForensicEngine(
        EngineConfig(
            workers=workers,
            queue_limit=64,
            max_file_seconds=5.0,
            **kwargs,
        )
    )


def _paths(root: Path, prefix: str):
    return (
        root / prefix / "report.json",
        root / prefix / "graph.json",
    )


def test_checkpoint_resume_after_simulated_force_stop(sample_root: Path) -> None:
    checkpoint = sample_root / "state" / "checkpoint.json"
    crash_report, crash_graph = _paths(sample_root, "crash")
    crashed = False

    def crash_after_first(source_id: str) -> None:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise SimulatedHardStop(source_id)

    with pytest.raises(SimulatedHardStop):
        _engine(workers=1).run(
            sample_root / "evidence",
            sample_root / "baseline" / "baseline.json",
            crash_report,
            crash_graph,
            checkpoint_path=checkpoint,
            crash_hook=crash_after_first,
        )
    assert not crash_report.exists()
    checkpoint_payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert len(checkpoint_payload["completed_source_ids"]) == 1
    assert checkpoint_payload["accumulated_chain_state_sha256"]
    assert not list(checkpoint.parent.glob(".*.tmp.*"))

    resumed_report, resumed_graph = _paths(sample_root, "resumed")
    resumed = _engine(workers=4).run(
        sample_root / "evidence",
        sample_root / "baseline" / "baseline.json",
        resumed_report,
        resumed_graph,
        checkpoint_path=checkpoint,
    )
    assert resumed.report["checkpoint"]["action"] == "resume"
    assert resumed.report["checkpoint"]["resumed_file_count"] == 1

    fresh_report, fresh_graph = _paths(sample_root, "fresh")
    fresh = _engine(workers=1).run(
        sample_root / "evidence",
        sample_root / "baseline" / "baseline.json",
        fresh_report,
        fresh_graph,
    )
    assert canonical_verdict(resumed.report) == canonical_verdict(fresh.report)
    assert resumed.graph == fresh.graph


def test_checkpoint_resume_after_real_process_exit(sample_root: Path) -> None:
    checkpoint = sample_root / "state" / "process-checkpoint.json"
    report_path, graph_path = _paths(sample_root, "process-crash")
    repo_root = Path(__file__).resolve().parents[1]
    code = """
import os
import sys
from pathlib import Path
from config_forensics.engine import EngineConfig, ForensicEngine

root = Path(sys.argv[1])
checkpoint = Path(sys.argv[2])
report = Path(sys.argv[3])
graph = Path(sys.argv[4])

def stop_after_checkpoint(source_id):
    os._exit(77)

ForensicEngine(
    EngineConfig(workers=1, queue_limit=64, max_file_seconds=5.0)
).run(
    root / "evidence",
    root / "baseline" / "baseline.json",
    report,
    graph,
    checkpoint_path=checkpoint,
    crash_hook=stop_after_checkpoint,
)
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(repo_root / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(sample_root),
            str(checkpoint),
            str(report_path),
            str(graph_path),
        ],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 77, completed.stderr
    assert not report_path.exists()
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert len(payload["completed_source_ids"]) == 1

    resumed_report, resumed_graph = _paths(sample_root, "process-resumed")
    resumed = _engine(workers=4).run(
        sample_root / "evidence",
        sample_root / "baseline" / "baseline.json",
        resumed_report,
        resumed_graph,
        checkpoint_path=checkpoint,
    )
    assert resumed.report["checkpoint"]["action"] == "resume"
    assert resumed.report["summary"]["drift_point_count"] == 5
    assert resumed.report["summary"]["tampered_count"] == 1


def test_checkpoint_schema_version_change_rebuilds(sample_root: Path) -> None:
    checkpoint = sample_root / "state" / "checkpoint.json"
    report_path, graph_path = _paths(sample_root, "first")
    _engine().run(
        sample_root / "evidence",
        sample_root / "baseline" / "baseline.json",
        report_path,
        graph_path,
        checkpoint_path=checkpoint,
    )
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload["checkpoint_schema_version"] = 999
    atomic_write_json(checkpoint, payload)

    rebuilt_report, rebuilt_graph = _paths(sample_root, "rebuilt")
    result = _engine().run(
        sample_root / "evidence",
        sample_root / "baseline" / "baseline.json",
        rebuilt_report,
        rebuilt_graph,
        checkpoint_path=checkpoint,
    )
    assert result.report["checkpoint"]["action"] == "rebuild"
    assert any(
        issue["code"] == Code.CHECKPOINT_VERSION for issue in result.report["errors"]
    )


def test_algorithm_whitelist_change_rebuilds(sample_root: Path) -> None:
    checkpoint = sample_root / "state" / "checkpoint.json"
    report_path, graph_path = _paths(sample_root, "first")
    _engine().run(
        sample_root / "evidence",
        sample_root / "baseline" / "baseline.json",
        report_path,
        graph_path,
        checkpoint_path=checkpoint,
    )
    changed_report, changed_graph = _paths(sample_root, "changed")
    result = _engine(algorithms=("sha256",)).run(
        sample_root / "evidence",
        sample_root / "baseline" / "baseline.json",
        changed_report,
        changed_graph,
        checkpoint_path=checkpoint,
    )
    assert result.report["checkpoint"]["action"] == "rebuild"
    assert any(
        issue["code"] == Code.CHECKPOINT_CONFIG_CHANGED
        for issue in result.report["errors"]
    )
    assert any(
        issue["code"] == Code.ALGORITHM_UNSUPPORTED and issue["host_id"] == "host-hmac"
        for issue in result.report["errors"]
    )


def test_checkpoint_lock_conflict_is_reported(sample_root: Path) -> None:
    checkpoint = sample_root / "state" / "checkpoint.json"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    lock_path = checkpoint.with_name(checkpoint.name + ".lock")
    report_path, graph_path = _paths(sample_root, "locked")
    with FileLock(lock_path, conflict_code=Code.CHECKPOINT_LOCK):
        result = _engine().run(
            sample_root / "evidence",
            sample_root / "baseline" / "baseline.json",
            report_path,
            graph_path,
            checkpoint_path=checkpoint,
        )
    assert result.report["checkpoint"]["action"] == "unavailable"
    assert result.report["checkpoint"]["disabled_due_to_lock"] is True
    assert any(
        issue["code"] == Code.CHECKPOINT_LOCK for issue in result.report["errors"]
    )


def test_checkpoint_save_lock_conflict_has_business_code(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text("{}", encoding="utf-8")
    lock_path = checkpoint.with_name(checkpoint.name + ".lock")
    manager = CheckpointManager(checkpoint)
    with FileLock(lock_path, conflict_code=Code.CHECKPOINT_LOCK):
        with pytest.raises(CheckpointError) as caught:
            manager.save(
                completed={},
                expected_generation=0,
                algorithm_whitelist=("sha256", "hmac-sha256"),
                baseline_sha256="baseline",
                hmac_fingerprint="keys",
                run_complete=False,
            )
    assert caught.value.code == Code.CHECKPOINT_LOCK


def test_output_lock_is_returned_without_partial_json(sample_root: Path) -> None:
    report_path, graph_path = _paths(sample_root, "output")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = report_path.with_name(report_path.name + ".lock")
    with FileLock(lock_path, conflict_code=Code.OUTPUT_LOCK):
        result = _engine().run(
            sample_root / "evidence",
            sample_root / "baseline" / "baseline.json",
            report_path,
            graph_path,
        )
    assert any(issue.code == Code.OUTPUT_LOCK for issue in result.output_errors)
    assert not report_path.exists()
    assert json.loads(graph_path.read_text(encoding="utf-8"))["nodes"]
