from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from schema_migrator.manager import load_workspace, status_workspace, verify_workspace


ROOT = Path(__file__).resolve().parent.parent


def test_demo_output_is_byte_identical_on_repeated_runs(tmp_path, sample_paths):
    command = [
        sys.executable,
        "-m",
        "schema_migrator.demo",
        "--base",
        str(tmp_path / "demo"),
    ]
    first = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    second = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first.stdout == second.stdout
    payload = json.loads(first.stdout)
    assert payload["identical"] is True
    assert payload["summary"]["final_version"] == 4
    assert payload["summary"]["verify_ok"] is True
    assert payload["summary"]["recovered"] is True


def test_verify_detects_sqlite_schema_tampering(tmp_path, sample_paths):
    workspace = tmp_path / "workspace"
    load_workspace(
        workspace,
        sample_paths["schemas"],
        sample_paths["migrations"],
        sample_paths["queries"],
    )
    status = status_workspace(workspace)
    snapshot = workspace / "snapshots" / status["snapshot"]
    conn = sqlite3.connect(str(snapshot))
    try:
        conn.execute("ALTER TABLE customers ADD COLUMN injected TEXT")
        conn.commit()
    finally:
        conn.close()
    result = verify_workspace(workspace)
    assert result["ok"] is False
    sqlite_check = next(item for item in result["checks"] if item["name"] == "sqlite_schema")
    assert sqlite_check["ok"] is False
    assert any(issue["code"] == "E_EXTRA_COLUMN" for issue in sqlite_check["issues"])

