#!/usr/bin/env python3.11
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def invoke(
    repo: Path,
    python: str,
    db: Path,
    args: list[str],
    *,
    crash_at: str | None = None,
) -> tuple[int, dict[str, Any] | None, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo)
    if crash_at is not None:
        env["EVENTBUS_CRASH_AT"] = crash_at
    completed = subprocess.run(
        [python, "-m", "eventbus", *args, "--db", str(db)],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    parsed: dict[str, Any] | None = None
    if completed.stdout.strip():
        try:
            parsed = json.loads(completed.stdout)
        except json.JSONDecodeError:
            parsed = None
    return completed.returncode, parsed, completed.stderr


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    python = sys.executable
    root = Path(tempfile.mkdtemp(prefix="eventbus-crash-"))
    db = root / "bus.db"
    records: list[dict[str, Any]] = []
    try:
        code, _, _ = invoke(
            repo,
            python,
            db,
            ["load", "--samples", "samples", "--clock", "11000"],
            crash_at="after_intent",
        )
        records.append({"phase": "load", "crash_exit": code})
        code, resumed, _ = invoke(repo, python, db, ["resume"])
        records.append(
            {"phase": "load-resume", "exit": code, "result": resumed}
        )

        code, _, _ = invoke(
            repo,
            python,
            db,
            ["edit", "--clock", "15000", "--kind", "event", "--action", "add",
             "--data", '{"clock_ms":1490,"partition_key":"customer-z","payload":{"amount":250,"kind":"order.created","order_id":"o-9"},"seq":9,"topic":"orders"}'],
        )
        code, _, _ = invoke(
            repo,
            python,
            db,
            ["publish", "--clock", "15050"],
            crash_at="after_append_batch",
        )
        records.append({"phase": "publish", "crash_exit": code})
        code, resumed, _ = invoke(repo, python, db, ["resume"])
        records.append(
            {"phase": "publish-resume", "exit": code, "result": resumed}
        )

        code, _, _ = invoke(
            repo,
            python,
            db,
            ["bridge", "--clock", "16000"],
            crash_at="after_bridge_stage",
        )
        records.append({"phase": "bridge", "crash_exit": code})
        code, resumed, _ = invoke(repo, python, db, ["resume"])
        records.append(
            {"phase": "bridge-resume", "exit": code, "result": resumed}
        )

        code, _, _ = invoke(
            repo,
            python,
            db,
            ["consume", "--consumer", "ledger", "--clock", "17000", "--budget", "1"],
            crash_at="after_consume_stage",
        )
        records.append({"phase": "consume", "crash_exit": code})
        code, resumed, _ = invoke(repo, python, db, ["resume"])
        records.append(
            {"phase": "consume-resume", "exit": code, "result": resumed}
        )

        for _ in range(20):
            status_code, status, _ = invoke(repo, python, db, ["status"])
            if status_code != 0 or status is None:
                raise RuntimeError("cannot read status while draining consumers")
            if all(item["lag"] == 0 for item in status["subscriptions"]):
                break
            for item in status["subscriptions"]:
                if item["lag"] > 0:
                    invoke(
                        repo,
                        python,
                        db,
                        ["consume", "--consumer", item["consumer_id"], "--clock", "18000"],
                    )
        else:
            raise RuntimeError("consumers did not drain")

        code, _, _ = invoke(
            repo,
            python,
            db,
            ["cleanup", "--clock", "19000"],
            crash_at="after_cleanup_stage",
        )
        records.append({"phase": "cleanup", "crash_exit": code})
        code, resumed, _ = invoke(repo, python, db, ["resume"])
        records.append(
            {"phase": "cleanup-resume", "exit": code, "result": resumed}
        )

        code, verify, _ = invoke(
            repo, python, db, ["verify", "--samples", "samples"]
        )
        records.append({"phase": "verify", "exit": code, "result": verify})
        ok = bool(code == 0 and verify and verify.get("ok"))
        result = {
            "ok": ok,
            "phases": records,
        }
        print(canonical_json(result))
        return 0 if ok else 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

