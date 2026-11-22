from __future__ import annotations

import json
import os
import subprocess
import sys

from conftest import ROOT, db_rows, manager_for, plan_v1, shard_policy


def test_two_processes_same_version_commit_once_idempotently(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    command = [
        sys.executable,
        "-m",
        "ckptlab.cli",
        "save",
        "--plan",
        str(ROOT / "samples" / "training_plan.json"),
        "--shards",
        str(ROOT / "samples" / "shard_policy.json"),
        "--db",
        str(tmp_path / "ledger.sqlite3"),
        "--store",
        str(tmp_path / "store"),
    ]
    first = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out_first, err_first = first.communicate(timeout=30)
    out_second, err_second = second.communicate(timeout=30)
    assert first.returncode == 0, err_first
    assert second.returncode == 0, err_second
    first_json = json.loads(out_first)
    second_json = json.loads(out_second)
    assert sorted(
        [first_json["idempotent"], second_json["idempotent"]]
    ) == [False, True]
    assert first_json["manifest_digest"] == second_json["manifest_digest"]

    builds = db_rows(tmp_path / "ledger.sqlite3", "SELECT * FROM builds")
    pointer = db_rows(tmp_path / "ledger.sqlite3", "SELECT * FROM pointer")
    commits = db_rows(tmp_path / "ledger.sqlite3", "SELECT * FROM shard_commits")
    assert len(builds) == 1
    assert pointer[0]["version"] == 1
    assert len(commits) == 4
    assert len(list((tmp_path / "store" / "objects").glob("*.shard"))) == 4

    manager = manager_for(tmp_path)
    assert manager.verify()["verified"] is True
    assert manager.restore()["restored"] is True
