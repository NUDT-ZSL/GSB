from __future__ import annotations

import json
import sqlite3
import subprocess

from tests.conftest import PROJECT_ROOT, PYTHON, subprocess_env
from health_machine.cli import main
from health_machine.status import build_status
from health_machine.store import Store
from health_machine.verify import verify_store


def backup(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def run_cli_subprocess(*args, fault=None, fault_mode=None):
    env = subprocess_env()
    if fault:
        env["HSM_FAULT"] = fault
    if fault_mode:
        env["HSM_FAULT_MODE"] = fault_mode
    return subprocess.run(
        [PYTHON, "-m", "health_machine", *args],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )


def comparable_status(db, instance_id):
    status = build_status(Store(db), instance_id)["instances"][0]
    return {
        key: status[key]
        for key in [
            "state", "failure_streak", "recovery_streak", "recovery_duration_ms",
            "last_committed_window_index", "logical_clock_watermark_ms",
            "latest_window_index", "latest_window_score_bp", "latest_scores_by_level",
            "committed_seqs", "transition_count", "transition_chain_head", "state_version", "state_hash",
        ]
    }


def test_sigkill_before_window_commit_replays_suffix(workspace):
    baseline = workspace.root / "baseline.sqlite"
    killed = workspace.root / "sigkill.sqlite"
    backup(workspace.db, baseline)
    backup(workspace.db, killed)
    assert main(["probe", "--db", str(baseline)]) == 0

    result = run_cli_subprocess(
        "probe", "--db", str(killed), "--instance", "inst-outage",
        fault="before_window_commit", fault_mode="sigkill",
    )
    assert result.returncode == -9, result.stderr + result.stdout

    conn = sqlite3.connect(killed)
    assert conn.execute("SELECT COUNT(*) FROM window_summaries").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM observation_inbox WHERE instance_id='inst-outage' AND status='received'"
    ).fetchone()[0] == 30
    conn.close()

    resume = run_cli_subprocess("resume", "--db", str(killed), "--instance", "inst-outage")
    assert resume.returncode == 0, resume.stdout + resume.stderr
    payload = json.loads(resume.stdout)
    assert payload["recovery_before"]["half_committed_received_observations"] == 30
    assert payload["verification"]["ok"] is True

    assert verify_store(Store(killed), "inst-outage")["ok"]
    assert comparable_status(killed, "inst-outage") == comparable_status(baseline, "inst-outage")


def test_sigterm_is_handleable_and_atomic(workspace):
    term_db = workspace.root / "sigterm.sqlite"
    backup(workspace.db, term_db)
    result = run_cli_subprocess(
        "probe", "--db", str(term_db), "--instance", "inst-flap",
        fault="before_window_commit", fault_mode="sigterm",
    )
    assert result.returncode == 130, result.stdout + result.stderr
    conn = sqlite3.connect(term_db)
    assert conn.execute("SELECT COUNT(*) FROM window_summaries WHERE instance_id='inst-flap'").fetchone()[0] == 0
    conn.close()

    resume = run_cli_subprocess("resume", "--db", str(term_db), "--instance", "inst-flap")
    assert resume.returncode == 0, resume.stdout + resume.stderr
    assert json.loads(resume.stdout)["verification"]["ok"] is True


def test_sigkill_during_load_and_merge_is_recoverable(workspace, tmp_path):
    kill_load_db = tmp_path / "kill-load.sqlite"
    loaded = run_cli_subprocess(
        "load", "--samples", str(workspace.samples), "--db", str(kill_load_db),
        fault="after_inbox_commit", fault_mode="sigkill",
    )
    assert loaded.returncode == -9
    conn = sqlite3.connect(kill_load_db)
    assert conn.execute("SELECT COUNT(*) FROM observation_inbox").fetchone()[0] == 75
    conn.close()

    merged = run_cli_subprocess("merge", "--samples", str(workspace.samples), "--db", str(kill_load_db),
                                "--output", str(tmp_path / "merge-out.json"))
    assert merged.returncode == 0, merged.stdout + merged.stderr
    probed = run_cli_subprocess("probe", "--db", str(kill_load_db))
    assert probed.returncode == 0, probed.stdout + probed.stderr
    assert verify_store(Store(kill_load_db))["ok"]

    kill_merge_db = tmp_path / "kill-merge.sqlite"
    assert main(["load", "--samples", str(workspace.samples), "--db", str(kill_merge_db)]) == 0
    killed_merge = run_cli_subprocess(
        "merge", "--samples", str(workspace.samples), "--db", str(kill_merge_db),
        "--output", str(tmp_path / "bad-merge.json"),
        fault="before_merge_commit", fault_mode="sigkill",
    )
    assert killed_merge.returncode == -9
    conn = sqlite3.connect(kill_merge_db)
    assert conn.execute("SELECT COUNT(*) FROM snapshot_versions").fetchone()[0] == 0
    conn.close()

    merged = run_cli_subprocess("merge", "--samples", str(workspace.samples), "--db", str(kill_merge_db),
                                "--output", str(tmp_path / "merge-out2.json"))
    assert merged.returncode == 0
    assert main(["probe", "--db", str(kill_merge_db)]) == 0
    assert verify_store(Store(kill_merge_db))["ok"]
