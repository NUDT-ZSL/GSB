#!/usr/bin/env python3
"""Run the fixed sample twice and prove that all command outputs are identical."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "fixed_sample.json"
ARTIFACTS = ROOT / "artifacts"

COMMANDS: list[tuple[str, ...]] = [
    ("load",),
    ("snapshot", "--sequence", "1"),
    ("snapshot", "--sequence", "2"),
    ("snapshot", "--sequence", "3"),
    ("rollback", "--request-id", "rb-001"),
    ("snapshot", "--sequence", "4"),
    ("rollback", "--request-id", "rb-002"),
    ("snapshot", "--sequence", "5"),
    ("rollback", "--request-id", "rb-003"),
    ("snapshot", "--sequence", "6"),
    ("gc",),
    ("status",),
    ("verify",),
]


def run_state(state: Path) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for command in COMMANDS:
        invocation = [
            sys.executable,
            "-m",
            "blockstore",
            command[0],
            "--state",
            str(state.relative_to(ROOT)),
            *command[1:],
        ]
        if command[0] == "load":
            invocation.extend(["--sample", str(SAMPLE.relative_to(ROOT))])
        result = subprocess.run(
            invocation,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"command failed: {' '.join(command)}\n{result.stdout}\n{result.stderr}"
            )
        outputs.append(
            {
                "command": list(command),
                "exit_code": result.returncode,
                "output": json.loads(result.stdout),
            }
        )
    return outputs


def main() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    state_a = ARTIFACTS / "demo_run_a"
    state_b = ARTIFACTS / "demo_run_b"
    for state in (state_a, state_b):
        shutil.rmtree(state, ignore_errors=True)
    outputs_a = run_state(state_a)
    outputs_b = run_state(state_b)
    identical = outputs_a == outputs_b
    report = {
        "ok": identical,
        "identical": identical,
        "sample": str(SAMPLE.relative_to(ROOT)),
        "steps": len(COMMANDS),
        "run_a": outputs_a,
        "run_b": outputs_b,
    }
    report_path = ARTIFACTS / "reproducibility_report.json"
    report_bytes = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode("utf-8")
    report_path.write_bytes(report_bytes)
    summary = {
        "ok": identical,
        "identical": identical,
        "steps": len(COMMANDS),
        "report": str(report_path.relative_to(ROOT)),
        "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
    }
    print(json.dumps(summary, sort_keys=True, indent=2))
    return 0 if identical else 1


if __name__ == "__main__":
    raise SystemExit(main())
