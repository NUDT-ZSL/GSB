#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PYTHON = str(ROOT / ".venv" / "bin" / "python")
if not Path(PYTHON).exists():
    PYTHON = sys.executable
SQLITE = str(ROOT / ".tools" / "bin" / "sqlite3")
if not Path(SQLITE).exists():
    SQLITE = "sqlite3"


def cli(args: list[str], *, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [PYTHON, "-m", "delivery_gateway.cli", *args],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )


def load_args(db: Path) -> list[str]:
    return [
        "load",
        "--db",
        str(db),
        "--topics",
        str(ROOT / "fixtures" / "topics.json"),
        "--consumers",
        str(ROOT / "fixtures" / "consumers.json"),
        "--quota",
        str(ROOT / "fixtures" / "quota.json"),
        "--clock",
        str(ROOT / "fixtures" / "clock.json"),
        "--faults",
        str(ROOT / "fixtures" / "faults.json"),
    ]


def parse_ok(proc: subprocess.CompletedProcess[str], stage: str) -> dict[str, Any]:
    if proc.returncode != 0:
        raise RuntimeError(f"{stage} failed rc={proc.returncode}: {proc.stdout} {proc.stderr}")
    return json.loads(proc.stdout)


def query(db: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return [tuple(row) for row in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def ledger_digest(db: Path) -> str:
    rows = query(
        db,
        "SELECT event_id, delivery_id, at_ms, global_before_scaled, global_refill_scaled, "
        "global_after_scaled, consumer_before_scaled, consumer_refill_scaled, consumer_after_scaled "
        "FROM ledger_events ORDER BY event_id",
    )
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def deterministic_ledger(root: Path) -> dict[str, Any]:
    outputs = []
    statuses = []
    digests = []
    for index in range(2):
        db = root / f"deterministic-{index}.db"
        parse_ok(cli(load_args(db)), "load deterministic")
        args = [
            "deliver",
            "--db",
            str(db),
            "--consumer",
            "consumer-b",
            "--request-id",
            "det-b-1",
            "--now-ms",
            "1000",
        ]
        result = parse_ok(cli(args), "deterministic deliver")
        repeated = parse_ok(cli(args), "idempotent repeat")
        status = cli(["status", "--db", str(db)])
        if status.returncode != 0:
            raise RuntimeError(status.stdout + status.stderr)
        if result != repeated:
            raise RuntimeError("repeated request result is not identical")
        outputs.append(result)
        statuses.append(status.stdout)
        digests.append(ledger_digest(db))
        verification = parse_ok(cli(["verify", "--db", str(db)]), "verify deterministic")
        if not verification["ok"]:
            raise RuntimeError(f"verify failed: {verification}")
    if outputs[0] != outputs[1] or statuses[0] != statuses[1] or digests[0] != digests[1]:
        raise RuntimeError("two identical logical runs diverged")
    return {
        "delivered": outputs[0]["summary"]["delivered"],
        "request_output_sha256": hashlib.sha256(
            json.dumps(outputs[0], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "status_sha256": hashlib.sha256(statuses[0].encode()).hexdigest(),
        "ledger_sha256": digests[0],
    }


def confirm_idempotency(root: Path) -> dict[str, Any]:
    db = root / "confirm.db"
    parse_ok(cli(load_args(db)), "load confirm")
    result = parse_ok(
        cli(
            [
                "deliver",
                "--db",
                str(db),
                "--consumer",
                "consumer-b",
                "--request-id",
                "confirm-1",
                "--now-ms",
                "1000",
                "--limit",
                "1",
            ]
        ),
        "confirm deliver",
    )
    delivery_id = result["outcomes"][0]["delivery_id"]
    message_id = result["outcomes"][0]["message_id"]
    args = [
        "confirm",
        "--db",
        str(db),
        "--delivery-id",
        delivery_id,
        "--consumer-id",
        "consumer-b",
        "--now-ms",
        "1010",
    ]
    first = parse_ok(cli(args), "first confirmation")
    second = parse_ok(cli(args), "second confirmation")
    cross = parse_ok(
        cli(
            [
                "confirm",
                "--db",
                str(db),
                "--delivery-id",
                delivery_id,
                "--consumer-id",
                "consumer-a",
                "--now-ms",
                "1010",
            ]
        ),
        "cross confirmation",
    )
    generation = query(
        db, f"SELECT generation FROM messages WHERE message_id='{message_id}'"
    )[0][0]
    verification = parse_ok(cli(["verify", "--db", str(db)]), "verify confirm")
    if not first["confirmed"] or not second["idempotent"] or cross["confirmed"]:
        raise RuntimeError("confirmation idempotency assertions failed")
    if generation != 1 or not verification["ok"]:
        raise RuntimeError("generation changed or verify failed")
    return {
        "delivery_id": delivery_id,
        "generation_after_two_confirms": generation,
        "second_confirm_idempotent": second["idempotent"],
        "cross_consumer_confirmed": cross["confirmed"],
    }


def crash_recovery(root: Path) -> dict[str, Any]:
    results = {}
    db = root / "crash-after-claim.db"
    parse_ok(cli(load_args(db)), "load crash after claim")
    proc = cli(
        [
            "deliver",
            "--db",
            str(db),
            "--consumer",
            "consumer-c",
            "--request-id",
            "crash-c",
            "--now-ms",
            "1000",
            "--limit",
            "1",
        ]
    )
    if proc.returncode != -9:
        raise RuntimeError(f"expected SIGKILL, got {proc.returncode}: {proc.stdout} {proc.stderr}")
    charged_before = query(
        db, "SELECT balance_scaled FROM buckets WHERE bucket_id='global'"
    )[0][0]
    resumed = parse_ok(cli(["resume", "--db", str(db), "--now-ms", "1000"]), "resume after claim")
    charged_after = query(
        db, "SELECT balance_scaled FROM buckets WHERE bucket_id='global'"
    )[0][0]
    verification = parse_ok(cli(["verify", "--db", str(db)]), "verify crash after claim")
    if charged_before != charged_after or not verification["ok"]:
        raise RuntimeError("resume recharged upgraded token after crash")
    results["fault_crash_after_claim"] = {
        "returncode": proc.returncode,
        "resumed_deliveries": resumed["resumed_deliveries"],
        "bucket_unchanged_during_resume": charged_before == charged_after,
        "ledger_events": verification["checks"]["ledger_events"],
    }

    db2 = root / "crash-before-claim.db"
    parse_ok(cli(load_args(db2)), "load crash before claim")
    proc2 = cli(
        [
            "deliver",
            "--db",
            str(db2),
            "--consumer",
            "consumer-b",
            "--request-id",
            "crash-before",
            "--now-ms",
            "1000",
            "--limit",
            "1",
        ],
        env_extra={"GATEWAY_CRASH_POINT": "claim_before_commit"},
    )
    if proc2.returncode != -9:
        raise RuntimeError(f"expected SIGKILL before claim commit, got {proc2.returncode}")
    before_events = query(db2, "SELECT COUNT(*) FROM ledger_events")[0][0]
    resumed2 = parse_ok(cli(["resume", "--db", str(db2), "--now-ms", "1000"]), "resume before claim")
    verification2 = parse_ok(cli(["verify", "--db", str(db2)]), "verify before claim")
    if before_events != 0 or not verification2["ok"]:
        raise RuntimeError("claim transaction was not rolled back before resume")
    results["sigkill_before_claim_commit"] = {
        "returncode": proc2.returncode,
        "ledger_events_before_resume": before_events,
        "resumed_deliveries": resumed2["resumed_deliveries"],
        "completed_requests": resumed2["completed_requests"],
        "ledger_events_after_resume": verification2["checks"]["ledger_events"],
    }
    return results


def concurrent_gateways(root: Path) -> dict[str, Any]:
    db = root / "concurrent.db"
    parse_ok(cli(load_args(db)), "load concurrent")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    commands = [
        [
            PYTHON,
            "-m",
            "delivery_gateway.cli",
            "deliver",
            "--db",
            str(db),
            "--consumer",
            "consumer-a",
            "--request-id",
            "concurrent-a",
            "--now-ms",
            "1000",
        ],
        [
            PYTHON,
            "-m",
            "delivery_gateway.cli",
            "deliver",
            "--db",
            str(db),
            "--consumer",
            "consumer-b",
            "--request-id",
            "concurrent-b",
            "--now-ms",
            "1000",
        ],
        [
            PYTHON,
            "-m",
            "delivery_gateway.cli",
            "resume",
            "--db",
            str(db),
            "--now-ms",
            "1000",
        ],
    ]
    procs = [
        subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for command in commands
    ]
    completed = [proc.communicate(timeout=30) for proc in procs]
    for proc, (stdout, stderr) in zip(procs, completed):
        if proc.returncode != 0:
            raise RuntimeError(f"concurrent process failed: {stdout} {stderr}")
    spent = query(db, "SELECT COALESCE(SUM(charge_scaled),0) FROM ledger_events")[0][0]
    balance = query(db, "SELECT balance_scaled FROM buckets WHERE bucket_id='global'")[0][0]
    duplicate_generations = query(
        db,
        "SELECT COUNT(*) FROM (SELECT message_id, generation, COUNT(*) c FROM deliveries "
        "GROUP BY message_id, generation HAVING c>1)",
    )[0][0]
    tombstones_per_message = query(
        db,
        "SELECT COUNT(*) FROM (SELECT message_id, COUNT(*) c FROM dead_letters "
        "GROUP BY message_id HAVING c>1)",
    )[0][0]
    verification = parse_ok(cli(["verify", "--db", str(db)]), "verify concurrent")
    if not verification["ok"] or duplicate_generations or tombstones_per_message or balance < 0:
        raise RuntimeError("concurrency invariants failed")
    return {
        "processes": 3,
        "ledger_charges": spent // 1_000_000,
        "global_balance_scaled": balance,
        "duplicate_message_generations": duplicate_generations,
        "duplicate_tombstones": tombstones_per_message,
        "verify": verification["ok"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    args = parser.parse_args()
    tests = subprocess.run(
        [PYTHON, "-m", "unittest", "discover", "-v", "tests"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
    )
    test_lines = tests.stdout.strip().splitlines()
    test_summary = next(
        (line for line in reversed(test_lines) if line.startswith("Ran ")),
        test_lines[-1] if test_lines else "",
    )
    with tempfile.TemporaryDirectory(prefix="delivery-gateway-validation-") as tmp_name:
        root = Path(tmp_name)
        record = {
            "ok": tests.returncode == 0,
            "python": subprocess.check_output(
                [PYTHON, "--version"], text=True, stderr=subprocess.STDOUT
            ).strip(),
            "sqlite_cli": subprocess.check_output(
                [SQLITE, "--version"], text=True, stderr=subprocess.STDOUT
            ).strip(),
            "unit_tests": {
                "returncode": tests.returncode,
                "summary": test_summary,
                "result": test_lines[-1] if test_lines else "",
            },
            "deterministic_ledger": deterministic_ledger(root),
            "confirm_idempotency": confirm_idempotency(root),
            "crash_recovery": crash_recovery(root),
            "concurrency": concurrent_gateways(root),
        }
    text = json.dumps(record, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    if not record["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
