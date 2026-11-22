from __future__ import annotations

import json

from panoweave.cli import main
from panoweave.diagnostics import run_diagnostics


def test_diagnostic_report_is_consistent(sample, tmp_path):
    report = run_diagnostics(
        sample["root"],
        tmp_path / "diagnostics",
        sample["config"],
        clean_cache=True,
    )
    assert report["consistent"] is True
    assert all(report["checks"].values())
    assert report["cross_version"]["v1"] == report["cross_version"]["v2"]
    assert report["cross_version"]["v2"] == report["cross_version"]["v3"]
    assert report["latest_business_error_code"] == "E_FEATHER_INVALID"
    html_path = tmp_path / "diagnostics" / "diagnostic.html"
    json_path = tmp_path / "diagnostics" / "diagnostic_report.json"
    assert html_path.exists()
    assert "PanoWeave" in html_path.read_text(encoding="utf-8")
    parsed = json.loads(json_path.read_text(encoding="utf-8"))
    assert parsed["schema"] == 1
    assert parsed["consistent"] is True


def test_cli_sample_generation(tmp_path):
    exit_code = main(
        [
            "sample",
            "--out",
            str(tmp_path / "sample"),
            "--seed",
            "17",
            "--clean",
        ]
    )
    assert exit_code == 0
    assert (tmp_path / "sample" / "params_v1.txt").exists()
    assert (tmp_path / "sample" / "params_v2.txt").exists()
    assert (tmp_path / "sample" / "params_v3.json").exists()
    manifest = json.loads(
        (tmp_path / "sample" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["seed"] == 17

