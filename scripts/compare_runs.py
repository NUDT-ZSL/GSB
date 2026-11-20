"""Run the same sample and injection manifest twice and compare every digest."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def run_cli(arguments: list[str]) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = (
        str(ROOT / "src")
        + os.pathsep
        + environment.get("PYTHONPATH", "")
    )
    completed = subprocess.run(
        [sys.executable, "-m", "qos_pipeline", *arguments],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {completed.stdout} {completed.stderr}"
        ) from exc
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {payload}"
        )
    return payload


def step_digests(database: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT step_index, version, step_digest FROM steps ORDER BY step_index, version"
        ).fetchall()
    return [
        {"step_index": row[0], "version": row[1], "step_digest": row[2]}
        for row in rows
    ]


def run_once(manifest: Path, database: Path, max_steps: int | None) -> dict[str, Any]:
    run_cli(["load", "--manifest", str(manifest), "--db", str(database)])
    shape_arguments = ["shape", "--db", str(database), "--no-steps"]
    if max_steps is not None:
        shape_arguments.extend(["--max-steps", str(max_steps)])
    shape = run_cli(shape_arguments)
    verify = run_cli(
        ["verify", "--db", str(database), "--sample", str(manifest), "--no-steps"]
    )
    return {
        "sample_digest": shape["sample_digest"],
        "state_digest": shape["status"]["state_digest"],
        "step_digest": shape["status"]["step_digest"],
        "derived": shape["derived"],
        "steps": step_digests(database),
        "verify": {
            "ok": verify["ok"],
            "complete": verify["complete"],
            "chain_valid": verify["chain_valid"],
            "first_mismatch": verify["first_mismatch"],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="samples/manifest.json")
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--keep-dir", default=None)
    args = parser.parse_args()
    manifest = Path(args.manifest).resolve()
    temporary_context = (
        tempfile.TemporaryDirectory() if args.keep_dir is None else None
    )
    base = (
        Path(temporary_context.name)
        if temporary_context is not None
        else Path(args.keep_dir).resolve()
    )
    if temporary_context is None:
        base.mkdir(parents=True, exist_ok=True)
    try:
        first = run_once(manifest, base / "run_a.db", args.max_steps)
        second = run_once(manifest, base / "run_b.db", args.max_steps)
        checks = {
            "sample_digest_equal": first["sample_digest"] == second["sample_digest"],
            "state_digest_equal": first["state_digest"] == second["state_digest"],
            "step_chain_equal": first["steps"] == second["steps"],
            "derived_equal": first["derived"] == second["derived"],
            "both_verified": first["verify"]["ok"] and second["verify"]["ok"],
        }
        result = {
            "ok": all(checks.values()),
            "checks": checks,
            "sample_digest": first["sample_digest"],
            "state_digest": first["state_digest"],
            "step_count": len(first["steps"]),
            "first_step_digest": first["steps"][0]["step_digest"] if first["steps"] else None,
            "last_step_digest": first["steps"][-1]["step_digest"] if first["steps"] else None,
            "estimator_comparison": first["derived"]["estimator_comparison"],
            "precision_marker_count": len(first["derived"]["precision_sensitive"]),
            "verify_a": first["verify"],
            "verify_b": second["verify"],
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if result["ok"] else 1
    finally:
        if temporary_context is not None:
            temporary_context.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())

