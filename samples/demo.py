#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def reset(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def invoke(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "pipeline.py"), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=os.environ.copy(),
    )


def main() -> int:
    state_base = ROOT / "var" / "demo"
    reset(state_base)
    reset(ROOT / "artifacts" / "demo")
    reset(ROOT / "artifacts" / "security")

    outputs: list[str] = []
    for number in (1, 2):
        result = invoke(
            "run",
            "--workspace",
            str(ROOT),
            "--manifest",
            "samples/manifest.json",
            "--state",
            f"var/demo/run{number}/state.sqlite3",
            "--cache",
            f"var/demo/run{number}/cache",
        )
        if result.returncode != 0:
            sys.stderr.write(result.stdout + result.stderr)
            return result.returncode
        outputs.append(result.stdout)
    if outputs[0] != outputs[1]:
        sys.stderr.write("two fresh runs produced different output\n")
        return 1

    verify = invoke(
        "verify",
        "--workspace",
        str(ROOT),
        "--manifest",
        "samples/manifest.json",
        "--state",
        "var/demo/run2/state.sqlite3",
        "--cache",
        "var/demo/run2/cache",
    )
    if verify.returncode != 0 or not json.loads(verify.stdout)["valid"]:
        sys.stderr.write(verify.stdout + verify.stderr)
        return 1

    security_results = []
    for name, expected in (
        ("security_read_manifest.json", "E_SANDBOX_READ_DENIED"),
        ("security_write_manifest.json", "E_SANDBOX_WRITE_DENIED"),
    ):
        result = invoke(
            "run",
            "--workspace",
            str(ROOT),
            "--manifest",
            f"samples/{name}",
            "--state",
            f"var/demo/{name}.sqlite3",
            "--cache",
            f"var/demo/{name}.cache",
        )
        payload = json.loads(result.stdout)
        codes = [task.get("error_code") for task in payload["tasks"]]
        if expected not in codes:
            sys.stderr.write(result.stdout + result.stderr)
            return 1
        security_results.append(expected)

    report = {
        "schema": "pipeline-demo-v1",
        "repeatable_runs": True,
        "run_output_sha256": __import__("hashlib").sha256(
            outputs[0].encode("utf-8")
        ).hexdigest(),
        "verify": "ok",
        "sandbox_denials": security_results,
    }
    sys.stdout.write(
        json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
