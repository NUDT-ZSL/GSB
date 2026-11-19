from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SAMPLE = ROOT / "samples" / "orders.json"
INJECTIONS = ROOT / "samples" / "injections"


def run_cli(
    db: Path,
    *args: str,
    env: dict[str, str] | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    merged_env = dict(os.environ)
    merged_env["PYTHONPATH"] = str(ROOT)
    if env:
        merged_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "sequential_mq", "--db", str(db), *args],
        cwd=ROOT,
        env=merged_env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def run_cli_parallel(
    db: Path,
    argument_sets: list[tuple[str, ...]],
    *,
    timeout: int = 30,
) -> list[subprocess.CompletedProcess[str]]:
    merged_env = dict(os.environ)
    merged_env["PYTHONPATH"] = str(ROOT)
    processes = [
        subprocess.Popen(
            [sys.executable, "-m", "sequential_mq", "--db", str(db), *args],
            cwd=ROOT,
            env=merged_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for args in argument_sets
    ]
    results: list[subprocess.CompletedProcess[str]] = []
    for process, args in zip(processes, argument_sets, strict=True):
        stdout, stderr = process.communicate(timeout=timeout)
        results.append(
            subprocess.CompletedProcess(
                args=process.args,
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        )
    return results


def run_json(db: Path, *args: str, expected_exit: int = 0) -> dict[str, Any]:
    result = run_cli(db, *args)
    assert result.returncode == expected_exit, (
        f"exit={result.returncode}, args={args}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert result.stderr == ""
    return json.loads(result.stdout)


def load_sample(
    db: Path,
    *,
    injection_manifest: str | None = None,
    expected_exit: int = 0,
) -> dict[str, Any]:
    args = ["load", "--sample", str(SAMPLE)]
    if injection_manifest is not None:
        args.extend(["--injections", str(INJECTIONS / injection_manifest)])
    return run_json(db, *args, expected_exit=expected_exit)


def sqlite_rows(db: Path, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    import sqlite3

    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()

