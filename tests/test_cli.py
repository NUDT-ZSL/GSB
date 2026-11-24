from __future__ import annotations

import json
from pathlib import Path

import pytest

from firewall_audit.cli import main


def _json_stdout(capsys: pytest.CaptureFixture[str]) -> dict:
    captured = capsys.readouterr()
    return json.loads(captured.out)


def test_cli_four_subcommands_and_jobs_consistency(
    sample_manifest: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reports: list[dict] = []
    for jobs in (1, 4):
        root = tmp_path / f"jobs-{jobs}"
        db = root / "ledger.sqlite3"
        checkpoint = root / "checkpoint.json"
        import_report = root / "import.json"
        audit_report = root / "audit.json"
        assert main(
            [
                "import-snapshot",
                "--sample",
                str(sample_manifest),
                "--db",
                str(db),
                "--checkpoint",
                str(checkpoint),
                "--report",
                str(import_report),
            ]
        ) == 0
        imported = _json_stdout(capsys)
        assert imported["command"] == "import-snapshot"
        assert json.loads(import_report.read_text())["report_type"] == "import"

        assert main(
            [
                "adjudicate",
                "--db",
                str(db),
                "--checkpoint",
                str(checkpoint),
                "--report",
                str(audit_report),
                "--jobs",
                str(jobs),
            ]
        ) == 0
        summary = _json_stdout(capsys)
        assert summary["command"] == "adjudicate"
        report = json.loads(audit_report.read_text())
        reports.append(report)
        assert report["report_type"] == "equivalence"
        assert len(report["cases"]) == 9
        for case in report["cases"]:
            assert {"v1", "v2", "unified", "code", "reason"} <= set(case)
            assert "basis_generation" in case["v1"]
            assert "basis_generation" in case["v2"]
        assert "invalidation_closure" in report
        assert "mapping_reconciliation" in report

        diagnostic = root / "diagnostic.json"
        assert main(
            [
                "export-report",
                "--db",
                str(db),
                "--report",
                str(diagnostic),
            ]
        ) == 0
        assert _json_stdout(capsys)["command"] == "export-report"
        assert json.loads(diagnostic.read_text())["report_type"] == "diagnostic"

        verify = root / "verify.json"
        assert main(
            [
                "verify-checkpoint",
                "--checkpoint",
                str(checkpoint),
                "--db",
                str(db),
                "--report",
                str(verify),
                "--require-match",
            ]
        ) == 0
        assert _json_stdout(capsys)["code"] == "OK"
        assert json.loads(verify.read_text())["after"]["matches_ledger"] is True

    serial_by_sync = {
        case["sync_marker"]: case for case in reports[0]["cases"]
    }
    parallel_by_sync = {
        case["sync_marker"]: case for case in reports[1]["cases"]
    }
    assert set(serial_by_sync) == set(parallel_by_sync)
    for sync_marker in sorted(serial_by_sync):
        assert serial_by_sync[sync_marker] == parallel_by_sync[sync_marker]


def test_cli_mapping_patch_uses_current_ledger(
    sample_manifest: Path,
    generated_sample: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "ledger.sqlite3"
    checkpoint = tmp_path / "checkpoint.json"
    assert main(
        [
            "import-snapshot",
            "--sample",
            str(sample_manifest),
            "--db",
            str(db),
            "--checkpoint",
            str(checkpoint),
            "--report",
            str(tmp_path / "import.json"),
        ]
    ) == 0
    _json_stdout(capsys)
    assert main(
        [
            "adjudicate",
            "--db",
            str(db),
            "--checkpoint",
            str(checkpoint),
            "--report",
            str(tmp_path / "audit.json"),
        ]
    ) == 0
    _json_stdout(capsys)
    assert main(
        [
            "import-snapshot",
            "--mapping-patch",
            str(generated_sample / "incremental" / "mapping-patch-v4.json"),
            "--db",
            str(db),
            "--checkpoint",
            str(checkpoint),
            "--report",
            str(tmp_path / "patch.json"),
        ]
    ) == 0
    summary = _json_stdout(capsys)
    assert summary["pending_cases"] == ["case-semantic"]
    patch_report = json.loads((tmp_path / "patch.json").read_text())
    assert patch_report["invalidation_closure"]["affected_cases"] == [
        "case-semantic"
    ]
