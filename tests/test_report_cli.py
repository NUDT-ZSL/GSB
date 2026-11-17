from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from klp_audit.atomic_io import FileLock, atomic_write_bytes, atomic_write_json
from klp_audit.config import AuditConfig

from .helpers import audit_path, load_json


def _run_cli(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    return subprocess.run(
        [sys.executable, "-m", "klp_audit", *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_exit_codes_and_business_codes(generated_root, manifest, tmp_path):
    legal = generated_root / manifest["files"]["legal"]["sample"]
    parse_errors = generated_root / manifest["files"]["parse_errors"]["sample"]
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    ok = _run_cli(
        [
            "audit",
            str(legal),
            "--workspace",
            str(workspace),
            "--report",
            str(workspace / "legal.json"),
        ],
        cwd=tmp_path,
    )
    assert ok.returncode == 0, ok.stderr
    assert "PASS" in ok.stderr
    assert load_json(workspace / "legal.json")["file"]["conclusion"] == "PASS"

    partial = _run_cli(
        [
            "audit",
            str(parse_errors),
            "--workspace",
            str(workspace),
            "--report",
            str(workspace / "parse.json"),
        ],
        cwd=tmp_path,
    )
    assert partial.returncode == 1
    report = load_json(workspace / "parse.json")
    report_codes = set(report["summary"]["business_error_codes"])
    assert {"E_CRC", "E_FORMAT", "E_ALGORITHM", "E_STAGE", "E_PARAM"} <= report_codes
    for code in report_codes:
        assert code in partial.stderr

    usage = _run_cli(
        ["audit", str(legal), "--workspace", str(workspace), "--concurrency", "0"],
        cwd=tmp_path,
    )
    assert usage.returncode == 2
    assert "E_PARAM" in usage.stderr

    missing = _run_cli(
        ["audit", "does-not-exist.klp", "--workspace", str(workspace)],
        cwd=tmp_path,
    )
    assert missing.returncode == 2
    assert "E_INPUT" in missing.stderr


def test_degraded_run_returns_partial_exit_code(generated_root, manifest, tmp_path):
    sample = generated_root / manifest["files"]["scale_large"]["sample"]
    result = _run_cli(
        [
            "audit",
            str(sample),
            "--workspace",
            str(tmp_path),
            "--report",
            str(tmp_path / "degraded.json"),
            "--scale-derived",
            "10",
        ],
        cwd=tmp_path,
    )
    assert result.returncode == 1
    report = load_json(tmp_path / "degraded.json")
    assert report["file"]["conclusion"] == "DEGRADED_PASS"
    assert report["adaptive"]["degraded"] is True


def test_report_is_atomic_and_stable(generated_root, manifest, tmp_path):
    sample = generated_root / manifest["files"]["legal"]["sample"]
    report = audit_path(sample, config=AuditConfig(concurrency=1))
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    lock = tmp_path / ".lock"
    atomic_write_bytes(first, b"old", lock_path=lock)
    atomic_write_json(first, report, lock_path=lock)
    atomic_write_json(second, report, lock_path=lock)
    assert first.read_bytes() == second.read_bytes()
    parsed = json.loads(first.read_text(encoding="utf-8"))
    assert parsed["sessions"][0]["session_id"] == "alpha"
    assert list(parsed) == sorted(parsed)
    assert not list(tmp_path.glob("*.tmp"))


def test_cli_lock_conflict_returns_e_lock(generated_root, manifest, tmp_path):
    sample = generated_root / manifest["files"]["legal"]["sample"]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with FileLock(workspace / ".klp-workspace.lock"):
        result = _run_cli(
            [
                "audit",
                str(sample),
                "--workspace",
                str(workspace),
                "--report",
                str(workspace / "report.json"),
            ],
            cwd=tmp_path,
        )
    assert result.returncode == 1
    assert "E_LOCK" in result.stderr
    assert not (workspace / "report.json").exists()

