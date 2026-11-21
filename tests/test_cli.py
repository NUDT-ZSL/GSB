"""Requirement 8: diagnostic CLI - stable sorted JSON output, error codes
on stderr, status/verify subcommands."""
import json

from conftest import SAMPLES, run_cli


def _load(state_dir):
    result = run_cli(["load", "--state", state_dir,
                      "--v1", f"{SAMPLES}/v1_snapshot.json",
                      "--v2", f"{SAMPLES}/v2",
                      "--clock-us", "1000000000"])
    assert result.returncode == 0, result.stderr


def test_diagnose_stable_sorted_json(tmp_path):
    state_dir = str(tmp_path / "state")
    _load(state_dir)
    first = run_cli(["diagnose", "--state", state_dir])
    second = run_cli(["diagnose", "--state", state_dir])
    assert first.returncode == 0 and second.returncode == 0
    assert first.stdout == second.stdout  # byte-stable
    doc = json.loads(first.stdout)
    keys = [(f["domain"], f["rtype"], f["decision_kind"])
            for f in doc["findings"]]
    assert keys == sorted(keys)  # stable ordering
    assert doc["finding_count"] == 4
    for finding in doc["findings"]:
        assert "chain" in finding and "flip" in finding
        assert "conclusion" in finding["flip"]


def test_load_then_status_then_verify(tmp_path):
    state_dir = str(tmp_path / "state")
    _load(state_dir)
    status = json.loads(run_cli(["status", "--state", state_dir]).stdout)
    assert status["current_snapshot_version"] == 2
    assert status["clock_us"] == 1_000_000_000
    verify = run_cli(["verify", "--state", state_dir])
    assert verify.returncode == 0


def test_business_error_goes_to_stderr_with_exit_2(tmp_path):
    state_dir = str(tmp_path / "state")
    _load(state_dir)
    result = run_cli(["replace", "--state", state_dir,
                      "--v2", f"{SAMPLES}/v2_rollback",
                      "--clock-us", "1000000000"])
    assert result.returncode == 2
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "E3001_SNAPSHOT_VERSION_ROLLBACK"


def test_resolve_without_snapshot_is_business_error(tmp_path):
    state_dir = str(tmp_path / "state")
    result = run_cli(["resolve", "--state", state_dir,
                      "--name", "alpha.test", "--type", "A",
                      "--no-upstream"])
    assert result.returncode == 2
    assert json.loads(result.stderr)["error"]["code"] == "E9002_NOT_FOUND"


def test_warmup_end_to_end_via_cli(tmp_path, dns_server):
    state_dir = str(tmp_path / "state")
    _load(state_dir)
    result = run_cli(["warmup", "--state", state_dir,
                      "--queries", f"{SAMPLES}/queries.json",
                      "--clock-table", f"{SAMPLES}/clock_steps.json",
                      "--upstream-port", str(dns_server)])
    assert result.returncode == 0, result.stderr
    kinds = [r["kind"] for r in json.loads(result.stdout)["results"]]
    assert kinds == ["REFETCH", "HIT", "MISS_FETCH", "NXDOMAIN", "NODATA",
                     "NEGATIVE_HIT_NXDOMAIN", "HIT"]
