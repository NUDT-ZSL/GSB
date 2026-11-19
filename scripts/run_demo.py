#!/usr/bin/env python3
"""Run the deterministic offline demonstration twice and compare output bytes."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / ".demo" / "state"
OUTPUT_DIR = ROOT / "demo_output"
NOW = "1760000200000"


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def command_steps() -> list[tuple[str, list[str], int]]:
    common = [sys.executable, "-m", "cacheman", "--state-dir", ".demo/state"]
    initial_sources = [
        "--v1",
        "samples/v1/cache.json",
        "--v2",
        "samples/v2/meta.sqlite3",
    ]
    pipelines = [
        "--pipeline",
        "samples/pipelines/app-ci.json",
        "--pipeline",
        "samples/pipelines/docs-ci.json",
    ]
    return [
        (
            "load",
            common
            + ["load", *initial_sources, "--now-ms", NOW],
            0,
        ),
        (
            "interrupted_merge",
            common
            + [
                "merge",
                *initial_sources,
                "--now-ms",
                NOW,
                "--fault-point",
                "after_stage",
            ],
            75,
        ),
        ("status_before_resume", common + ["status"], 0),
        ("resume_interrupted_merge", common + ["resume"], 0),
        ("status_after_merge", common + ["status"], 0),
        (
            "judge_before_replacement",
            common + ["judge", *pipelines, "--now-ms", NOW],
            0,
        ),
        (
            "verify_before_replacement",
            common
            + [
                "verify",
                "--v1",
                "samples/v1/cache.json",
                "--v2",
                "samples/v2/meta.sqlite3",
                "--pipeline",
                "samples/pipelines/app-ci.json",
            ],
            0,
        ),
        (
            "replace_current_noop",
            common
            + [
                "replace",
                "--package",
                "samples/packages/current/package.json",
                *pipelines,
                "--now-ms",
                NOW,
            ],
            0,
        ),
        (
            "replace_full",
            common
            + [
                "replace",
                "--package",
                "samples/packages/replacement/package.json",
                *pipelines,
                "--now-ms",
                NOW,
            ],
            0,
        ),
        (
            "judge_after_replacement",
            common + ["judge", *pipelines, "--now-ms", NOW],
            0,
        ),
        (
            "verify_after_replacement",
            common
            + [
                "verify",
                "--v1",
                "samples/v1/cache.json",
                "--v2",
                "samples/v2-replacement/meta.sqlite3",
                "--pipeline",
                "samples/pipelines/app-ci.json",
            ],
            0,
        ),
        ("status_final", common + ["status"], 0),
        ("resume_no_pending", common + ["resume"], 0),
    ]


def run_once() -> bytes:
    shutil.rmtree(ROOT / ".demo", ignore_errors=True)
    steps = []
    for name, argv, expected_code in command_steps():
        completed = subprocess.run(
            argv,
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
        stdout = completed.stdout.decode("utf-8")
        stderr = completed.stderr.decode("utf-8")
        if completed.returncode != expected_code:
            raise RuntimeError(
                f"step {name} returned {completed.returncode}, expected {expected_code}: "
                f"{stdout}{stderr}"
            )
        parsed_output: Any = None
        if stdout.strip():
            try:
                parsed_output = json.loads(stdout)
            except json.JSONDecodeError:
                parsed_output = {"non_json_stdout": stdout}
        steps.append(
            {
                "name": name,
                "argv": argv[3:],
                "exit_code": completed.returncode,
                "output": parsed_output,
                "stderr": stderr,
            }
        )
    aggregate = {
        "format": "cacheman-demo-output-v1",
        "logical_clock_ms": int(NOW),
        "steps": steps,
    }
    return pretty_json(aggregate).encode("utf-8")


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    first = run_once()
    second = run_once()
    run1 = OUTPUT_DIR / "run1.json"
    run2 = OUTPUT_DIR / "run2.json"
    run1.write_bytes(first)
    run2.write_bytes(second)
    identical = first == second
    digest = hashlib.sha256(first).hexdigest()
    summary = {
        "ok": identical,
        "run1": run1.relative_to(ROOT).as_posix(),
        "run2": run2.relative_to(ROOT).as_posix(),
        "byte_identical": identical,
        "sha256": digest,
        "size_bytes": len(first),
    }
    (OUTPUT_DIR / "comparison.json").write_text(pretty_json(summary), encoding="utf-8")
    sys.stdout.write(pretty_json(summary))
    return 0 if identical else 1


if __name__ == "__main__":
    raise SystemExit(main())
