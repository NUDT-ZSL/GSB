from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = ROOT / "samples" / "main"


def read_sample() -> dict[str, Any]:
    return {
        name: json.loads((SAMPLE_DIR / f"{name}.json").read_text(encoding="utf-8"))
        for name in ("nodes", "pods", "config", "injections")
    }


def write_bundle(directory: Path, bundle: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("nodes", "pods", "config", "injections"):
        (directory / f"{name}.json").write_text(
            json.dumps(bundle[name], indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return directory


def sample_copy() -> dict[str, Any]:
    return copy.deepcopy(read_sample())


def run_cli(
    *args: str,
    cwd: Path | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", "scheduler_diag", *args],
        cwd=str(cwd or ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"CLI failed ({result.returncode})\nstdout={result.stdout}\n"
            f"stderr={result.stderr}"
        )
    return result


def cli_json(
    *args: str,
    cwd: Path | None = None,
    check: bool = False,
) -> dict[str, Any]:
    result = run_cli(*args, cwd=cwd, check=check)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"CLI output is not JSON: {result.stdout!r}; stderr={result.stderr!r}"
        ) from exc


def ledger_payload(db_path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT v.payload_json
            FROM ledger_pointer p
            JOIN ledger_versions v ON v.version = p.version
            WHERE p.id = 1
            """
        ).fetchone()
        return json.loads(row["payload_json"])
    finally:
        connection.close()


def entries_by_pod(db_path: Path) -> dict[str, dict[str, Any]]:
    return {
        entry["pod"]: entry
        for entry in ledger_payload(db_path)["entries"]
    }
