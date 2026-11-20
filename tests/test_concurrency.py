from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from schema_migrator.manager import load_workspace, status_workspace, verify_workspace


ROOT = Path(__file__).resolve().parent.parent


def test_two_migration_processes_commit_once(tmp_path, sample_paths):
    workspace = tmp_path / "workspace"
    load_workspace(
        workspace,
        sample_paths["schemas"],
        sample_paths["migrations"],
        sample_paths["queries"],
    )
    command = [
        sys.executable,
        "-m",
        "schema_migrator",
        "--workspace",
        str(workspace),
        "migrate",
        "--migration",
        "m001",
    ]
    left = subprocess.Popen(command, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    right = subprocess.Popen(command, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    left_out, left_err = left.communicate(timeout=30)
    right_out, right_err = right.communicate(timeout=30)
    assert left.returncode == 0, left_err
    assert right.returncode == 0, right_err
    payloads = [json.loads(left_out), json.loads(right_out)]
    assert sorted(item["change_kind"] for item in payloads) == ["IDEMPOTENT", "INCREMENTAL_APPEND"]
    assert status_workspace(workspace)["current_version"] == 2
    assert verify_workspace(workspace)["ok"] is True

