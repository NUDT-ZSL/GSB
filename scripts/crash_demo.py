#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
POINTS = (
    "after_prepare_windows",
    "inside_window_commit",
    "after_window_commit",
    "after_commit_before_settle",
)


def run(*args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "schedkernel", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )


def parsed(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--point", choices=POINTS, default="after_window_commit")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    run_dir = Path(
        tempfile.mkdtemp(prefix=f"crash-{args.point}-", dir=ROOT / "artifacts/demo")
    )
    db = run_dir / "scheduler.db"
    lock = run_dir / "scheduler.lock"
    tmp = run_dir / "tmp"
    common = ("--db", str(db), "--lock", str(lock), "--tmp", str(tmp))

    load_result = run("load", *common)
    if load_result.returncode != 0:
        sys.stderr.write(load_result.stderr)
        return 1
    crash_result = run("dispatch", *common, "--crash-at", args.point)
    before_result = run("status", *common)
    before_verify_result = run("verify", *common)
    resume_result = run("resume", *common)
    after_result = run("status", *common)
    after_verify_result = run("verify", *common)
    if crash_result.returncode != 137:
        sys.stderr.write(
            f"crash point {args.point} did not terminate with 137: "
            f"{crash_result.returncode}\n{crash_result.stderr}"
        )
        return 1
    report = {
        "point": args.point,
        "crash_exit_code": crash_result.returncode,
        "before_resume": {
            "status": parsed(before_result),
            "verify": parsed(before_verify_result),
        },
        "resume": {
            "exit_code": resume_result.returncode,
            "result": parsed(resume_result) if resume_result.stdout else None,
            "stderr": resume_result.stderr,
        },
        "after_resume": {
            "status": parsed(after_result),
            "verify": parsed(after_verify_result),
        },
        "run_dir": str(run_dir),
    }
    out = Path(args.out) if args.out else ROOT / "artifacts/demo" / f"crash-{args.point}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, sort_keys=True, indent=2))
    return (
        0
        if resume_result.returncode == 0 and parsed(after_verify_result)["ok"]
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())

