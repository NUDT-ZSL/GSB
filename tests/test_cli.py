from __future__ import annotations

import json
from pathlib import Path

from summarize_pipeline.cli import main


def test_cli_all_command_end_to_end(tmp_path: Path) -> None:
    data = tmp_path / "samples.jsonl"
    config = tmp_path / "config.json"
    run_out = tmp_path / "run.json"
    validation_out = tmp_path / "validation.json"
    snapshots = tmp_path / "snapshots"
    code = main(
        [
            "all",
            "--data",
            str(data),
            "--config",
            str(config),
            "--run-out",
            str(run_out),
            "--validation-out",
            str(validation_out),
            "--snapshot-dir",
            str(snapshots),
            "--snapshot-version",
            "1",
            "--seed",
            "2026",
            "--docs",
            "3",
        ]
    )
    assert code == 0
    report = json.loads(run_out.read_text(encoding="utf-8"))
    validation = json.loads(validation_out.read_text(encoding="utf-8"))
    assert report["run_hash"]
    assert validation["ok"] is True
    assert validation["reproducibility"]["hash_match"] is True
    assert (snapshots / "current.json").exists()


def test_cli_snapshot_rollback_is_exit_two(tmp_path: Path) -> None:
    data = tmp_path / "samples.jsonl"
    config = tmp_path / "config.json"
    snapshots = tmp_path / "snapshots"
    common = [
        "--data",
        str(data),
        "--config",
        str(config),
        "--snapshot-dir",
        str(snapshots),
        "--seed",
        "2026",
        "--docs",
        "2",
    ]
    assert main(["all", *common, "--snapshot-version", "3", "--run-out", str(tmp_path / "r.json"), "--validation-out", str(tmp_path / "v.json")]) == 0
    assert main(["run", "--input", str(data), "--config", str(config), "--out", str(tmp_path / "r2.json"), "--snapshot-dir", str(snapshots), "--snapshot-version", "2"]) == 2


def test_cli_default_snapshot_version_advances(tmp_path: Path) -> None:
    data = tmp_path / "samples.jsonl"
    config = tmp_path / "config.json"
    snapshots = tmp_path / "snapshots"
    common = [
        "--data",
        str(data),
        "--config",
        str(config),
        "--snapshot-dir",
        str(snapshots),
        "--seed",
        "2026",
        "--docs",
        "2",
        "--run-out",
        str(tmp_path / "run.json"),
        "--validation-out",
        str(tmp_path / "validation.json"),
    ]
    assert main(["all", *common]) == 0
    assert json.loads((snapshots / "current.json").read_text())["snapshot_version"] == 1
    assert main(["all", *common]) == 0
    assert json.loads((snapshots / "current.json").read_text())["snapshot_version"] == 2

