from __future__ import annotations

from pathlib import Path

from gbdt_workbench.cli import main

from .helpers import ROOT, write_config


def test_cli_validate_and_run_exit_codes(tmp_path: Path) -> None:
    assert main(["validate", "--config", str(ROOT / "configs/example.yaml")]) == 0
    assert (
        main(
            [
                "validate",
                "--config",
                str(ROOT / "configs/anomalies/bad_learning_rate.yaml"),
            ]
        )
        == 2
    )

    config_path = write_config(tmp_path)
    assert (
        main(
            [
                "run",
                "--config",
                str(config_path),
                "--output",
                str(tmp_path / "out"),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "rebuild",
                "--config",
                str(config_path),
                "--output",
                str(tmp_path / "rebuild"),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "compare",
                "--left",
                str(tmp_path / "out" / "diagnostic_report.json"),
                "--right",
                str(tmp_path / "rebuild" / "diagnostic_report.json"),
            ]
        )
        == 0
    )
