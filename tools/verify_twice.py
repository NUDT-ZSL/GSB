#!/usr/bin/env python3
"""Run fixed workloads twice and persist a byte-for-byte comparison."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"


def run_command(display_args: list[str], state_path: Path) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "shardroute",
        *display_args,
        "--state",
        str(state_path),
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return {
        "argv": display_args,
        "exit": completed.returncode,
        "stderr": completed.stderr,
        "stdout": completed.stdout,
    }


def normal_workload(run_dir: Path) -> dict[str, Any]:
    state = run_dir / "normal-state.db"
    commands: list[dict[str, Any]] = []
    commands.append(
        run_command(
            ["load", "--sample", "samples/sample.json", "--clock", "1000"],
            state,
        )
    )
    for template_id in (
        "orders_eq",
        "orders_range",
        "orders_in",
        "orders_all",
        "orders_sum_count",
        "orders_agg_range",
        "events_range",
        "events_in",
    ):
        commands.append(
            run_command(
                ["query", "--template", template_id, "--clock", "1100"],
                state,
            )
        )
    commands.append(
        run_command(
            ["query", "--template", "orders_eq", "--clock", "1150"],
            state,
        )
    )
    commands.append(
        run_command(
            [
                "apply",
                "--sample",
                "samples/sample.json",
                "--version",
                "r2",
                "--clock",
                "2000",
            ],
            state,
        )
    )
    commands.append(
        run_command(
            ["query", "--template", "orders_eq", "--clock", "2100"],
            state,
        )
    )
    commands.append(
        run_command(
            ["query", "--template", "orders_sum_count", "--clock", "2100"],
            state,
        )
    )
    commands.append(
        run_command(
            ["route", "--template", "orders_eq", "--clock", "2200"],
            state,
        )
    )
    commands.append(run_command(["status"], state))
    commands.append(run_command(["verify"], state))
    return {"commands": commands, "name": "normal", "state": "normal-state.db"}


def injection_workload(run_dir: Path) -> dict[str, Any]:
    state = run_dir / "injection-state.db"
    commands: list[dict[str, Any]] = [
        run_command(
            ["load", "--sample", "samples/sample.json", "--clock", "1000"],
            state,
        ),
        run_command(
            [
                "query",
                "--template",
                "orders_eq",
                "--clock",
                "1100",
                "--injections",
                "samples/injections.json",
            ],
            state,
        ),
        run_command(
            [
                "query",
                "--template",
                "orders_range",
                "--clock",
                "1100",
                "--injections",
                "samples/injections.json",
            ],
            state,
        ),
        run_command(
            [
                "query",
                "--template",
                "orders_all",
                "--clock",
                "1100",
                "--injections",
                "samples/injections.json",
            ],
            state,
        ),
        run_command(
            [
                "query",
                "--template",
                "orders_sum_count",
                "--clock",
                "1100",
                "--injections",
                "samples/injections.json",
            ],
            state,
        ),
        run_command(
            [
                "query",
                "--template",
                "orders_in",
                "--clock",
                "1100",
                "--injections",
                "samples/injections.json",
            ],
            state,
        ),
        run_command(["status"], state),
        run_command(["verify"], state),
    ]
    return {"commands": commands, "name": "injection", "state": "injection-state.db"}


def recovery_workload(run_dir: Path) -> dict[str, Any]:
    state = run_dir / "recovery-state.db"
    commands: list[dict[str, Any]] = [
        run_command(
            ["load", "--sample", "samples/sample.json", "--clock", "1000"],
            state,
        ),
        run_command(
            [
                "query",
                "--template",
                "orders_eq",
                "--clock",
                "1100",
                "--crash-at",
                "after_rewrite",
            ],
            state,
        ),
        run_command(
            [
                "apply",
                "--sample",
                "samples/sample.json",
                "--version",
                "r2",
                "--clock",
                "2000",
            ],
            state,
        ),
        run_command(["resume", "--clock", "2100"], state),
        run_command(["status"], state),
        run_command(["verify"], state),
    ]
    return {"commands": commands, "name": "recovery", "state": "recovery-state.db"}


def execute_workloads(run_dir: Path) -> list[dict[str, Any]]:
    return [
        normal_workload(run_dir),
        injection_workload(run_dir),
        recovery_workload(run_dir),
    ]


def stream_digest(text: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="shardroute-twice-") as temp_root:
        root = Path(temp_root)
        first_dir = root / "run-1"
        second_dir = root / "run-2"
        first_dir.mkdir()
        second_dir.mkdir()
        first = execute_workloads(first_dir)
        second = execute_workloads(second_dir)
    first_text = json.dumps(first, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    second_text = json.dumps(second, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    identical = first_text == second_text
    summary = {
        "commands_per_run": sum(len(workload["commands"]) for workload in first),
        "identical": identical,
        "run1_digest": stream_digest(first_text),
        "run1_file": "artifacts/verification-run-1.json",
        "run2_digest": stream_digest(second_text),
        "run2_file": "artifacts/verification-run-2.json",
        "workloads": [workload["name"] for workload in first],
    }
    (ARTIFACTS / "verification-run-1.json").write_text(first_text, encoding="utf-8")
    (ARTIFACTS / "verification-run-2.json").write_text(second_text, encoding="utf-8")
    (ARTIFACTS / "verification-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if identical else 1


if __name__ == "__main__":
    raise SystemExit(main())

