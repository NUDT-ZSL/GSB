from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from schema_migrator.manager import load_workspace, resume_workspace, status_workspace, verify_workspace


ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    ("failpoint", "committed_before_crash", "reconciled"),
    [
        ("add_column", "m001.001", ["m001.002", "m001.003", "m001.004", "m001.005", "m001.006"]),
        ("replace_view", "m001.003", ["m001.004", "m001.005", "m001.006"]),
        ("alter_column_type", "m001.004", ["m001.005", "m001.006"]),
    ],
)
def test_hard_kill_resume_replays_only_unfinished_operations(
    tmp_path,
    sample_paths,
    failpoint,
    committed_before_crash,
    reconciled,
):
    workspace = tmp_path / "workspace"
    load_workspace(
        workspace,
        sample_paths["schemas"],
        sample_paths["migrations"],
        sample_paths["queries"],
    )
    crashed = subprocess.run(
        [
            sys.executable,
            "-m",
            "schema_migrator",
            "--workspace",
            str(workspace),
            "migrate",
            "--migration",
            "m001",
            "--failpoint",
            failpoint,
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert crashed.returncode == 91
    interrupted = status_workspace(workspace)
    assert interrupted["current_version"] == 1
    assert interrupted["pending_journal"]["status"] == "RUNNING"
    recovered = resume_workspace(workspace)
    assert recovered["change_kind"] == "RECOVERED"
    assert recovered["current_version"] == 2
    assert recovered["reconciled_operations"] == reconciled
    assert committed_before_crash not in recovered["reconciled_operations"]
    alter_logs = [
        item
        for item in recovered["report"]["conversion_log"]
        if item["operation_id"] == "m001.004"
    ]
    assert len(alter_logs) == 4
    assert verify_workspace(workspace)["ok"] is True

