from __future__ import annotations

from pathlib import Path

from sensitivity_engine.cli import main


def test_full_cli_workflow_and_exit_codes(tmp_path: Path, capsys):
    root = tmp_path / "workspace"
    code = main(["generate-sample", "--root", str(root)])
    assert code == 0

    db = root / "run" / "ledger.sqlite3"
    report = root / "run" / "import.json"
    checkpoint = root / "run" / "checkpoint.json"
    reentry = root / "run" / "reentry.json"

    code = main(
        [
            "import",
            "--data",
            str(root / "data"),
            "--db",
            str(db),
            "--report",
            str(report),
            "--checkpoint",
            str(checkpoint),
            "--concurrency",
            "3",
            "--queue-limit",
            "8",
        ]
    )
    assert code == 0
    assert report.exists()
    assert checkpoint.exists()

    assert main(["query", "--db", str(db), "--object", "obj:finance"]) == 0
    query_output = capsys.readouterr().out
    assert "RESTRICTED" in query_output

    assert main(["export-reentry", "--db", str(db), "--out", str(reentry)]) == 0
    assert reentry.exists()
    assert main(["verify-checkpoint", "--db", str(db), "--snapshot", str(checkpoint)]) == 0
    assert (
        main(
            [
                "verify-truth",
                "--db",
                str(db),
                "--truth",
                str(root / "truth" / "expected.json"),
            ]
        )
        == 0
    )


def test_invalid_sample_returns_partial_exit(tmp_path: Path):
    root = tmp_path / "workspace"
    main(["generate-sample", "--root", str(root)])
    code = main(
        [
            "import",
            "--data",
            str(root / "data" / "invalid"),
            "--db",
            str(root / "invalid.sqlite3"),
            "--report",
            str(root / "invalid-import.json"),
            "--checkpoint",
            str(root / "invalid-checkpoint.json"),
        ]
    )
    assert code == 1


def test_parameter_error_exit_code(tmp_path: Path):
    code = main(
        [
            "import",
            "--data",
            str(tmp_path),
            "--db",
            str(tmp_path / "x.sqlite3"),
            "--concurrency",
            "0",
        ]
    )
    assert code == 2


def test_argparse_error_exit_code():
    assert main(["not-a-command"]) == 2
