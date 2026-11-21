"""Requirement 8: multi-process consistency - one commit per version,
idempotent replace/resolve, readers only see complete snapshots."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from conftest import SAMPLES, run_cli


def _load(state_dir):
    result = run_cli(["load", "--state", state_dir,
                      "--v1", f"{SAMPLES}/v1_snapshot.json",
                      "--v2", f"{SAMPLES}/v2",
                      "--clock-us", "1000000000"])
    assert result.returncode == 0, result.stderr


def test_concurrent_replace_commits_once(tmp_path):
    state_dir = str(tmp_path / "state")
    _load(state_dir)

    def do_replace(_):
        return run_cli(["replace", "--state", state_dir,
                        "--v2", f"{SAMPLES}/v2_incr",
                        "--clock-us", "1000900000"])

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(do_replace, range(6)))
    assert all(r.returncode == 0 for r in results)
    outcomes = [json.loads(r.stdout) for r in results]
    committed = [o for o in outcomes if o["status"] == "committed"]
    replays = [o for o in outcomes if o["status"] == "already_committed"]
    assert len(committed) == 1
    assert len(replays) == 5
    conn = sqlite3.connect(f"{state_dir}/ledger.db")
    assert conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE version=3").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM replace_audit").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(DISTINCT op_id) FROM ledger "
        "WHERE op_id LIKE 'replace:%'").fetchone()[0] == 1
    conn.close()


def test_concurrent_resolve_is_idempotent(tmp_path, dns_server):
    state_dir = str(tmp_path / "state")
    _load(state_dir)

    def do_resolve(_):
        return run_cli(["resolve", "--state", state_dir,
                        "--name", "gamma.test", "--type", "A",
                        "--clock-us", "1000000000",
                        "--upstream-port", str(dns_server)])

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(do_resolve, range(4)))
    assert all(r.returncode == 0 for r in results)
    kinds = {json.loads(r.stdout)["kind"] for r in results}
    assert kinds == {"REFETCH"}
    conn = sqlite3.connect(f"{state_dir}/ledger.db")
    assert conn.execute(
        "SELECT COUNT(DISTINCT op_id) FROM ledger "
        "WHERE op_id LIKE 'resolve:%'").fetchone()[0] == 1
    conn.close()


def test_reader_sees_only_complete_snapshot(tmp_path):
    state_dir = str(tmp_path / "state")
    _load(state_dir)
    result = run_cli(["replace", "--state", state_dir,
                      "--v2", f"{SAMPLES}/v2_incr",
                      "--clock-us", "1000900000"])
    assert result.returncode == 0
    status = json.loads(run_cli(["status", "--state", state_dir]).stdout)
    assert status["current_snapshot_version"] == 3
    verify = run_cli(["verify", "--state", state_dir])
    assert json.loads(verify.stdout)["ok"] is True
