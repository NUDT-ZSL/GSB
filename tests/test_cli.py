from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from sso_ticket_guard.samplegen import DEFAULT_SEED, generate_samples

ROOT = Path(__file__).resolve().parents[1]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "sso_ticket_guard.cli", *args],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_exit_codes_and_atomic_reports(tmp_path: Path):
    samples = tmp_path / "samples"
    generate_samples(samples, DEFAULT_SEED)
    reports = tmp_path / "reports"

    imported = _run(
        "import",
        "--samples",
        str(samples),
        "--output",
        str(reports / "import.json"),
    )
    assert imported.returncode == 1
    assert json.loads((reports / "import.json").read_text(encoding="utf-8"))[
        "status"
    ] == "partial"
    assert list(reports.glob("*.tmp")) == []
    assert list(reports.glob(".*.tmp")) == []

    missing = _run("import", "--samples", str(tmp_path / "absent"))
    assert missing.returncode == 2

    bad_argument = _run("checkpoint", "not-an-action")
    assert bad_argument.returncode == 2


def test_cli_clean_import_returns_zero(tmp_path: Path):
    samples = tmp_path / "samples"
    generate_samples(samples, DEFAULT_SEED)
    for name, invalid_indexes in {
        "platforms.json": {3, 4},
        "fingerprints.json": {8, 9, 10},
        "tickets.json": {17, 18, 19, 20},
        "renewals.json": {13, 14, 15},
        "quotas.json": {2},
    }.items():
        path = samples / name
        records = json.loads(path.read_text(encoding="utf-8"))
        records = [
            record for index, record in enumerate(records) if index not in invalid_indexes
        ]
        path.write_text(json.dumps(records), encoding="utf-8")
    output = tmp_path / "clean-import.json"
    result = _run("import", "--samples", str(samples), "--output", str(output))
    assert result.returncode == 0
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "ok"


def test_cli_interruption_then_resume(tmp_path: Path):
    samples = tmp_path / "samples"
    generate_samples(samples, DEFAULT_SEED)
    state = tmp_path / "state"
    first_report = tmp_path / "first.json"
    second_report = tmp_path / "second.json"

    first = _run(
        "renew",
        "--samples",
        str(samples),
        "--state-dir",
        str(state),
        "--fresh",
        "--max-batches",
        "2",
        "--output",
        str(first_report),
    )
    assert first.returncode == 1
    first_data = json.loads(first_report.read_text(encoding="utf-8"))
    assert first_data["checkpoint"]["stopped_early"] is True
    assert first_data["checkpoint"]["processed_batches"] == [1, 2]

    second = _run(
        "renew",
        "--samples",
        str(samples),
        "--state-dir",
        str(state),
        "--output",
        str(second_report),
    )
    assert second.returncode == 1
    second_data = json.loads(second_report.read_text(encoding="utf-8"))
    assert second_data["checkpoint"]["processed_batches"] == [3, 4, 5]
    assert second_data["checkpoint"]["cumulative"]["watermark"] == 5
    assert len(second_data["checkpoint"]["cumulative"]["processed_request_ids"]) == 13
    assert len(
        set(second_data["checkpoint"]["cumulative"]["processed_request_ids"])
    ) == 13

    verified = _run(
        "checkpoint",
        "verify",
        "--samples",
        str(samples),
        "--state-dir",
        str(state),
        "--output",
        str(tmp_path / "verify.json"),
    )
    assert verified.returncode == 0
    verify_data = json.loads((tmp_path / "verify.json").read_text(encoding="utf-8"))
    assert verify_data["checkpoint"]["watermark"] == 5
    assert verify_data["checkpoint"]["diagnostics"]["replay_verified"] is True

