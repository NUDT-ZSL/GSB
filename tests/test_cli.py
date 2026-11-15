from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "card_replay", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_demo_command_is_byte_deterministic_and_compare_succeeds(tmp_path: Path) -> None:
    left_dir = tmp_path / "left"
    right_dir = tmp_path / "right"
    left_run = _run_cli(
        "demo",
        "--out-dir",
        str(left_dir),
        "--threads",
        "4",
        "--memory-budget-mib",
        "64",
        "--per-match-memory-mib",
        "8",
    )
    right_run = _run_cli(
        "demo",
        "--out-dir",
        str(right_dir),
        "--threads",
        "4",
        "--memory-budget-mib",
        "64",
        "--per-match-memory-mib",
        "8",
    )
    assert left_run.returncode == 0, left_run.stderr
    assert right_run.returncode == 0, right_run.stderr

    left_report_bytes = (left_dir / "report.json").read_bytes()
    right_report_bytes = (right_dir / "report.json").read_bytes()
    assert left_report_bytes == right_report_bytes

    report = json.loads(left_report_bytes)
    assert report["summary"]["violations_by_code"]["E_SEQ_GAP"] == 1
    assert report["summary"]["violations_by_code"]["E_TRIGGER_DEPTH"] == 1
    assert report["summary"]["violations_by_code"]["E_RESOURCE_OVERFLOW"] == 2

    comparison_path = tmp_path / "compare.json"
    compare_run = _run_cli(
        "compare",
        str(left_dir / "report.json"),
        str(right_dir / "report.json"),
        "--out",
        str(comparison_path),
    )
    assert compare_run.returncode == 0, compare_run.stderr
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    assert comparison["verdict"] == "consistent"
    assert comparison["checks"]["all_event_state_hashes_equal"] is True


def test_cli_reports_bad_config_without_traceback(tmp_path: Path) -> None:
    config_path = tmp_path / "bad.json"
    config_path.write_text("{not-json", encoding="utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("", encoding="utf-8")
    result = _run_cli(
        "verify",
        "--events",
        str(events_path),
        "--config",
        str(config_path),
        "--out",
        str(tmp_path / "report.json"),
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    error = json.loads(result.stderr)
    assert error["error"]["violation"]["code"] == "E_CONFIG_INVALID"
