from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from conftest import ROOT, SAMPLES, remove_db, run_cli_json


def _spawn(args: list[str]) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    return subprocess.Popen(
        [sys.executable, "-m", "ic_ledger", *args, "--compact"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_parallel_queues_and_duplicate_same_queue_are_idempotent(
    tmp_path: Path,
) -> None:
    db = tmp_path / "concurrent.sqlite"
    remove_db(db)
    run_cli_json(["load", str(SAMPLES / "sample.json"), "--db", str(db)])

    processes = [
        _spawn(["run", "--db", str(db), "--queue", "eth0:0"]),
        _spawn(["run", "--db", str(db), "--queue", "eth0:1"]),
        _spawn(["run", "--db", str(db), "--queue", "eth0:0"]),
    ]
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr
        outputs.append(stdout)

    _, status = run_cli_json(["status", "--db", str(db)])
    q0 = next(item for item in status["queues"] if item["iface"] == "eth0" and item["queue"] == 0)
    q1 = next(item for item in status["queues"] if item["iface"] == "eth0" and item["queue"] == 1)
    assert q0["committed_records"] == 30
    assert q0["record_watermark"] == 1730
    assert q0["fixed_interrupts"] == 9
    assert q1["committed_records"] == 20
    assert q1["fixed_interrupts"] == 6

    code, rerun = run_cli_json(["run", "--db", str(db), "--queue", "eth0:0"])
    assert code == 0
    assert rerun["idempotent"] == 30
    code, rerun = run_cli_json(["run", "--db", str(db), "--queue", "eth0:1"])
    assert code == 0
    assert rerun["idempotent"] == 20
    code, verified = run_cli_json(["verify", "--db", str(db), "--queue", "eth0:0"])
    assert code == 0
    assert verified["ok"] is True
    assert verified["checked_queues"] == 1

