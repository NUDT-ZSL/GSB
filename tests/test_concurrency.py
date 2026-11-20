from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from conftest import ROOT, query


def test_two_executors_commit_each_shard_once(tmp_path: Path, sample_dir) -> None:
    db = tmp_path / "shared.db"
    work = tmp_path / "shared-var"
    command = [
        sys.executable,
        "-m",
        "columnar_engine.cli",
        "run",
        "--sample",
        str(sample_dir),
        "--db",
        str(db),
        "--work-dir",
        str(work),
    ]
    processes = [
        subprocess.Popen(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr
        outputs.append(json.loads(stdout)["summary"])
    assert all(output["status"] == "COMPLETE" for output in outputs)
    committed_sets = [set(output["scheduler"]["committed_this_call"]) for output in outputs]
    assert committed_sets[0].isdisjoint(committed_sets[1])
    assert committed_sets[0] | committed_sets[1] == set(range(6))
    assert query(db, "SELECT COUNT(*) FROM shards WHERE status='COMMITTED'")[0][0] == 6
    assert query(db, "SELECT COUNT(*) FROM commits")[0][0] == 12
    assert query(db, "SELECT COUNT(*) FROM node_states")[0][0] == 2
    assert query(db, "SELECT COUNT(*) FROM leases")[0][0] == 0

