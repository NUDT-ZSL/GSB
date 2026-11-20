"""Exercise hard termination at refill and scheduling boundaries, then resume."""

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


def command(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = (
        str(ROOT / "src")
        + os.pathsep
        + environment.get("PYTHONPATH", "")
    )
    return subprocess.run(
        [sys.executable, "-m", "qos_pipeline", *arguments],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def successful_json(arguments: list[str]) -> dict[str, Any]:
    completed = command(arguments)
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {completed.stdout} {completed.stderr}"
        )
    return json.loads(completed.stdout)


def state_digest(database: Path) -> str:
    return successful_json(["status", "--db", str(database)])["state_digest"]


def step_digests(database: Path) -> list[tuple[int, str]]:
    with sqlite3.connect(database) as connection:
        return connection.execute(
            "SELECT step_index, step_digest FROM steps ORDER BY step_index"
        ).fetchall()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="samples/manifest.json")
    parser.add_argument("--crash-step", type=int, default=12)
    parser.add_argument("--max-steps", type=int, default=20)
    args = parser.parse_args()
    manifest = Path(args.manifest).resolve()
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        clean_db = base / "clean.db"
        successful_json(
            ["load", "--manifest", str(manifest), "--db", str(clean_db)]
        )
        successful_json(["shape", "--db", str(clean_db), "--no-steps"])
        expected_state = state_digest(clean_db)
        expected_chain = step_digests(clean_db)
        results: dict[str, Any] = {}
        for point in ("after_refill", "after_schedule", "before_commit"):
            database = base / f"crash_{point}.db"
            successful_json(
                ["load", "--manifest", str(manifest), "--db", str(database)]
            )
            crashed = command(
                [
                    "shape",
                    "--db",
                    str(database),
                    "--max-steps",
                    str(args.max_steps),
                    "--crash-at-step",
                    str(args.crash_step),
                    "--crash-point",
                    point,
                    "--no-steps",
                ]
            )
            after_crash = successful_json(["status", "--db", str(database)])
            resumed = successful_json(
                ["resume", "--db", str(database), "--no-steps"]
            )
            verified = successful_json(
                [
                    "verify",
                    "--db",
                    str(database),
                    "--sample",
                    str(manifest),
                    "--no-steps",
                ]
            )
            results[point] = {
                "crash_exit_code": crashed.returncode,
                "watermark_after_crash": after_crash["last_step"],
                "expected_watermark": args.crash_step - 1,
                "resumed_first_step": (
                    min(resumed["steps_committed_this_call"])
                    if resumed["steps_committed_this_call"]
                    else None
                ),
                "state_digest_equal": state_digest(database) == expected_state,
                "step_chain_equal": step_digests(database) == expected_chain,
                "verify_ok": verified["ok"],
                "verify_complete": verified["complete"],
                "verify_first_mismatch": verified["first_mismatch"],
            }
        checks = {
            "all_exit_137": all(
                item["crash_exit_code"] == 137 for item in results.values()
            ),
            "all_watermarks_correct": all(
                item["watermark_after_crash"] == item["expected_watermark"]
                for item in results.values()
            ),
            "all_resumed_after_watermark": all(
                item["resumed_first_step"] == args.crash_step
                for item in results.values()
            ),
            "all_digests_equal": all(
                item["state_digest_equal"] and item["step_chain_equal"]
                for item in results.values()
            ),
            "all_verified": all(item["verify_ok"] for item in results.values()),
        }
        output = {
            "ok": all(checks.values()),
            "checks": checks,
            "expected_state_digest": expected_state,
            "step_count": len(expected_chain),
            "points": results,
        }
        print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

