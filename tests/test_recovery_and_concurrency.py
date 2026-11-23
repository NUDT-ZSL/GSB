from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from diagnostic_workbench.engine import Workbench
from diagnostic_workbench.errors import BusinessError
from diagnostic_workbench.util import canonical_json_text

from .conftest import ROOT


def _spawn(
    module: str,
    state_dir: Path,
    round_id: int,
    clock: int,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    process_env = os.environ.copy()
    process_env["PYTHONPATH"] = str(ROOT)
    if env:
        process_env.update(env)
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            module,
            str(state_dir),
            str(round_id),
            str(clock),
        ],
        cwd=ROOT,
        env=process_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_forced_termination_replays_only_unfinished_domains(
    state_dir: Path,
    sample: dict,
) -> None:
    workbench = Workbench(state_dir)
    workbench.load({"sample": copy.deepcopy(sample)})

    worker = _spawn(
        "tests.crash_worker",
        state_dir,
        1,
        130,
        env={"WORKBENCH_CRASH_AFTER_DOMAIN_COMMITS": "1"},
    )
    stdout, stderr = worker.communicate(timeout=60)
    assert worker.returncode == 77, (stdout, stderr)

    interrupted = Workbench(state_dir)
    status = interrupted.status()
    assert status["pointer_version"] == 1
    assert len(status["prepared_rounds"]) == 1
    prepared = status["prepared_rounds"][0]
    assert prepared["round_id"] == 1
    assert len(prepared["completed_domains"]) == 1
    assert interrupted.ledger(1)["count"] == 0

    with pytest.raises(BusinessError) as prepared_error:
        interrupted.switch({"round_id": 2, "logical_time_ms": 900})
    assert prepared_error.value.code == "PREPARED_ROUND_EXISTS"
    assert prepared_error.value.context["prepared_round_id"] == 1

    resumed = interrupted.resume()
    assert resumed["status"] == "ok"
    assert len(resumed["results"]) == 1
    assert resumed["results"][0]["status"] == "committed"
    assert interrupted.status()["pointer_version"] == 2
    assert interrupted.ledger(1)["count"] == 4
    services = [item["service"] for item in interrupted.ledger(1)["entries"]]
    assert len(services) == len(set(services)) == 4

    second_resume = interrupted.resume()
    assert second_resume["resumed_rounds"] == []
    assert interrupted.ledger(1)["count"] == 4
    current_file = state_dir / "snapshots" / "CURRENT"
    assert current_file.read_text(encoding="ascii").strip() == "0000000002"


def test_multiple_processes_commit_same_round_once(
    state_dir: Path,
    sample: dict,
) -> None:
    workbench = Workbench(state_dir)
    workbench.load({"sample": copy.deepcopy(sample)})

    workers = [
        _spawn("tests.concurrency_worker", state_dir, 1, 130)
        for _ in range(4)
    ]
    outputs: list[dict] = []
    for worker in workers:
        stdout, stderr = worker.communicate(timeout=90)
        assert worker.returncode == 0, (stdout, stderr)
        outputs.append(json.loads(stdout))

    assert all(item["status"] == "committed" for item in outputs)
    assert len({canonical_json_text(item) for item in outputs}) == 1
    assert workbench.ledger(1)["count"] == 4
    entries = workbench.ledger(1)["entries"]
    assert len({(item["round_id"], item["service"]) for item in entries}) == 4
    assert workbench.status()["round_watermark"] == 1
    assert workbench.status()["pointer_version"] == 2

