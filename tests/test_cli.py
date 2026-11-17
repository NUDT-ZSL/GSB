"""End-to-end CLI tests: exit codes, reproducibility, input immutability."""

import hashlib
import json

from policy_validator import errors
from policy_validator.cli import run

from sample_gen import write_scenario


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_scenario(tmp_path, name, extra=None, concurrency=2):
    scenario = write_scenario(tmp_path / "samples", name)
    workdir = tmp_path / f"out-{name}-{concurrency}"
    argv = [
        "--policies", str(scenario / "policies.json"),
        "--tasks", str(scenario / "tasks.json"),
        "--workdir", str(workdir),
        "--concurrency", str(concurrency),
        "--batch-id", "test-batch",
    ] + list(extra or [])
    code = run(argv)
    report = json.loads((workdir / "report.json").read_text())
    return code, report, scenario


def test_clean_scenario_exit_zero(tmp_path):
    code, report, _ = _run_scenario(tmp_path, "clean")
    assert code == errors.EXIT_OK
    assert report["findings"] == {
        "conflicts": [], "shadows": [], "dependency_cycles": []}
    assert report["summary"]["succeeded"] == report["summary"]["total"]


def test_conflict_and_shadow_exit_findings(tmp_path):
    code, report, _ = _run_scenario(tmp_path, "conflict")
    assert code & errors.EXIT_FINDINGS
    assert report["findings"]["conflicts"][0]["code"] == errors.E_CONFLICT

    code, report, _ = _run_scenario(tmp_path, "shadow")
    assert code & errors.EXIT_FINDINGS
    assert report["findings"]["shadows"][0]["code"] == errors.E_SHADOW


def test_cycle_exit_findings(tmp_path):
    code, report, _ = _run_scenario(tmp_path, "cycle")
    assert code & errors.EXIT_FINDINGS
    cycles = report["findings"]["dependency_cycles"]
    assert cycles[0]["code"] == errors.E_DEPENDENCY_CYCLE
    assert cycles[0]["cycle"] == ["R-301", "R-302", "R-303"]


def test_invalid_entries_exit_rejections(tmp_path):
    code, report, _ = _run_scenario(tmp_path, "invalid")
    assert code & errors.EXIT_REJECTIONS
    assert len(report["rejections"]) == 6
    indexes = [r["index"] for r in report["rejections"]]
    assert indexes == sorted(indexes)


def test_timeout_exit_resource_events(tmp_path):
    code, report, _ = _run_scenario(
        tmp_path, "timeout", extra=["--task-timeout", "0.2"])
    assert code & errors.EXIT_RESOURCE_EVENTS
    timed_out = [t for t in report["tasks"] if t["status"] == "timeout"]
    assert timed_out and timed_out[0]["code"] == errors.E_TASK_TIMEOUT


def test_memory_exit_resource_events(tmp_path):
    code, report, _ = _run_scenario(
        tmp_path, "memory",
        extra=["--task-memory-limit", str(8 * 1024 * 1024),
               "--batch-memory-budget", str(1 << 40)])
    assert code & errors.EXIT_RESOURCE_EVENTS
    exceeded = [t for t in report["tasks"] if t["status"] == "memory_exceeded"]
    assert exceeded and exceeded[0]["code"] == errors.E_TASK_MEMORY_EXCEEDED


def test_concurrency_reproducibility(tmp_path):
    _, report1, _ = _run_scenario(tmp_path, "concurrency", concurrency=1)
    _, report4, _ = _run_scenario(tmp_path, "concurrency", concurrency=4)
    assert report1["findings"] == report4["findings"]
    assert [t["task_id"] for t in report1["tasks"]] == \
           [t["task_id"] for t in report4["tasks"]]
    assert report1["summary"] == report4["summary"]


def test_input_files_are_not_modified(tmp_path):
    scenario = write_scenario(tmp_path / "samples", "conflict")
    policies = scenario / "policies.json"
    tasks = scenario / "tasks.json"
    before = (_hash(policies), _hash(tasks))
    run(["--policies", str(policies), "--tasks", str(tasks),
         "--workdir", str(tmp_path / "out")])
    assert (_hash(policies), _hash(tasks)) == before


def test_lock_conflict_exit_code(tmp_path):
    scenario = write_scenario(tmp_path / "samples", "clean")
    workdir = tmp_path / "out-lock"
    workdir.mkdir()
    (workdir / "report.json.lock").write_text("held")
    code = run(["--policies", str(scenario / "policies.json"),
                "--tasks", str(scenario / "tasks.json"),
                "--workdir", str(workdir)])
    assert code == errors.EXIT_LOCK_CONFLICT
    assert not (workdir / "report.json").exists()
