#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from graphstore.samples import DEFAULT_LOGICAL_CLOCK_MS, generate_samples
from graphstore.service import GraphStore
from graphstore.util import canonical_json


def _env(delay_ms: int = 0) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    if delay_ms:
        env["GRAPHSTORE_TEST_BATCH_DELAY_MS"] = str(delay_ms)
    else:
        env.pop("GRAPHSTORE_TEST_BATCH_DELAY_MS", None)
    return env


def _command_text(command: list[str]) -> str:
    return " ".join(
        f"'{item}'" if " " in item else item for item in command
    )


def _run_json(
    command: list[str],
    *,
    log: list[str],
    delay_ms: int = 0,
    check: bool = True,
) -> tuple[dict[str, Any], subprocess.CompletedProcess[str]]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=_env(delay_ms),
        capture_output=True,
        text=True,
        timeout=60,
    )
    log.append(f"$ {_command_text(command)}")
    if completed.stdout:
        log.append("stdout:")
        log.append(json.dumps(json.loads(completed.stdout), indent=2, sort_keys=True))
    if completed.stderr:
        log.append("stderr:")
        log.append(completed.stderr.rstrip())
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {_command_text(command)}"
        )
    payload = json.loads(completed.stdout) if completed.stdout else {}
    return payload, completed


def _traverse_command(
    state_db: Path,
    *,
    starts: list[str],
    max_parallel: int,
    memory_budget: int,
    batch_size: int,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "graphstore",
        "traverse",
        "--state-db",
        str(state_db),
        "--mode",
        "reach",
        "--max-depth",
        "2",
        "--max-parallel",
        str(max_parallel),
        "--memory-budget",
        str(memory_budget),
        "--batch-size",
        str(batch_size),
    ]
    for start in starts:
        command.extend(["--start", start])
    return command


