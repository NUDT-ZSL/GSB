from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from walrec.engine import Engine

from conftest import NORMAL, ROOT, json_stdout, run_cli


def test_status_and_fresh_runs_have_bit_identical_output(tmp_path: Path) -> None:
    outputs: list[dict[str, str]] = []
    for index in range(2):
        db_path = tmp_path / f"stable-{index}.db"
        load = run_cli(
            "load",
            "--sample",
            str(NORMAL),
            "--db",
            str(db_path),
            "--reset",
        )
        apply_result = run_cli(
            "apply",
            "--sample",
            str(NORMAL),
            "--db",
            str(db_path),
        )
        status = run_cli(
            "status",
            "--sample",
            str(NORMAL),
            "--db",
            str(db_path),
        )
        status_again = run_cli(
            "status",
            "--sample",
            str(NORMAL),
            "--db",
            str(db_path),
        )
        assert status.stdout == status_again.stdout
        outputs.append(
            {
                "apply": apply_result.stdout,
                "load": load.stdout,
                "status": status.stdout,
            }
        )
    assert outputs[0] == outputs[1]
    parsed = json.loads(outputs[0]["status"])
    assert parsed["status"]["applied_watermark"] == 17


def test_verify_reports_injected_row_transaction(tmp_path: Path) -> None:
    db_path = tmp_path / "verify.db"
    run_cli(
        "load",
        "--sample",
        str(NORMAL),
        "--db",
        str(db_path),
        "--reset",
    )
    run_cli(
        "apply",
        "--sample",
        str(NORMAL),
        "--db",
        str(db_path),
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE data_rows
            SET payload_json = '{"balance":1}'
            WHERE logical_table = 'accounts' AND row_key = 'eve'
            """
        )
        source_tx = connection.execute(
            """
            SELECT source_tx FROM data_rows
            WHERE logical_table = 'accounts' AND row_key = 'eve'
            """
        ).fetchone()[0]
        connection.commit()
    result = run_cli(
        "verify",
        "--sample",
        str(NORMAL),
        "--db",
        str(db_path),
        check=False,
    )
    assert result.returncode == 1
    payload = json_stdout(result)
    assert not payload["ok"]
    assert any(
        mismatch["txid"] == source_tx and mismatch["reason"] == "row_hash_mismatch"
        for mismatch in payload["mismatches"]
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            DELETE FROM data_rows
            WHERE logical_table = 'accounts' AND row_key = 'eve'
            """
        )
        connection.commit()
    deleted = json_stdout(
        run_cli(
            "verify",
            "--sample",
            str(NORMAL),
            "--db",
            str(db_path),
            check=False,
        )
    )
    assert any(
        mismatch["txid"] == 16 and mismatch["reason"] == "row_missing"
        for mismatch in deleted["mismatches"]
    )


def test_multiple_processes_apply_each_transaction_once(tmp_path: Path) -> None:
    db_path = tmp_path / "concurrent.db"
    run_cli(
        "load",
        "--sample",
        str(NORMAL),
        "--db",
        str(db_path),
        "--reset",
    )
    import os

    process_env = os.environ.copy()
    process_env["PYTHONPATH"] = str(ROOT)
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "walrec",
                "apply",
                "--sample",
                str(NORMAL),
                "--db",
                str(db_path),
                "--no-auto-checkpoint",
            ],
            cwd=ROOT,
            env=process_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    results = [process.communicate(timeout=30) for process in processes]
    for process, (stdout, stderr) in zip(processes, results):
        assert process.returncode == 0, f"stdout={stdout}\nstderr={stderr}"

    status = json_stdout(
        run_cli(
            "status",
            "--sample",
            str(NORMAL),
            "--db",
            str(db_path),
        )
    )["status"]
    assert status["applied_watermark"] == 17
    assert status["applied_tx_count"] == 17
    assert status["in_flight"] == []
    assert json_stdout(
        run_cli(
            "verify",
            "--sample",
            str(NORMAL),
            "--db",
            str(db_path),
        )
    )["ok"]

    checkpoint_processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "walrec",
                "checkpoint",
                "--sample",
                str(NORMAL),
                "--db",
                str(db_path),
                "--clock-ms",
                "1700",
            ],
            cwd=ROOT,
            env=process_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    checkpoint_results = [
        process.communicate(timeout=30) for process in checkpoint_processes
    ]
    for process, (stdout, stderr) in zip(checkpoint_processes, checkpoint_results):
        assert process.returncode == 0, f"stdout={stdout}\nstderr={stderr}"
    with sqlite3.connect(db_path) as connection:
        package_count = connection.execute(
            "SELECT COUNT(*) FROM checkpoint_history WHERE checkpoint_gen = 3"
        ).fetchone()[0]
    assert package_count == 1

    reference = Engine(tmp_path / "reference.db")
    reference.load(NORMAL)
    reference.apply(NORMAL, no_auto_checkpoint=True)
    assert Engine(db_path).compare(tmp_path / "reference.db")["ok"]

    before = Engine(db_path).snapshot()
    run_cli(
        "apply",
        "--sample",
        str(NORMAL),
        "--db",
        str(db_path),
        "--no-auto-checkpoint",
    )
    assert Engine(db_path).snapshot() == before
