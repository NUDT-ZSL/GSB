from __future__ import annotations

import json
import sqlite3

from bsl_workbench.checkpoint import verify_checkpoint, write_snapshot
from bsl_workbench.ledger import Ledger
from bsl_workbench.parser import parse_file
from bsl_workbench.locking import WorkbenchLock
from bsl_workbench.util import safe_file_id


def _recovery_files(sample_root):
    return [
        sample_root / "recovery" / "recovery_a.bsl",
        sample_root / "recovery" / "recovery_b.bsl",
        sample_root / "recovery" / "recovery_c.bsl",
    ]


def test_kill_before_snapshot_then_recover_and_resume(
    sample_root, tmp_path, cli_runner
) -> None:
    workdir = tmp_path / "workbench"
    files = _recovery_files(sample_root)

    killed = cli_runner(
        [
            "parse",
            "--workdir",
            workdir,
            "--quiet",
            "--kill-after-files",
            "1",
            "--kill-point",
            "before_snapshot",
            *files,
        ]
    )
    assert killed.returncode != 0

    bad_verify = cli_runner(["verify-checkpoint", "--workdir", workdir])
    assert bad_verify.returncode == 1
    bad_payload = json.loads(bad_verify.stdout)
    assert bad_payload["ok"] is False
    assert bad_payload["temporary_files"]

    recovered = cli_runner(["verify-checkpoint", "--workdir", workdir, "--recover"])
    assert recovered.returncode == 0
    recovered_payload = json.loads(recovered.stdout)
    assert recovered_payload["ok"] is True
    assert recovered_payload["snapshot_match"] is True

    report_path = tmp_path / "batch.json"
    resumed = cli_runner(
        [
            "parse",
            "--workdir",
            workdir,
            "--quiet",
            "--report",
            report_path,
            *files,
        ]
    )
    assert resumed.returncode == 0, resumed.stderr
    batch = json.loads(report_path.read_text(encoding="utf-8"))
    assert batch["summary"]["total"] == 3
    assert batch["summary"]["failed"] == 0

    connection = sqlite3.connect(workdir / "ledger.sqlite3")
    generations = dict(
        connection.execute("SELECT file_id, latest_generation FROM files").fetchall()
    )
    checkpoint_sequence = connection.execute(
        "SELECT MAX(sequence) FROM checkpoints"
    ).fetchone()[0]
    connection.close()

    expected_ids = {safe_file_id(path) for path in files}
    assert set(generations) == expected_ids
    assert set(generations.values()) == {1}
    assert checkpoint_sequence == 3


def test_kill_after_complete_snapshot_is_immediately_consistent(
    sample_root, tmp_path, cli_runner
) -> None:
    workdir = tmp_path / "workbench"
    files = _recovery_files(sample_root)
    killed = cli_runner(
        [
            "parse",
            "--workdir",
            workdir,
            "--quiet",
            "--kill-after-files",
            "1",
            "--kill-point",
            "after_snapshot",
            *files[:2],
        ]
    )
    assert killed.returncode == 137

    verified = cli_runner(["verify-checkpoint", "--workdir", workdir])
    assert verified.returncode == 0
    payload = json.loads(verified.stdout)
    assert payload["ok"] is True
    assert payload["latest_sequence"] == 1

    resumed = cli_runner(
        ["parse", "--workdir", workdir, "--quiet", *files[:2]]
    )
    assert resumed.returncode == 0


def test_lock_conflict_returns_e_lock(sample_root, tmp_path, cli_runner) -> None:
    workdir = tmp_path / "locked-workbench"
    with WorkbenchLock(workdir):
        result = cli_runner(
            [
                "parse",
                "--workdir",
                workdir,
                "--quiet",
                sample_root / "valid" / "valid_basic.bsl",
            ]
        )
    assert result.returncode == 3
    payload = json.loads(result.stderr)
    assert payload["code"] == "E_LOCK"


def test_checkpoint_detects_ledger_digest_tamper(sample_root, tmp_path) -> None:
    workdir = tmp_path / "tamper-wd"
    sample = sample_root / "valid" / "valid_basic.bsl"
    report = parse_file(sample, file_id="tamper-demo")

    with Ledger(workdir / "ledger.sqlite3") as ledger:
        checkpoint = ledger.commit_file_report(
            report, source_path=str(sample), completed_files={}
        )
        write_snapshot(checkpoint, workdir)

    connection = sqlite3.connect(workdir / "ledger.sqlite3")
    connection.execute(
        "UPDATE segments SET state = 'tampered' WHERE canonical_key = '/alpha'"
    )
    connection.commit()
    connection.close()

    with Ledger(workdir / "ledger.sqlite3") as ledger:
        result = verify_checkpoint(workdir, ledger=ledger)

    assert result["ok"] is False
    assert result["digest_match"] is False
    assert any("digest" in issue for issue in result["issues"])