def _wait_for_first_commit(state_db: Path, timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        connection = sqlite3.connect(state_db, timeout=5)
        try:
            row = connection.execute(
                """
                SELECT run_id, committed_batches
                FROM traversal_runs
                ORDER BY created_at_ms DESC, run_id DESC
                LIMIT 1
                """
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        finally:
            connection.close()
        if row is not None and row[1] >= 1:
            return row[0]
        time.sleep(0.02)
    raise TimeoutError("traversal did not commit a batch before the deadline")


def _commit_log_counts(state_db: Path, run_id: str) -> tuple[int, int]:
    connection = sqlite3.connect(state_db)
    try:
        total = connection.execute(
            "SELECT COUNT(*) FROM batch_commit_log WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
        duplicates = connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT batch_key, COUNT(*) AS c
                FROM batch_commit_log
                WHERE run_id = ?
                GROUP BY batch_key
                HAVING c > 1
            )
            """,
            (run_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    return total, duplicates


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the complete deterministic acceptance flow")
    parser.add_argument("--output", default="artifacts/acceptance")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    output_root = (ROOT / args.output).resolve()
    if output_root.exists():
        if not args.force:
            raise SystemExit(f"{output_root} exists; pass --force to replace it")
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True)
    samples_root = output_root / "samples"
    generate_samples(samples_root)
    v1 = samples_root / "v1" / "snapshot.json"
    v2 = samples_root / "v2"
    merged_state = output_root / "merged" / "state.sqlite"
    killed_state = output_root / "killed" / "state.sqlite"
    fresh_state = output_root / "fresh" / "state.sqlite"
    multi_state = output_root / "multi" / "state.sqlite"
    log: list[str] = [
        "Local graphstore acceptance run",
        f"python: {sys.version.split()[0]}",
        f"sqlite: {sqlite3.sqlite_version}",
        f"logical_clock_ms: {DEFAULT_LOGICAL_CLOCK_MS}",
        "",
    ]
    environment = GraphStore(merged_state).environment()
    log.append("SQLite/environment probe:")
    log.append(json.dumps(environment, indent=2, sort_keys=True))

    for state_db in (merged_state, killed_state, fresh_state, multi_state):
        state_db.parent.mkdir(parents=True, exist_ok=True)
        merge_command = [
            sys.executable,
            "-m",
            "graphstore",
            "merge",
            "--v1",
            str(v1),
            "--v2",
            str(v2),
            "--state-db",
            str(state_db),
        ]
        _run_json(merge_command, log=log)

    starts = ["n1", "n2", "n4", "n7"]
    budget_command = _traverse_command(
        merged_state,
        starts=starts,
        max_parallel=2,
        memory_budget=20,
        batch_size=1,
    )
    budget_payload, _ = _run_json(budget_command, log=log)
    budget_result = budget_payload["result"]
    if budget_result["outcomes"]["abandoned"] <= 0:
        raise RuntimeError("budget scenario did not abandon any batch")

    kill_command = _traverse_command(
        killed_state,
        starts=starts,
        max_parallel=1,
        memory_budget=1000,
        batch_size=1,
    )
    log.append(f"$ {_command_text(kill_command)}")
    log.append("(spawned and will be SIGKILLed after the first committed batch)")
    killed_process = subprocess.Popen(
        kill_command,
        cwd=ROOT,
        env=_env(500),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    killed_run_id = _wait_for_first_commit(killed_state)
    os.kill(killed_process.pid, signal.SIGKILL)
    killed_process.wait(timeout=10)
    log.append(f"SIGKILL pid={killed_process.pid} run_id={killed_run_id}")

    killed_status, _ = _run_json(
        [
            sys.executable,
            "-m",
            "graphstore",
            "status",
            "--state-db",
            str(killed_state),
            "--run-id",
            killed_run_id,
        ],
        log=log,
    )
    if killed_status["traversal"]["watermark"] != 1:
        raise RuntimeError("expected exactly one committed batch before SIGKILL")

    resumed_payload, _ = _run_json(
        [
            sys.executable,
            "-m",
            "graphstore",
            "resume",
            "--state-db",
            str(killed_state),
            "--run-id",
            killed_run_id,
        ],
        log=log,
    )
    resumed_result = resumed_payload["result"]

    fresh_payload, _ = _run_json(
        _traverse_command(
            fresh_state,
            starts=starts,
            max_parallel=1,
            memory_budget=1000,
            batch_size=1,
        ),
        log=log,
    )
    fresh_result = fresh_payload["result"]
    deterministic_match = (
        canonical_json(resumed_result["batch_digests"])
        == canonical_json(fresh_result["batch_digests"])
    )
    if not deterministic_match:
        raise RuntimeError("resumed traversal digest differs from fresh traversal")

    multi_command = _traverse_command(
        multi_state,
        starts=starts,
        max_parallel=2,
        memory_budget=1000,
        batch_size=1,
    )
    log.append(f"$ {_command_text(multi_command)}  # launched twice concurrently")
    multi_processes = [
        subprocess.Popen(
            multi_command,
            cwd=ROOT,
            env=_env(100),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    for process in multi_processes:
        _stdout, stderr = process.communicate(timeout=40)
        if process.returncode != 0:
            raise RuntimeError(f"concurrent traversal failed: {stderr}")
    multi_status, _ = _run_json(
        [
            sys.executable,
            "-m",
            "graphstore",
            "status",
            "--state-db",
            str(multi_state),
        ],
        log=log,
    )
    multi_run_id = multi_status["traversal"]["run_id"]
    multi_commits, multi_duplicates = _commit_log_counts(
        multi_state, multi_run_id
    )

    verify_payload, _ = _run_json(
        [
            sys.executable,
            "-m",
            "graphstore",
            "verify",
            "--state-db",
            str(killed_state),
            "--run-id",
            killed_run_id,
            "--v1",
            str(v1),
            "--v2",
            str(v2),
            "--logical-clock-ms",
            str(DEFAULT_LOGICAL_CLOCK_MS),
        ],
        log=log,
    )
    if not verify_payload.get("ok"):
        raise RuntimeError("verify failed")

    summary = {
        "environment": environment,
        "deterministic_match": deterministic_match,
        "budget": {
            "outcomes": budget_result["outcomes"],
            "abandoned": budget_result["abandoned"],
        },
        "sigkill": {
            "run_id": killed_run_id,
            "watermark_before_resume": killed_status["traversal"]["watermark"],
            "watermark_after_resume": resumed_payload["status"]["traversal"]["watermark"],
            "outcomes_after_resume": resumed_result["outcomes"],
            "result_digest": resumed_payload["status"]["traversal"]["result_digest"],
            "truth_digest": resumed_payload["status"]["traversal"]["truth_digest"],
        },
        "multi_process": {
            "run_id": multi_run_id,
            "watermark": multi_status["traversal"]["watermark"],
            "outcomes": multi_status["traversal"]["outcomes"],
            "batch_commit_rows": multi_commits,
            "duplicate_batch_rows": multi_duplicates,
        },
        "verify": verify_payload,
    }
    log.append("")
    log.append("ACCEPTANCE SUMMARY")
    log.append(json.dumps(summary, indent=2, sort_keys=True))
    summary_path = output_root / "summary.json"
    summary_path.write_text(canonical_json(summary) + "\n", encoding="utf-8")
    log_path = output_root / "verification.log"
    log_path.write_text("\n".join(log) + "\n", encoding="utf-8")
    print(
        canonical_json(
            {
                "ok": True,
                "summary": str(summary_path),
                "log": str(log_path),
                "deterministic_match": deterministic_match,
                "budget_abandoned": budget_result["outcomes"]["abandoned"],
                "resume_watermark": resumed_payload["status"]["traversal"]["watermark"],
                "multi_process_duplicates": multi_duplicates,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

