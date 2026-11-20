#!/usr/bin/env python3
"""Run the deterministic end-to-end demo twice and compare every JSON output."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PROJECT = ROOT / "samples" / "project.json"
OUTPUT_ROOT = ROOT / ".demo"


def _run(
    *arguments: str,
    expected_exit: int = 0,
) -> dict[str, Any]:
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "gitops_controller",
            "--project",
            str(PROJECT),
            *arguments,
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if process.returncode != expected_exit:
        raise SystemExit(
            f"command failed: {' '.join(arguments)}\n"
            f"expected exit {expected_exit}, got {process.returncode}\n"
            f"stdout:\n{process.stdout}\nstderr:\n{process.stderr}"
        )
    payload: dict[str, Any] = {
        "exit_code": process.returncode,
        "stderr": process.stderr,
        "stdout": process.stdout,
    }
    if process.stdout.strip():
        json.loads(process.stdout)
    return payload


def _write_result(output_dir: Path, name: str, result: dict[str, Any]) -> None:
    path = output_dir / f"{name}.json"
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _one_pass(output_dir: Path) -> None:
    generator = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_samples.py"), "--reset-runtime"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if generator.returncode != 0:
        raise SystemExit(generator.stderr or generator.stdout)
    output_dir.mkdir(parents=True, exist_ok=True)
    steps = [
        ("01-load", _run("load")),
        ("02-plan-dev", _run("plan", "--env", "dev")),
        (
            "03-plan-staging-version-rollback",
            _run("plan", "--env", "staging", expected_exit=2),
        ),
        (
            "04-rollback-dev",
            _run(
                "rollback",
                "--env",
                "dev",
                "--budget-ms",
                "10000",
            ),
        ),
        ("05-status-dev", _run("status", "--env", "dev")),
        (
            "06-prod-crash",
            _run(
                "rollback",
                "--env",
                "prod",
                "--budget-ms",
                "10000",
                "--crash-after",
                "2",
                expected_exit=75,
            ),
        ),
        (
            "07-resume-prod",
            _run("resume", "--env", "prod", "--budget-ms", "10000"),
        ),
        ("08-verify-dev", _run("verify", "--env", "dev")),
        ("09-verify-prod", _run("verify", "--env", "prod")),
    ]
    for name, result in steps:
        _write_result(output_dir, name, result)


def _directory_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for file_path in sorted(path.glob("*.json")):
        digest.update(file_path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_path.read_bytes())
    return digest.hexdigest()


def main() -> int:
    if OUTPUT_ROOT.exists():
        shutil.rmtree(OUTPUT_ROOT)
    first = OUTPUT_ROOT / "run-1"
    second = OUTPUT_ROOT / "run-2"
    _one_pass(first)
    _one_pass(second)
    first_hash = _directory_hash(first)
    second_hash = _directory_hash(second)
    identical = first_hash == second_hash
    summary = {
        "deterministic": identical,
        "output_directory": str(OUTPUT_ROOT.relative_to(ROOT)),
        "run_1_sha256": first_hash,
        "run_2_sha256": second_hash,
        "steps": sorted(path.stem for path in first.glob("*.json")),
    }
    # Leave the checked-in samples and runtime in a clean, initial state.
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_samples.py"), "--reset-runtime"],
        cwd=str(ROOT),
        check=True,
        capture_output=True,
        text=True,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if identical else 1


if __name__ == "__main__":
    raise SystemExit(main())
