from __future__ import annotations

import json
from pathlib import Path

from .conftest import run_cli


COMMANDS = [
    ("load", "--sample", "samples/sample.json", "--clock", "1000"),
    ("route", "--template", "orders_range", "--clock", "1100"),
    ("route", "--template", "events_range", "--clock", "1100"),
    ("query", "--template", "orders_eq", "--clock", "1100"),
    ("query", "--template", "orders_range", "--clock", "1100"),
    ("query", "--template", "orders_in", "--clock", "1100"),
    ("query", "--template", "orders_all", "--clock", "1100"),
    ("query", "--template", "orders_sum_count", "--clock", "1100"),
    ("query", "--template", "orders_agg_range", "--clock", "1100"),
    ("query", "--template", "events_range", "--clock", "1100"),
    ("query", "--template", "orders_eq", "--clock", "1150"),
    ("apply", "--sample", "samples/sample.json", "--version", "r2", "--clock", "2000"),
    ("query", "--template", "orders_eq", "--clock", "2100"),
    ("query", "--template", "orders_sum_count", "--clock", "2100"),
    ("status",),
    ("verify",),
]


def _run_sequence(state_path: Path) -> list[dict]:
    outputs = []
    for command in COMMANDS:
        completed = run_cli(*command, state_path=state_path)
        outputs.append(
            {
                "argv": list(command),
                "exit": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        )
    return outputs


def test_two_fresh_runs_are_byte_identical(tmp_path: Path):
    first = _run_sequence(tmp_path / "run-1.db")
    second = _run_sequence(tmp_path / "run-2.db")
    assert first == second
    for item in first:
        assert item["exit"] == 0
        assert item["stderr"] == ""
        json.loads(item["stdout"])
    verify_output = json.loads(first[-1]["stdout"])
    assert verify_output["ok"] is True
    assert verify_output["issues"] == []

