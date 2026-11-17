from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _cli(*args: str):
    return subprocess.run(
        [sys.executable, "-m", "authdiag", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )


def _summary(process):
    return json.loads(process.stdout.strip().splitlines()[-1])


def test_all_success_exit_zero_and_outputs_are_parseable(
    sample_dir: Path,
    tmp_path: Path,
):
    out = tmp_path / "out"
    process = _cli(
        str(sample_dir / "policy.json"),
        str(sample_dir / "events_a.json"),
        str(sample_dir / "events_b.json"),
        "--out",
        str(out),
        "--workdir",
        str(out),
    )
    assert process.returncode == 0, process.stderr
    summary = _summary(process)
    assert summary["exit_code"] == 0
    assert summary["status"] == "success"
    report = json.loads((out / "diagnostics.json").read_text(encoding="utf-8"))
    graph = json.loads((out / "dependency_graph.json").read_text(encoding="utf-8"))
    assert report["summary"]["truth_consistent"] == 3
    assert graph["nodes"] and graph["edges"]
    assert (out / "dependency_graph.dot").is_file()
    assert (out / "dependency_graph.mmd").is_file()
    assert not list(out.glob(".*.tmp"))


def test_partial_failure_exit_one_and_other_files_continue(
    sample_dir: Path,
    tmp_path: Path,
):
    out = tmp_path / "partial"
    process = _cli(
        str(sample_dir / "invalid_policy.json"),
        str(sample_dir / "events_malformed.json"),
        str(sample_dir / "invalid_events.json"),
        "--out",
        str(out),
        "--workdir",
        str(out),
    )
    assert process.returncode == 1
    summary = _summary(process)
    assert summary["exit_code"] == 1
    assert summary["status"] == "partial"
    report = json.loads((out / "diagnostics.json").read_text(encoding="utf-8"))
    assert any(item["code"] == "E_JSON" for item in report["errors"])
    assert report["active_sessions"]
    files = {item["source"]: item for item in report["files"]}
    bad = files[str(sample_dir / "events_malformed.json")]
    good = files[str(sample_dir / "invalid_events.json")]
    assert bad["status"] == "failed"
    assert good["accepted"] == 1


def test_argument_error_exit_two():
    process = _cli()
    assert process.returncode == 2
    assert "usage:" in process.stderr


def test_cycle_exit_one(sample_dir: Path, tmp_path: Path):
    out = tmp_path / "cycle"
    process = _cli(
        str(sample_dir / "cycle_policy.json"),
        str(sample_dir / "cycle_events.json"),
        "--out",
        str(out),
        "--workdir",
        str(out),
    )
    assert process.returncode == 1
    report = json.loads((out / "diagnostics.json").read_text(encoding="utf-8"))
    cycle = next(item for item in report["errors"] if item["code"] == "E_CYCLE")
    assert cycle["details"]["entry_indices"] == [0, 1]
