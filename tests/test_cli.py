from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from supply_diag.generator import generate_samples


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cli(
    *args: str,
    cwd: Path = REPO_ROOT,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "supply_diag", *args],
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_generator_is_byte_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    assert run_cli("generate", "--output", str(first)).returncode == 0
    assert run_cli("generate", "--output", str(second)).returncode == 0
    files_first = sorted(path.relative_to(first) for path in first.rglob("*.json"))
    files_second = sorted(path.relative_to(second) for path in second.rglob("*.json"))
    assert files_first == files_second
    for relative in files_first:
        assert (first / relative).read_bytes() == (second / relative).read_bytes()


def test_cli_partial_exit_report_graph_and_incremental_reuse(tmp_path: Path) -> None:
    samples = tmp_path / "samples"
    generate_samples(samples)
    report = tmp_path / "report.json"
    graph = tmp_path / "graph.json"
    checkpoint = tmp_path / "checkpoint.json"
    command = (
        "diagnose",
        str(samples / "manifests"),
        "--report",
        str(report),
        "--graph",
        str(graph),
        "--checkpoint",
        str(checkpoint),
    )
    first = run_cli(*command)
    assert first.returncode == 1
    first_report = json.loads(report.read_text(encoding="utf-8"))
    assert first_report["conclusion"] == "partial_failure"
    assert first_report["summary"]["manifest_count"] == 4
    assert first_report["summary"]["successful_manifest_count"] == 1
    assert first_report["summary"]["truth_inconsistent_count"] == 0
    assert first_report["uncertain_items"]
    assert len(json.loads(graph.read_text(encoding="utf-8"))["graphs"]) == 4
    assert not list(report.parent.glob(".*.tmp"))

    second = run_cli(*command)
    assert second.returncode == 1
    second_report = json.loads(report.read_text(encoding="utf-8"))
    assert second_report["checkpoint"]["mode"] == "incremental"
    assert second_report["checkpoint"]["reused_manifest_count"] == 4
    assert second_report["manifests"] == first_report["manifests"]

    rebuilt = run_cli(*command, "--rebuild")
    assert rebuilt.returncode == 1
    rebuilt_report = json.loads(report.read_text(encoding="utf-8"))
    assert rebuilt_report["checkpoint"]["mode"] == "full_rebuild"
    assert rebuilt_report["manifests"] == first_report["manifests"]
    assert rebuilt_report["run_id"] == first_report["run_id"]


def test_cli_success_exit_code_on_fully_valid_manifest(tmp_path: Path) -> None:
    samples = tmp_path / "samples"
    generate_samples(samples)
    report = tmp_path / "report.json"
    graph = tmp_path / "graph.json"
    completed = run_cli(
        "diagnose",
        str(samples / "manifests/delta-secure.json"),
        "--report",
        str(report),
        "--graph",
        str(graph),
    )
    assert completed.returncode == 0
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["conclusion"] == "all_success"
    assert result["summary"]["successful_manifest_count"] == 1
    assert result["summary"]["complete_component_count"] == 3


def test_cli_parameter_error_exit_code(tmp_path: Path) -> None:
    completed = run_cli("diagnose", cwd=tmp_path)
    assert completed.returncode == 2
    assert "no inputs" in completed.stderr


def test_corrupt_checkpoint_causes_full_rebuild(tmp_path: Path) -> None:
    samples = tmp_path / "samples"
    generate_samples(samples)
    report = tmp_path / "report.json"
    graph = tmp_path / "graph.json"
    checkpoint = tmp_path / "checkpoint.json"
    command = (
        "diagnose",
        str(samples / "manifests/delta-secure.json"),
        "--report",
        str(report),
        "--graph",
        str(graph),
        "--checkpoint",
        str(checkpoint),
    )
    assert run_cli(*command).returncode == 0
    document = json.loads(checkpoint.read_text(encoding="utf-8"))
    document["payload"]["watermark"] = "999.0.0"
    checkpoint.write_text(json.dumps(document), encoding="utf-8")
    completed = run_cli(*command)
    assert completed.returncode == 0
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["checkpoint"]["mode"] == "full_rebuild"
    assert result["checkpoint"]["reason"] == "checkpoint_checksum_mismatch"


def test_duplicate_project_ids_fail_the_whole_batch_without_crash(
    tmp_path: Path,
) -> None:
    samples = tmp_path / "samples"
    generate_samples(samples)
    source = samples / "manifests/delta-secure.json"
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_bytes(source.read_bytes())
    second.write_bytes(source.read_bytes())
    report = tmp_path / "report.json"
    graph = tmp_path / "graph.json"
    completed = run_cli(
        "diagnose",
        str(first),
        str(second),
        "--report",
        str(report),
        "--graph",
        str(graph),
    )
    assert completed.returncode == 1
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["summary"]["failed_manifest_count"] == 0
    assert result["summary"]["partial_manifest_count"] == 2
    assert all(
        any(error["code"] == "E_DUPLICATE_PROJECT" for error in manifest["errors"])
        for manifest in result["manifests"]
    )


def test_malformed_manifest_does_not_abort_batch(tmp_path: Path) -> None:
    samples = tmp_path / "samples"
    generate_samples(samples)
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    (input_dir / "good.json").write_bytes(
        (samples / "manifests/delta-secure.json").read_bytes()
    )
    (input_dir / "broken.json").write_text("{broken", encoding="utf-8")
    report = tmp_path / "report.json"
    graph = tmp_path / "graph.json"
    completed = run_cli(
        "diagnose",
        str(input_dir),
        "--report",
        str(report),
        "--graph",
        str(graph),
    )
    assert completed.returncode == 1
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["summary"]["manifest_count"] == 2
    assert result["summary"]["successful_manifest_count"] == 1
    assert result["summary"]["failed_manifest_count"] == 1

