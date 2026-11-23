from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pose_diag.cli import main


def test_cli_diagnose_acceptance(
    manifest_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "report.json"
    status = main(
        [
            "diagnose",
            "--manifest",
            str(manifest_path),
            "--state-dir",
            str(tmp_path / "state"),
            "--report",
            str(report),
            "--baseline-report",
            str(tmp_path / "baseline.json"),
            "--preview-report",
            str(tmp_path / "preview.json"),
            "--scene-limit",
            "1",
        ]
    )
    assert status == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["business_code"] == "OK"
    assert all(payload["acceptance"].values())
    assert "E_SNAPSHOT_ROLLBACK" in payload["business_codes_observed"]
    assert "E_SNAPSHOT_CORRUPT" in payload["business_codes_observed"]
    assert (
        payload["scenes"][0]["snapshot_invalidation"]["scoring_only"][
            "cache_stats"
        ]["group"]["hits"]
        >= 1
    )


def test_gui_imports_and_smoke_window(
    manifest_path: Path, tmp_path: Path
) -> None:
    pytest.importorskip("PySide6")
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pose_diag",
            "gui",
            "--smoke-test",
            "--manifest",
            str(manifest_path),
            "--state-dir",
            str(tmp_path / "gui_state"),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

