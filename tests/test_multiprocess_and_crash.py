from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from chaos_budget.bundle import load_bundle
from chaos_budget.reporting import verify_database
from chaos_budget.storage import Database
from generate_samples import generate
import main as cli


REPO_ROOT = Path(cli.__file__).resolve().parent
MAIN_PY = REPO_ROOT / "main.py"


def run_subprocess_cli(sample_dir: Path, db_path: Path, *args: str, crash_stage: str | None = None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    if crash_stage:
        env["CHAOS_CRASH_STAGE"] = crash_stage
    return subprocess.run(
        [
            sys.executable,
            str(MAIN_PY),
            "--sample-dir",
            str(sample_dir),
            "--db",
            str(db_path),
            *args,
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def write_simple_sample(sample_dir: Path, *, count: int = 6, round_budget: int = 100000, concurrency: int = 10):
    generate(sample_dir)
    experiments = [
        {
            "experiment_id": f"PAR-{index:02d}",
            "target_service": f"svc-{index}",
            "fault_type": "latency_injection",
            "fault_params": {"index": index},
            "start_logical_clock_ms": 1000,
            "expected_duration_ms": 100,
            "priority": index * 10,
            "round_id": "default",
        }
        for index in range(1, count + 1)
    ]
    (sample_dir / "experiments.json").write_text(
        json.dumps({"experiments": experiments}), encoding="utf-8"
    )
    (sample_dir / "observations.json").write_text(json.dumps({"observations": []}), encoding="utf-8")
    budget = {
        "per_experiment_budget": 10000,
        "round_logical_time_budget_ms": round_budget,
        "global_concurrency_limit": concurrency,
        "backoff_base_ms": 100,
        "backoff_cap_ms": 1000,
        "phase_charge_points": 1,
    }
    (sample_dir / "budget_config.json").write_text(
        json.dumps({"budget_config": budget}), encoding="utf-8"
    )


def initialize_db(sample_dir: Path, db_path: Path) -> Database:
    bundle = load_bundle(sample_dir)
    db = Database.initialize(db_path, bundle, reset=True)
    db.close()
    return db


def test_parallel_processes_admit_different_experiments_once(tmp_path):
    sample_dir = tmp_path / "sample"
    db_path = tmp_path / "runtime" / "parallel.sqlite3"
    write_simple_sample(sample_dir, count=4)
    initialize_db(sample_dir, db_path)

    processes = [
        subprocess.Popen(
            [
                sys.executable,
                str(MAIN_PY),
                "--sample-dir",
                str(sample_dir),
                "--db",
                str(db_path),
                "run",
                "--experiment",
                f"PAR-{index:02d}",
                "--clock",
                "1000",
            ],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(1, 5)
    ]
    outputs = [process.communicate(timeout=60) for process in processes]
    return_codes = [process.returncode for process in processes]
    assert return_codes == [0, 0, 0, 0], outputs

    with sqlite3.connect(db_path) as conn:
        admission_count = conn.execute("SELECT COUNT(*) FROM admissions").fetchone()[0]
        running_count = conn.execute(
            "SELECT COUNT(*) FROM experiments WHERE state='running'"
        ).fetchone()[0]
        accepted = conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE action='admit' AND result='accepted'"
        ).fetchone()[0]
    assert admission_count == 4
    assert running_count == 4
    assert accepted == 4


def test_parallel_duplicate_admission_is_idempotent(tmp_path):
    sample_dir = tmp_path / "sample"
    db_path = tmp_path / "runtime" / "same.sqlite3"
    write_simple_sample(sample_dir, count=1)
    initialize_db(sample_dir, db_path)

    processes = [
        subprocess.Popen(
            [
                sys.executable,
                str(MAIN_PY),
                "--sample-dir",
                str(sample_dir),
                "--db",
                str(db_path),
                "run",
                "--experiment",
                "PAR-01",
                "--clock",
                "1000",
            ],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(8)
    ]
    payloads = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr + stdout
        payloads.append(json.loads(stdout))

    admission_results = [payload["result"]["results"][0] for payload in payloads]
    assert sum(1 for item in admission_results if item.get("admitted")) == 1
    assert sum(1 for item in admission_results if item.get("idempotent")) >= 7

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM admissions").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM decisions WHERE action='admit' AND result='accepted'"
            ).fetchone()[0]
            == 1
        )
        balance = conn.execute(
            "SELECT balance FROM budget_accounts WHERE account_id='experiment:PAR-01:points'"
        ).fetchone()[0]
        assert balance == 9900


def test_parallel_budget_contention_never_overdraws(tmp_path):
    sample_dir = tmp_path / "sample"
    db_path = tmp_path / "runtime" / "contend.sqlite3"
    write_simple_sample(sample_dir, count=4, round_budget=100, concurrency=10)
    initialize_db(sample_dir, db_path)

    results = [
        run_subprocess_cli(
            sample_dir,
            db_path,
            "run",
            "--experiment",
            f"PAR-{index:02d}",
            "--clock",
            "1000",
        )
        for index in range(1, 5)
    ]
    assert all(result.returncode == 0 for result in results), [r.stdout + r.stderr for r in results]
    payloads = [json.loads(result.stdout) for result in results]
    constraints = [
        payload["result"]["results"][0].get("constraint_code")
        for payload in payloads
    ]
    assert constraints.count("ROUND_BUDGET_INSUFFICIENT") == 3

    db = Database(db_path).connect()
    try:
        report = verify_database(db)
        assert report["ok"] is True, report["checks"]
        round_account = db.conn.execute(
            "SELECT balance FROM budget_accounts WHERE account_id='round:default:logical_time_ms'"
        ).fetchone()
        assert round_account["balance"] == 0
    finally:
        db.close()


def test_sigkill_after_inbox_commit_replays_exactly_once(tmp_path, capsys):
    sample_dir = tmp_path / "sample_data"
    db_path = tmp_path / "runtime" / "crash-inbox.sqlite3"
    generate(sample_dir)
    assert cli.main(["--sample-dir", str(sample_dir), "--db", str(db_path), "load", "--reset"]) == 0
    assert cli.main(
        ["--sample-dir", str(sample_dir), "--db", str(db_path), "run", "--experiment", "EXP-001", "--clock", "1000"]
    ) == 0
    capsys.readouterr()

    raw_events = json.loads((sample_dir / "observations.json").read_text())["observations"]
    blocked_event = next(e for e in raw_events if e["experiment_id"] == "EXP-001" and e["sequence"] == 1)

    killed = run_subprocess_cli(
        sample_dir,
        db_path,
        "observe",
        "--event-json",
        json.dumps(blocked_event),
        crash_stage="after_inbox_commit",
    )
    assert killed.returncode == -9

    with sqlite3.connect(db_path) as conn:
        status = conn.execute(
            "SELECT status FROM observation_inbox WHERE experiment_id='EXP-001' AND sequence=1"
        ).fetchone()[0]
        assert status == "received"

    assert cli.main(["--sample-dir", str(sample_dir), "--db", str(db_path), "resume"]) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["result"]["inbox_recovery"]["replayed_count"] == 1

    duplicate = run_subprocess_cli(
        sample_dir,
        db_path,
        "observe",
        "--event-json",
        json.dumps(blocked_event),
    )
    assert duplicate.returncode == 0
    duplicate_payload = json.loads(duplicate.stdout)
    assert duplicate_payload["results"][0]["idempotent"] is True

    db = Database(db_path).connect()
    try:
        report = verify_database(db)
        assert report["ok"] is True, report["checks"]
        phase_charges = db.conn.execute(
            "SELECT COUNT(*) FROM budget_ledger WHERE reason='phase_progression_charge' AND account_id=?",
            ("experiment:EXP-001:points",),
        ).fetchone()[0]
        assert phase_charges == 1
    finally:
        db.close()


def test_sigkill_before_admission_commit_then_resume_admits_once(tmp_path, capsys):
    sample_dir = tmp_path / "sample_data"
    db_path = tmp_path / "runtime" / "crash-admit.sqlite3"
    generate(sample_dir)
    assert cli.main(["--sample-dir", str(sample_dir), "--db", str(db_path), "load", "--reset"]) == 0

    killed = run_subprocess_cli(
        sample_dir,
        db_path,
        "run",
        "--experiment",
        "EXP-005",
        "--clock",
        "1500",
        crash_stage="before_admission_commit",
    )
    assert killed.returncode == -9

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT state FROM experiments WHERE experiment_id='EXP-005'").fetchone()[0] == "pending_admission"
        assert conn.execute("SELECT COUNT(*) FROM admissions").fetchone()[0] == 0

    assert cli.main(
        ["--sample-dir", str(sample_dir), "--db", str(db_path), "resume", "--experiment", "EXP-005", "--clock", "1500"]
    ) == 0
    capsys.readouterr()

    assert cli.main(
        ["--sample-dir", str(sample_dir), "--db", str(db_path), "resume", "--experiment", "EXP-005", "--clock", "1500"]
    ) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["result"]["admission"]["idempotent"] is True

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM admissions WHERE experiment_id='EXP-005'").fetchone()[0] == 1
        assert conn.execute("SELECT state FROM experiments WHERE experiment_id='EXP-005'").fetchone()[0] == "running"

    db = Database(db_path).connect()
    try:
        assert verify_database(db)["ok"] is True
    finally:
        db.close()
