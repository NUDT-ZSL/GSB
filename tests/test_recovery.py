from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from lbworkbench.db import WorkbenchDB
from lbworkbench.engine import run_check
from lbworkbench.probes import ProbeHTTPServer
from lbworkbench.store import get_status, list_rounds, load_package, verify_state
from lbworkbench.util import canonical_json

from conftest import ROOT


def _run_cli(
    workdir: Path,
    args: list[str],
    *,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "lbworkbench", "--workdir", str(workdir), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )


def _uninterrupted_status(
    workdir: Path,
    package: Path,
    plan: Path,
    server: ProbeHTTPServer,
) -> dict:
    db = WorkbenchDB(workdir)
    load_package(db, package)
    assert run_check(db, plan_path=str(plan), probe_base_url=server.base_url)["ok"]
    return get_status(db)


def test_resume_after_mid_round_crash_matches_uninterrupted_run(
    sample_copy: Path,
    check_plan: Path,
    probe_server: ProbeHTTPServer,
    tmp_path: Path,
) -> None:
    interrupted = tmp_path / "interrupted"
    reference = tmp_path / "reference"
    loaded = _run_cli(
        interrupted,
        ["load", "--package", str(sample_copy / "package-2026.11.1.json")],
    )
    assert loaded.returncode == 0, loaded.stderr
    crashed = _run_cli(
        interrupted,
        [
            "check",
            "--plan",
            str(check_plan),
            "--probe-base-url",
            probe_server.base_url,
        ],
        env_extra={"LB_CRASH_AFTER_OBSERVATIONS": "10"},
    )
    assert crashed.returncode == 97, (crashed.stdout, crashed.stderr)

    rounds_before = list_rounds(WorkbenchDB(interrupted))["rounds"]
    assert [item["round_index"] for item in rounds_before if item["status"] == "started"] == [2]
    assert rounds_before[0]["status"] == "committed"
    assert len(rounds_before[1]["missing_instance_ids"]) == 4
    state_before = get_status(WorkbenchDB(interrupted))
    status_by_id = {item["instance_id"]: item for item in state_before["instances"]}
    assert status_by_id["api-01"]["revision"] == 1
    assert status_by_id["api-02"]["revision"] == 1
    assert state_before["committed_rounds"]["count"] == 1

    resumed = _run_cli(
        interrupted,
        ["resume", "--probe-base-url", probe_server.base_url],
    )
    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    resume_json = json.loads(resumed.stdout)
    assert resume_json["ok"] is True
    assert [item["round_index"] for item in resume_json["rounds"]] == [2, 3]
    assert resume_json["rounds"][0]["previously_missing_instance_ids"]

    expected = _uninterrupted_status(
        reference,
        sample_copy / "package-2026.11.1.json",
        check_plan,
        probe_server,
    )
    actual = get_status(WorkbenchDB(interrupted))
    assert canonical_json(actual) == canonical_json(expected)
    assert verify_state(WorkbenchDB(interrupted))["ok"] is True


def test_resume_after_finalize_crash_uses_persisted_observations(
    sample_copy: Path,
    check_plan: Path,
    probe_server: ProbeHTTPServer,
    tmp_path: Path,
) -> None:
    interrupted = tmp_path / "interrupted"
    _run_cli(
        interrupted,
        ["load", "--package", str(sample_copy / "package-2026.11.1.json")],
    )
    crashed = _run_cli(
        interrupted,
        [
            "check",
            "--plan",
            str(check_plan),
            "--probe-base-url",
            probe_server.base_url,
            "--round",
            "1",
        ],
        env_extra={"LB_CRASH_DURING_FINALIZE": "1"},
    )
    assert crashed.returncode == 99
    rounds = list_rounds(WorkbenchDB(interrupted))["rounds"]
    assert rounds[0]["status"] == "started"
    assert rounds[0]["missing_instance_ids"] == []
    before_revisions = {
        item["instance_id"]: item["revision"]
        for item in get_status(WorkbenchDB(interrupted))["instances"]
    }
    assert set(before_revisions.values()) == {0}

    resumed = _run_cli(
        interrupted,
        ["resume", "--probe-base-url", probe_server.base_url],
    )
    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    report = json.loads(resumed.stdout)
    assert report["rounds"][0]["round_index"] == 1
    assert report["rounds"][0]["executed_instance_ids"] == []
    assert report["rounds"][0]["finalized"] is True
    after = get_status(WorkbenchDB(interrupted))
    assert all(item["revision"] == 3 for item in after["instances"] if not item["manual"])


def test_crash_during_snapshot_load_leaves_no_snapshot(
    sample_copy: Path,
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    crashed = _run_cli(
        work,
        ["load", "--package", str(sample_copy / "package-2026.11.1.json")],
        env_extra={"LB_CRASH_DURING_LOAD": "1"},
    )
    assert crashed.returncode == 98
    status = get_status(WorkbenchDB(work))
    assert status["current_snapshot"] is None
    assert status["instances"] == []

    loaded = _run_cli(
        work,
        ["load", "--package", str(sample_copy / "package-2026.11.1.json")],
    )
    assert loaded.returncode == 0
    assert get_status(WorkbenchDB(work))["current_snapshot"]["version"] == "2026.11.1"
