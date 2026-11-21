"""Requirement 7: crash mid-operation, resume replays only the unfinished
steps, and the precision ledger is identical to an uninterrupted run."""
import json
import sqlite3

from conftest import SAMPLES, run_cli


def _ledger_rows(state_dir):
    conn = sqlite3.connect(f"{state_dir}/ledger.db")
    rows = conn.execute(
        "SELECT seq,op_id,kind,domain,rtype,level,detail_json "
        "FROM ledger ORDER BY seq").fetchall()
    conn.close()
    return [list(r) for r in rows]


def _load(state_dir):
    result = run_cli(["load", "--state", state_dir,
                      "--v1", f"{SAMPLES}/v1_snapshot.json",
                      "--v2", f"{SAMPLES}/v2",
                      "--clock-us", "1000000000"])
    assert result.returncode == 0, result.stderr


def _warmup(state_dir, port, env_extra=None):
    return run_cli(["warmup", "--state", state_dir,
                    "--queries", f"{SAMPLES}/queries.json",
                    "--clock-table", f"{SAMPLES}/clock_steps.json",
                    "--upstream-port", str(port)],
                   env_extra=env_extra)


def test_resume_after_crash_reproduces_ledger(tmp_path, dns_server):
    ref_dir = str(tmp_path / "ref")
    crashed_dir = str(tmp_path / "crashed")
    _load(ref_dir)
    _load(crashed_dir)
    ref = _warmup(ref_dir, dns_server)
    assert ref.returncode == 0, ref.stderr
    # force-kill the warmup after step 2 of 7
    crashed = _warmup(crashed_dir, dns_server,
                      env_extra={"DNSCACHE_CRASH_AFTER_STEP": "2"})
    assert crashed.returncode == 3
    # the interrupted operation is visible as pending
    status = run_cli(["status", "--state", crashed_dir])
    pending = json.loads(status.stdout)["pending_ops"]
    assert len(pending) == 1 and pending[0].startswith("warmup:")
    # ledger so far only contains the load + the first 3 committed steps
    partial_rows = _ledger_rows(crashed_dir)
    assert len(partial_rows) < len(_ledger_rows(ref_dir))
    # resume replays only the unfinished suffix
    resumed = run_cli(["resume", "--state", crashed_dir,
                       "--upstream-port", str(dns_server)])
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout)["replayed"] == pending
    # ledger (sequence numbers, error bounds, all details) is identical
    assert _ledger_rows(crashed_dir) == _ledger_rows(ref_dir)
    # findings are identical too
    diag_ref = run_cli(["diagnose", "--state", ref_dir])
    diag_new = run_cli(["diagnose", "--state", crashed_dir])
    assert json.loads(diag_ref.stdout)["findings"] == \
        json.loads(diag_new.stdout)["findings"]
    # nothing left pending; verify passes
    verify = run_cli(["verify", "--state", crashed_dir])
    assert verify.returncode == 0
    assert json.loads(verify.stdout)["ok"] is True


def test_resume_is_idempotent(tmp_path, dns_server):
    state_dir = str(tmp_path / "state")
    _load(state_dir)
    _warmup(state_dir, dns_server,
            env_extra={"DNSCACHE_CRASH_AFTER_STEP": "1"})
    first = run_cli(["resume", "--state", state_dir,
                     "--upstream-port", str(dns_server)])
    assert first.returncode == 0
    rows_after_first = _ledger_rows(state_dir)
    second = run_cli(["resume", "--state", state_dir,
                      "--upstream-port", str(dns_server)])
    assert json.loads(second.stdout)["replayed"] == []
    assert _ledger_rows(state_dir) == rows_after_first
