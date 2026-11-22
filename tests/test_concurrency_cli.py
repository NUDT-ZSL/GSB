from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys

from conftest import ROOT, cli

from log_retention.config import Config


def _prepare(config: Config) -> None:
    assert cli(config, "load").returncode == 0
    assert cli(config, "merge").returncode == 0


def test_parallel_instance_workers_commit_one_view(config: Config) -> None:
    _prepare(config)
    processes = []
    env = os.environ.copy()
    for instance_id in ("replica-a", "replica-b", "replica-c"):
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "log_retention.cli",
                    "--config",
                    str(config.path),
                    "judge",
                    "--instance",
                    instance_id,
                ],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr
        outputs.append(json.loads(stdout))
    committed = [output for output in outputs if output.get("status") == "committed"]
    assert len(committed) == 1
    view_version = committed[0]["view_version"]
    status = json.loads(cli(config, "status").stdout)
    assert status["view_version"] == view_version
    assert status["unresolved_conflicts"][0]["segment_id"] == 3
    with sqlite3.connect(config.ledger_db) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM operations WHERE kind = 'judge' AND status = 'committed'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM op_calculations WHERE state = 'committed'"
            ).fetchone()[0]
            == 3
        )
        windows = conn.execute(
            "SELECT instance_id, logical_clock_ms, remainder_units "
            "FROM retention_windows ORDER BY instance_id"
        ).fetchall()
        assert windows == [
            ("replica-a", 20000, 0),
            ("replica-b", 20000, 1000),
            ("replica-c", 20000, 5000),
        ]
    repeat = json.loads(cli(config, "judge").stdout)
    assert repeat["view_version"] == view_version
    assert repeat["segments"] == committed[0]["segments"]
    assert json.loads(cli(config, "verify").stdout)["status"] == "ok"


def test_repeated_cli_output_is_byte_stable(config: Config) -> None:
    _prepare(config)
    first = cli(config, "judge")
    second = cli(config, "judge")
    assert first.stdout == second.stdout
    assert first.stderr == ""
    assert second.stderr == ""


def test_parallel_workers_then_rebuild_and_verify(config: Config) -> None:
    _prepare(config)
    assert cli(config, "judge").returncode == 0
    rebuilt = json.loads(cli(config, "rebuild").stdout)
    assert rebuilt["clean_equivalence"]["all_equal"] is True
    verified = json.loads(cli(config, "verify").stdout)
    assert verified["status"] == "ok"
    second_rebuild = json.loads(cli(config, "rebuild").stdout)
    assert second_rebuild["status"] == "no_conflicts"
    assert second_rebuild["view_version"] == rebuilt["view_version"]
