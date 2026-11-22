from __future__ import annotations

import copy
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ckptlab.manager import CheckpointManager


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def read_json(name: str) -> dict[str, Any]:
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


def plan_v1(**save_overrides: Any) -> dict[str, Any]:
    plan = copy.deepcopy(read_json("training_plan.json"))
    plan["save"].update(save_overrides)
    return plan


def plan_v2(**save_overrides: Any) -> dict[str, Any]:
    plan = copy.deepcopy(read_json("training_plan_v2.json"))
    plan["save"].update(save_overrides)
    return plan


def shard_policy(**overrides: Any) -> dict[str, Any]:
    policy = copy.deepcopy(read_json("shard_policy.json"))
    policy.update(overrides)
    return policy


def crash_plan(**overrides: Any) -> dict[str, Any]:
    plan = copy.deepcopy(read_json("crash_injection.json"))
    plan.update(overrides)
    return plan


def manager_for(root: Path) -> CheckpointManager:
    return CheckpointManager(root / "ledger.sqlite3", root / "store")


def db_rows(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def run_cli(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "ckptlab.cli", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
        check=False,
    )


@pytest.fixture
def sample_inputs() -> tuple[dict[str, Any], dict[str, Any]]:
    return plan_v1(), shard_policy()
