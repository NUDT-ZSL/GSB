#!/usr/bin/env python3.11
from __future__ import annotations

import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    import numpy as np  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised on minimal Python installs
    np = None


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def invoke(repo: Path, python: str, db: Path, args: list[str]) -> dict[str, Any]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo)
    completed = subprocess.run(
        [python, "-m", "eventbus", *args, "--db", str(db)],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed: {args}: {completed.returncode}: {completed.stdout}"
        )
    return json.loads(completed.stdout)


def spawn_many(
    repo: Path,
    python: str,
    db: Path,
    commands: list[list[str]],
) -> list[dict[str, Any]]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo)
    processes = [
        subprocess.Popen(
            [python, "-m", "eventbus", *args, "--db", str(db)],
            cwd=repo,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for args in commands
    ]
    results: list[dict[str, Any]] = []
    for proc in processes:
        stdout, stderr = proc.communicate(timeout=90)
        if proc.returncode != 0:
            raise RuntimeError(
                f"concurrent command failed: {proc.returncode}: {stdout}: {stderr}"
            )
        results.append(json.loads(stdout))
    return results


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    python = sys.executable
    root = Path(tempfile.mkdtemp(prefix="eventbus-concurrency-"))
    db = root / "bus.db"
    seed = 149
    if np is not None:
        rng = np.random.default_rng(seed)
        permutation = lambda size: rng.permutation(size).tolist()
        rng_backend = f"numpy:{np.__version__}"
    else:
        rng = random.Random(seed)

        def permutation(size: int) -> list[int]:
            values = list(range(size))
            rng.shuffle(values)
            return values

        rng_backend = "stdlib-random"
    try:
        invoke(
            repo,
            python,
            db,
            ["load", "--samples", "samples", "--clock", "11000"],
        )
        invoke(
            repo,
            python,
            db,
            [
                "edit",
                "--clock",
                "15000",
                "--kind",
                "event",
                "--action",
                "add",
                "--data",
                '{"clock_ms":1490,"partition_key":"customer-z","payload":{"amount":250,"kind":"order.created","order_id":"o-9"},"seq":9,"topic":"orders"}',
            ],
        )
        publish_commands = [
            ["publish", "--clock", "15050"]
            for _ in range(4)
        ]
        order = permutation(len(publish_commands))
        publish_results = spawn_many(
            repo, python, db, [publish_commands[index] for index in order]
        )
        edit_commands = [
            [
                "edit",
                "--clock",
                "15500",
                "--kind",
                "event",
                "--action",
                "update",
                "--id",
                "1",
                "--data",
                '{"payload":{"amount":121,"kind":"order.created","order_id":"o-1"}}',
            ],
            [
                "edit",
                "--clock",
                "15500",
                "--kind",
                "event",
                "--action",
                "update",
                "--id",
                "2",
                "--data",
                '{"payload":{"amount":46,"kind":"order.created","order_id":"o-2"}}',
            ],
        ]
        order = permutation(len(edit_commands))
        edit_results = spawn_many(
            repo, python, db, [edit_commands[index] for index in order]
        )
        bridge_commands = [["bridge", "--clock", "16000"] for _ in range(3)]
        order = permutation(len(bridge_commands))
        bridge_results = spawn_many(
            repo, python, db, [bridge_commands[index] for index in order]
        )
        consume_commands = (
            [
                ["consume", "--consumer", "ledger", "--clock", "17000"]
                for _ in range(4)
            ]
            + [
                ["consume", "--consumer", "audit-index", "--clock", "17000"]
                for _ in range(8)
            ]
            + [
                ["consume", "--consumer", "orders-reader", "--clock", "17000"]
                for _ in range(3)
            ]
        )
        order = permutation(len(consume_commands))
        consume_results = spawn_many(
            repo, python, db, [consume_commands[index] for index in order]
        )
        verify = invoke(
            repo, python, db, ["verify", "--samples", "samples"]
        )
        conn = sqlite3.connect(db)
        try:
            published = int(
                conn.execute("SELECT COUNT(*) FROM published_events").fetchone()[0]
            )
            duplicate_offsets = int(
                conn.execute(
                    "SELECT COUNT(*) FROM ("
                    "SELECT channel, offset FROM records "
                    "GROUP BY channel, offset HAVING COUNT(*) > 1)"
                ).fetchone()[0]
            )
            duplicate_actions = int(
                conn.execute(
                    "SELECT COUNT(*) FROM ("
                    "SELECT rule_id, source_offset FROM bridge_actions "
                    "GROUP BY rule_id, source_offset HAVING COUNT(*) > 1)"
                ).fetchone()[0]
            )
            subscription_offsets = {
                str(row[0]): int(row[1])
                for row in conn.execute(
                    "SELECT consumer_id, current_offset FROM subscriptions "
                    "ORDER BY consumer_id"
                )
            }
            ledger_offset = subscription_offsets["ledger"]
        finally:
            conn.close()
        result = {
            "bridge_commands": len(bridge_results),
            "consume_commands": len(consume_results),
            "duplicate_bridge_actions": duplicate_actions,
            "duplicate_offsets": duplicate_offsets,
            "edit_commands": len(edit_results),
            "ledger_offset": ledger_offset,
            "ok": bool(
                verify["ok"]
                and published == 9
                and duplicate_offsets == 0
                and duplicate_actions == 0
                and subscription_offsets
                == {"audit-index": 7, "ledger": 5, "orders-reader": 6}
            ),
            "published_events": published,
            "publish_commands": len(publish_results),
            "rng_backend": rng_backend,
            "seed": seed,
            "subscription_offsets": subscription_offsets,
            "verify": verify,
        }
        print(canonical_json(result))
        return 0 if result["ok"] else 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

