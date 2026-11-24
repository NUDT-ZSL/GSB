from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from smuggle_detector.io_utils import WorkspaceLock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def run_cli(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = str(SRC) + os.pathsep + child_env.get(
        "PYTHONPATH", ""
    )
    if env:
        child_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "smuggle_detector", *args],
        cwd=str(ROOT),
        env=child_env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cli_import_detect_query_and_verify(
    sample_dir: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / "cli-workspace"
    imported = run_cli(
        "import",
        "--sample-dir",
        str(sample_dir),
        "--workspace",
        str(workspace),
    )
    assert imported.returncode == 0, imported.stderr
    assert json.loads(imported.stdout)["ok"] is True

    detected = run_cli("detect", "--workspace", str(workspace))
    assert detected.returncode == 1
    report = json.loads(detected.stdout)
    assert report["summary"]["task_count"] == 14
    assert report["summary"]["timeout_count"] == 1
    assert (workspace / "report.json").is_file()

    queried = run_cli(
        "query",
        "--workspace",
        str(workspace),
        "--connection",
        "conn-v2-cl-te",
    )
    assert queried.returncode == 0, queried.stderr
    queried_doc = json.loads(queried.stdout)
    assert queried_doc["connections"][0]["rule_view"]["generation"] == 2

    verified = run_cli("verify-checkpoint", "--workspace", str(workspace))
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["valid"] is True
    assert not list(workspace.glob(".report.json.tmp-*"))


def test_cli_parameter_error_returns_two(sample_dir: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "bad-parameter"
    result = run_cli(
        "import",
        "--sample-dir",
        str(sample_dir),
        "--workspace",
        str(workspace),
    )
    assert result.returncode == 0
    detected = run_cli(
        "detect",
        "--workspace",
        str(workspace),
        "--concurrency",
        "0",
    )
    assert detected.returncode == 2
    assert json.loads(detected.stderr)["error"]["code"] == "E_INPUT"


def test_cli_lock_conflict_returns_e_lock_without_writes(
    sample_dir: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / "locked-workspace"
    workspace.mkdir()
    with WorkspaceLock(workspace / ".lock"):
        result = run_cli(
            "import",
            "--sample-dir",
            str(sample_dir),
            "--workspace",
            str(workspace),
        )
    assert result.returncode == 1
    assert json.loads(result.stderr)["error"]["code"] == "E_LOCK"
    assert not (workspace / "normalized_input.json").exists()

