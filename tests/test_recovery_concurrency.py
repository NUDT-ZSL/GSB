from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from conftest import CLOCK, ROOT, SAMPLE, audit, run_cli, verify_sample


def _status(db: Path) -> dict:
    return json.loads(run_cli("--db", str(db), "status").stdout)


def test_forced_termination_during_verify_then_resume(
    loaded_db: Path, one_request_file: Path
) -> None:
    completed = run_cli(
        "--db",
        str(loaded_db),
        "verify",
        "--requests",
        str(one_request_file),
        "--cert-dir",
        str(SAMPLE / "certs"),
        "--revocation-evidence",
        str(SAMPLE / "revocation/evidence.json"),
        env={"TLS_TRUST_CRASH_POINT": "verify_before_commit"},
        check=False,
    )
    assert completed.returncode == 99
    status = _status(loaded_db)
    assert status["active"]["running_jobs"] == 1
    assert status["active"]["chains"] == 0
    assert status["active"]["revocations"] == 0
    resumed = json.loads(
        run_cli(
            "--db",
            str(loaded_db),
            "resume",
            "--cert-dir",
            str(SAMPLE / "certs"),
            "--revocation-evidence",
            str(SAMPLE / "revocation/evidence.json"),
        ).stdout
    )
    assert resumed["running_before"] == 1
    assert len(resumed["resumed"]) == 1
    status = _status(loaded_db)
    assert status["active"]["chains"] == 1
    assert status["active"]["running_jobs"] == 0
    assert audit(loaded_db)["ok"] is True


def test_forced_termination_during_apply_then_resume(loaded_db: Path) -> None:
    completed = run_cli(
        "--db",
        str(loaded_db),
        "apply",
        "--patch",
        str(SAMPLE / "patches/p1-tombstone-root-retired.json"),
        "--cert-dir",
        str(SAMPLE / "certs"),
        "--logical-clock-ms",
        str(CLOCK),
        env={"TLS_TRUST_CRASH_POINT": "apply_before_commit"},
        check=False,
    )
    assert completed.returncode == 99
    status = _status(loaded_db)
    assert status["current_version"] == "trust-v2"
    assert status["active"]["running_jobs"] == 1
    resumed = json.loads(
        run_cli(
            "--db",
            str(loaded_db),
            "resume",
            "--cert-dir",
            str(SAMPLE / "certs"),
            "--revocation-evidence",
            str(SAMPLE / "revocation/evidence.json"),
        ).stdout
    )
    assert resumed["running_before"] == 1
    status = _status(loaded_db)
    assert status["current_version"] == "trust-v2-p1"
    assert status["active"]["running_jobs"] == 0
    assert audit(loaded_db)["ok"] is True


def test_multiple_verifiers_are_consistent(loaded_db: Path) -> None:
    command = [
        sys.executable,
        "-m",
        "tls_trust_pipeline",
        "--db",
        str(loaded_db),
        "verify",
        "--requests",
        str(SAMPLE / "requests/handshake-requests.json"),
        "--cert-dir",
        str(SAMPLE / "certs"),
        "--revocation-evidence",
        str(SAMPLE / "revocation/evidence.json"),
    ]
    processes = [
        subprocess.Popen(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(4)
    ]
    results = [process.communicate(timeout=60) for process in processes]
    for process, (stdout, stderr) in zip(processes, results):
        assert process.returncode == 0, stderr
        assert json.loads(stdout)["ok"] is True
    connection = sqlite3.connect(loaded_db)
    try:
        chain_count = connection.execute(
            "SELECT COUNT(*) FROM chains WHERE generation=2 AND state='active'"
        ).fetchone()[0]
        duplicate_count = connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT hostname, purpose, generation, COUNT(*) AS c
                FROM chains GROUP BY hostname, purpose, generation HAVING c > 1
            )
            """
        ).fetchone()[0]
        running = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE status='running'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert chain_count == 10
    assert duplicate_count == 0
    assert running == 0
    assert audit(loaded_db)["ok"] is True


def test_verifiers_and_patch_writer_can_run_concurrently(loaded_db: Path) -> None:
    base = [sys.executable, "-m", "tls_trust_pipeline", "--db", str(loaded_db)]
    verify_command = [
        *base,
        "verify",
        "--requests",
        str(SAMPLE / "requests/handshake-requests.json"),
        "--cert-dir",
        str(SAMPLE / "certs"),
        "--revocation-evidence",
        str(SAMPLE / "revocation/evidence.json"),
    ]
    apply_command = [
        *base,
        "apply",
        "--patch",
        str(SAMPLE / "patches/p1-tombstone-root-retired.json"),
        "--cert-dir",
        str(SAMPLE / "certs"),
        "--logical-clock-ms",
        str(CLOCK),
    ]
    process_specs = [
        (subprocess.Popen(verify_command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE), "verify")
        for _ in range(3)
    ]
    process_specs.append(
        (subprocess.Popen(apply_command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE), "apply")
    )
    for process, kind in process_specs:
        stdout, stderr = process.communicate(timeout=90)
        assert process.returncode == 0, f"{kind}: {stderr}"
        assert json.loads(stdout)["ok"] is True
    verify_sample(loaded_db)
    status = _status(loaded_db)
    assert status["current_version"] == "trust-v2-p1"
    assert status["active"]["chains"] == 10
    assert status["active"]["running_jobs"] == 0
    assert audit(loaded_db)["ok"] is True

