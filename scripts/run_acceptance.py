#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "artifacts" / "validation_report.json"
RUNTIME_ROOT = ROOT / ".runtime"


def run(
    args: list[str],
    *,
    env_extra: dict[str, str] | None = None,
    expected: int = 0,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    if env_extra:
        env.update(env_extra)
    result = subprocess.run(
        args,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )
    if result.returncode != expected:
        raise RuntimeError(
            f"command failed ({result.returncode} != {expected}): {' '.join(args)}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


def cli(
    workspace: Path,
    *args: str,
    env_extra: dict[str, str] | None = None,
    expected: int = 0,
) -> dict[str, Any]:
    result = run(
        [
            sys.executable,
            "-m",
            "metric_engine",
            "--compact",
            "--workspace",
            str(workspace),
            *args,
        ],
        env_extra=env_extra,
        expected=expected,
    )
    return json.loads(result.stdout)


def snapshot_db(workspace: Path) -> dict[str, Any]:
    conn = sqlite3.connect(workspace / "state" / "engine.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        def scalar(sql: str, params: tuple[Any, ...] = ()) -> int:
            return int(conn.execute(sql, params).fetchone()[0])

        return {
            "version": scalar("SELECT version FROM ledger_pointer WHERE id=1"),
            "quota_charges": scalar("SELECT COUNT(*) FROM quota_charges"),
            "token_charges": scalar("SELECT COUNT(*) FROM token_charges"),
            "borrow_entries": scalar("SELECT COUNT(*) FROM borrow_ledger"),
            "precision_operations": scalar("SELECT COUNT(*) FROM precision_ops"),
            "precision_sensitive": scalar("SELECT COUNT(*) FROM precision_sensitive"),
            "persisted_samples": scalar(
                "SELECT COUNT(*) FROM samples WHERE state='PERSISTED'"
            ),
            "buffered_samples": scalar(
                "SELECT COUNT(*) FROM samples WHERE state='BUFFERED'"
            ),
            "dropped_samples": scalar(
                "SELECT COUNT(*) FROM samples WHERE state='DROPPED'"
            ),
            "attempt_outcomes": {
                row["outcome"]: int(row["n"])
                for row in conn.execute(
                    "SELECT outcome, COUNT(*) AS n FROM attempts "
                    "GROUP BY outcome ORDER BY outcome"
                )
            },
            "recovery_actions": {
                row["action"]: int(row["n"])
                for row in conn.execute(
                    "SELECT action, COUNT(*) AS n FROM recovery_events "
                    "GROUP BY action ORDER BY action"
                )
            },
        }
    finally:
        conn.close()


def reset(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def main() -> int:
    run([sys.executable, str(ROOT / "scripts" / "generate_samples.py")])
    main_runtime = RUNTIME_ROOT / "acceptance-main"
    recovery_runtime = RUNTIME_ROOT / "acceptance-recovery"
    reset(main_runtime)
    reset(recovery_runtime)

    commands: list[str] = []

    def command(workspace: Path, *args: str, **kwargs: Any) -> dict[str, Any]:
        commands.append(
            "metric-engine --workspace "
            + str(workspace.relative_to(ROOT))
            + " "
            + " ".join(args)
        )
        return cli(workspace, *args, **kwargs)

    load = command(
        main_runtime,
        "load",
        "--streams",
        "samples/streams.json",
        "--partitions",
        "samples/partitions.json",
        "--quotas",
        "samples/quotas.json",
        "--tokens",
        "samples/tokens.json",
    )
    phase1 = command(main_runtime, "ingest", "--events", "samples/events.json")
    flush1 = command(main_runtime, "flush", "--clock-ms", "2500")
    phase2 = command(
        main_runtime, "ingest", "--events", "samples/events_phase2.json"
    )
    flush2 = command(main_runtime, "flush", "--clock-ms", "6500")
    status = command(main_runtime, "status")
    verify = command(main_runtime, "verify")
    main_db = snapshot_db(main_runtime)

    recovery_load = command(
        recovery_runtime,
        "load",
        "--streams",
        "samples/streams.json",
        "--partitions",
        "samples/partitions.json",
        "--quotas",
        "samples/quotas.json",
        "--tokens",
        "samples/tokens.json",
    )
    recovery_ingest = command(
        recovery_runtime, "ingest", "--events", "samples/events_recovery.json"
    )
    commands.append(
        "METRIC_ENGINE_FAILPOINT=after_data_append_before_commit "
        "metric-engine --workspace "
        f"{recovery_runtime.relative_to(ROOT)} flush --clock-ms 2500"
    )
    crashed = run(
        [
            sys.executable,
            "-m",
            "metric_engine",
            "--compact",
            "--workspace",
            str(recovery_runtime),
            "flush",
            "--clock-ms",
            "2500",
        ],
        env_extra={
            "METRIC_ENGINE_FAILPOINT": "after_data_append_before_commit"
        },
        expected=86,
    ).returncode
    data_lines_before_resume = sum(
        1
        for path in (recovery_runtime / "data").glob("partition-*.jsonl")
        for _line in path.open(encoding="utf-8")
    )
    resume = command(recovery_runtime, "resume")
    recovery_status = command(recovery_runtime, "status")
    command(recovery_runtime, "flush", "--clock-ms", "2500")
    command(recovery_runtime, "flush", "--clock-ms", "6500")
    recovery_verify = command(recovery_runtime, "verify")
    recovery_db = snapshot_db(recovery_runtime)

    test_result = run(
        [sys.executable, "-m", "pytest", "-q"],
        env_extra={"PYTHONPATH": str(ROOT)},
    )
    test_lines = [
        line for line in test_result.stdout.strip().splitlines() if line.strip()
    ]

    import numpy
    import pytest

    report = {
        "ok": verify["ok"]
        and recovery_verify["ok"]
        and recovery_db["quota_charges"] == 5
        and recovery_db["token_charges"] == 5,
        "environment": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "numpy": numpy.__version__,
            "pytest": pytest.__version__,
            "platform": platform.platform(),
        },
        "commands": commands,
        "main_scenario": {
            "load": load,
            "phase1_ingest_count": phase1["count"],
            "phase1_flush_persisted": flush1["persisted_count"],
            "phase2_ingest_count": phase2["count"],
            "phase2_flush_persisted": flush2["persisted_count"],
            "status": {
                "version": status["version"],
                "logical_clock_ms": status["logical_clock_ms"],
                "token": status["token"],
                "buffer_depth": status["buffer_depth"],
                "tenants": status["tenants"],
                "precision_sensitive_count": len(status["precision_sensitive"]),
                "precision_sensitive": status["precision_sensitive"],
            },
            "verify": {
                "ok": verify["ok"],
                "persisted_samples": verify["persisted_samples"],
                "buffered_samples": verify["buffered_samples"],
                "dropped_samples": verify["dropped_samples"],
                "manifest_digest": verify["manifest_digest"],
                "errors": verify["errors"],
            },
            "database": main_db,
        },
        "recovery_scenario": {
            "load": recovery_load,
            "ingest_count": recovery_ingest["count"],
            "crash_exit_code": crashed,
            "data_lines_before_resume": data_lines_before_resume,
            "resume": resume,
            "status_buffer_depth": recovery_status["buffer_depth"],
            "status_token": recovery_status["token"],
            "verify": {
                "ok": recovery_verify["ok"],
                "persisted_samples": recovery_verify["persisted_samples"],
                "buffered_samples": recovery_verify["buffered_samples"],
                "manifest_digest": recovery_verify["manifest_digest"],
                "errors": recovery_verify["errors"],
            },
            "database": recovery_db,
        },
        "tests": {
            "returncode": test_result.returncode,
            "summary": test_lines[-1] if test_lines else "",
        },
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"ok": report["ok"], "report": str(REPORT)}, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

