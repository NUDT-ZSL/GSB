#!/usr/bin/env python3
"""Reproducible end-to-end demo for a local, isolated SQLite database."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def clean_database(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", ".lock"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()


def run_command(
    db_path: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
    allowed_codes: set[int] | None = None,
) -> tuple[int, dict[str, Any]]:
    command = [
        sys.executable,
        "-m",
        "eventbus",
        "--db",
        str(db_path),
        *arguments,
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=env,
    )
    expected = allowed_codes if allowed_codes is not None else {0}
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        if completed.returncode in expected and not completed.stdout.strip():
            document = {}
        else:
            raise RuntimeError(
                f"command did not return JSON: {command}\n"
                f"stdout={completed.stdout}\nstderr={completed.stderr}"
            ) from exc
    if completed.returncode not in expected:
        raise RuntimeError(
            f"command failed with {completed.returncode}: {command}\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return completed.returncode, document


def print_step(name: str, value: Any) -> None:
    print(f"\n## {name}")
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_samples.py")],
        cwd=ROOT,
        check=True,
    )
    main_db = ROOT / "data" / "demo.sqlite"
    recovery_db = ROOT / "data" / "demo-recovery.sqlite"
    concurrency_db = ROOT / "data" / "demo-concurrency.sqlite"
    for path in (main_db, recovery_db, concurrency_db):
        clean_database(path)

    _, loaded = run_command(
        main_db,
        "load",
        "--v1",
        "samples/v1/events.json",
        "--v2-shards",
        "samples/v2/shards",
        "--v2-metadata",
        "samples/v2/metadata.sqlite",
        "--clock-ms",
        "1000",
    )
    print_step("load", loaded)

    _, status = run_command(main_db, "status")
    print_step(
        "initial watermarks",
        {
            "config_version": status["config_version"],
            "channels": status["channels"],
            "bridges": status["bridges"],
            "subscriptions": status["subscriptions"],
            "merge_decisions": status["merge_decisions"],
            "compatibility_degradations": status["compatibility_degradations"],
        },
    )

    _, bridged = run_command(
        main_db, "bridge", "--all", "--clock-ms", "2000"
    )
    print_step(
        "bridge",
        {
            "runs": bridged["runs"],
            "summary": {
                "delivered": sum(item["delivered"] for item in bridged["runs"]),
                "filtered": sum(item["filtered"] for item in bridged["runs"]),
                "final_offset": bridged["runs"][-1]["next_offset"],
            },
        },
    )

    _, consumed = run_command(
        main_db,
        "consume",
        "--subscription",
        "orders-reader",
        "--count",
        "7",
        "--request-id",
        "demo-orders-7",
        "--clock-ms",
        "3000",
    )
    print_step(
        "consume",
        {
            "subscription": consumed["subscription"],
            "from_offset": consumed["from_offset"],
            "offset": consumed["offset"],
            "event_count": len(consumed["events"]),
        },
    )

    _, cleanup = run_command(
        main_db,
        "cleanup",
        "--clock-ms",
        "4000",
        "--retention-bytes",
        "1000",
    )
    print_step(
        "cleanup",
        {
            "physical_removed": cleanup["physical_removed"],
            "tombstones_marked": cleanup["tombstones_marked"],
            "bytes_before": cleanup["bytes_before"],
            "bytes_after": cleanup["bytes_after"],
            "blocked_slowest": cleanup["blocked_slowest"],
            "reason": cleanup["reason"],
        },
    )

    _, verified = run_command(main_db, "verify")
    print_step("verify after bridge and cleanup", verified)

    _, edited = run_command(
        main_db,
        "edit",
        "--patch",
        "samples/patches/toggle-audit-tag.json",
        "--clock-ms",
        "5000",
    )
    print_step("edit/publish config version", edited)

    _, appended = run_command(
        main_db,
        "publish",
        "--channel",
        "orders",
        "--events",
        "samples/append-events.json",
        "--max-batch",
        "1",
        "--clock-ms",
        "6000",
    )
    print_step("append", appended)

    _, verified = run_command(main_db, "verify")
    print_step("verify after edit and append", verified)

    _, loaded_recovery = run_command(
        recovery_db,
        "load",
        "--v1",
        "samples/v1/events.json",
        "--v2-shards",
        "samples/v2/shards",
        "--v2-metadata",
        "samples/v2/metadata.sqlite",
        "--clock-ms",
        "1000",
    )
    env = dict(os.environ)
    env["EVENTBUS_FAULT_POINT"] = "bridge_before_commit"
    crash_code, _ = run_command(
        recovery_db,
        "bridge",
        "orders-to-audit",
        env=env,
        allowed_codes={86},
    )
    print_step(
        "forced termination before bridge commit",
        {
            "process_exit": crash_code,
            "database": str(recovery_db.relative_to(ROOT)),
        },
    )
    _, resumed = run_command(recovery_db, "resume")
    print_step(
        "resume",
        {
            "ok": resumed["ok"],
            "recovered_operations": resumed["recovered_operations"],
            "recovered_bridge_runs": resumed["recovered"],
            "channels": resumed["channels"],
        },
    )
    _, recovery_verify = run_command(recovery_db, "verify")
    print_step("verify after recovery", recovery_verify)

    _, loaded_concurrency = run_command(
        concurrency_db,
        "load",
        "--v1",
        "samples/v1/events.json",
        "--v2-shards",
        "samples/v2/shards",
        "--v2-metadata",
        "samples/v2/metadata.sqlite",
        "--clock-ms",
        "1000",
    )
    producers = []
    for index in range(4):
        event_file = ROOT / "data" / f"demo-event-{index}.json"
        event_file.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "sequence": 100 + index,
                            "partition_key": f"demo-concurrent-{index}",
                            "logical_clock": 500 + index,
                            "produced_at_ms": 1_700_000_100_000 + index,
                            "fields": {
                                "order_id": f"O-DEMO-{index}",
                                "region": "east",
                                "amount": index + 1,
                                "note": "demo-concurrent",
                                "currency": "CNY",
                                "version_tag": "demo",
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        producers.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "eventbus",
                    "--db",
                    str(concurrency_db),
                    "publish",
                    "--channel",
                    "orders",
                    "--events",
                    str(event_file),
                    "--clock-ms",
                    "7000",
                ],
                cwd=ROOT,
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(os.environ),
            )
        )
    producer_codes = [process.wait() for process in producers]
    _, concurrent_status = run_command(concurrency_db, "status")
    _, concurrent_verify = run_command(concurrency_db, "verify")
    print_step(
        "concurrent producers",
        {
            "exit_codes": producer_codes,
            "orders_high_watermark": next(
                item["high_watermark"]
                for item in concurrent_status["channels"]
                if item["name"] == "orders"
            ),
            "verify": concurrent_verify["ok"],
        },
    )

    print("\nDEMO OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
