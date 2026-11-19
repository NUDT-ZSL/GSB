from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "fixed_sample.json"


def run_cli(
    state: Path,
    *args: str,
    crash_at: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("BLOCKSTORE_CRASH_AT", None)
    if crash_at is not None:
        env["BLOCKSTORE_CRASH_AT"] = crash_at
    command = [
        sys.executable,
        "-m",
        "blockstore",
        args[0],
        "--state",
        str(state),
        *args[1:],
    ]
    if args[0] == "load" and "--sample" not in args:
        command.extend(["--sample", str(SAMPLE)])
    return subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


def run_cli_json(
    state: Path,
    *args: str,
    crash_at: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    result = run_cli(state, *args, crash_at=crash_at)
    payload = json.loads(result.stdout) if result.stdout.strip() else None
    return result, payload


def setup_standard_state(state: Path) -> None:
    result, payload = run_cli_json(state, "load")
    assert result.returncode == 0, result.stdout + result.stderr
    assert payload and payload["ok"]
    for sequence in (1, 2, 3):
        result, payload = run_cli_json(state, "snapshot", "--sequence", str(sequence))
        assert result.returncode == 0, result.stdout + result.stderr
        assert payload and payload["ok"]


def setup_full_state(state: Path, *, gc: bool = False) -> list[dict[str, Any]]:
    commands = [
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
    ]
    if gc:
        commands.append(("gc",))
    outputs = []
    for command in commands:
        result, payload = run_cli_json(state, *command)
        assert result.returncode == 0, result.stdout + result.stderr
        assert payload and payload["ok"]
        outputs.append(payload)
    return outputs


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
