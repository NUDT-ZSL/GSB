#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from leasecoord.config import read_json  # noqa: E402
from leasecoord.db import answer_json  # noqa: E402
from leasecoord.engine import Engine  # noqa: E402
from leasecoord.errors import BusinessError  # noqa: E402


def _elect_worker(workspace: str, barrier: Any, queue: Any) -> None:
    try:
        barrier.wait(timeout=30)
        result = Engine(workspace).elect(clock="0")
        queue.put(
            {
                "ok": True,
                "leader": result["leader"],
                "election_id": result["election_id"],
                "idempotent": result["idempotent"],
            }
        )
    except BusinessError as exc:
        queue.put({"ok": False, "code": exc.code, "details": exc.details})
    except BaseException as exc:
        queue.put({"ok": False, "code": "E_WORKER", "details": {"reason": str(exc)}})


def _renew_worker(workspace: str, barrier: Any, queue: Any) -> None:
    try:
        barrier.wait(timeout=30)
        result = Engine(workspace).renew(
            node="alpha",
            session_epoch=1,
            clock="100",
        )
        queue.put(
            {
                "ok": True,
                "expires_ms": result["expires_ms"],
                "lease_generation": result["lease_generation"],
                "idempotent": result["idempotent"],
            }
        )
    except BusinessError as exc:
        queue.put({"ok": False, "code": exc.code, "details": exc.details})
    except BaseException as exc:
        queue.put({"ok": False, "code": "E_WORKER", "details": {"reason": str(exc)}})


def _run_workers(
    context: Any,
    target: Any,
    workspace: Path,
    workers: int,
) -> list[dict[str, Any]]:
    barrier = context.Barrier(workers)
    queue = context.Queue()
    processes = [
        context.Process(target=target, args=(str(workspace), barrier, queue))
        for _ in range(workers)
    ]
    for process in processes:
        process.start()
    results = [queue.get(timeout=60) for _ in range(workers)]
    for process in processes:
        process.join(timeout=60)
        if process.exitcode not in (0, None):
            results.append(
                {
                    "ok": False,
                    "code": "E_WORKER_EXIT",
                    "details": {"exitcode": process.exitcode},
                }
            )
    return results


def _clean_runtime(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    for name in ("coord.db", "coord.db-wal", "coord.db-shm", ".coord.lock"):
        (workspace / name).unlink(missing_ok=True)


def run_check(workspace: Path, *, workers: int = 8) -> dict[str, Any]:
    sample = ROOT / "workspace"
    workspace = workspace.resolve()
    _clean_runtime(workspace)
    engine = Engine(workspace)
    engine.load(
        read_json(sample / "nodes.json"),
        read_json(sample / "election_config.json"),
        clock="0",
        reset=True,
    )
    context = mp.get_context("spawn")
    bootstrap_results = _run_workers(context, _elect_worker, workspace, workers)
    renew_results = _run_workers(context, _renew_worker, workspace, workers)

    connection = sqlite3.connect(workspace / "coord.db")
    connection.row_factory = sqlite3.Row
    try:
        generation_duplicates = [
            dict(row)
            for row in connection.execute(
                """
                SELECT node_name, lease_generation, COUNT(*) AS amount
                FROM lease_history
                GROUP BY node_name, lease_generation
                HAVING COUNT(*) > 1
                """
            ).fetchall()
        ]
        lease_count = connection.execute("SELECT COUNT(*) AS n FROM leases").fetchone()["n"]
        history_count = connection.execute(
            "SELECT COUNT(*) AS n FROM lease_history"
        ).fetchone()["n"]
        renewal_count = connection.execute(
            "SELECT COUNT(*) AS n FROM renewals"
        ).fetchone()["n"]
        prepared = {
            row["status"]: row["n"]
            for row in connection.execute(
                "SELECT status, COUNT(*) AS n FROM prepared_ops GROUP BY status ORDER BY status"
            ).fetchall()
        }
        raw_leases = {
            row["node_name"]: {
                "lease_generation": row["lease_generation"],
                "expires_ms": row["expires_text"],
            }
            for row in connection.execute(
                "SELECT * FROM leases ORDER BY node_name"
            ).fetchall()
        }
    finally:
        connection.close()

    verification = engine.verify()
    bootstrap_ok = all(item.get("ok") for item in bootstrap_results)
    renew_ok = all(item.get("ok") for item in renew_results)
    unique_election_ids = sorted(
        {item["election_id"] for item in bootstrap_results if item.get("ok")}
    )
    winners = sorted(
        {item["leader"] for item in bootstrap_results if item.get("ok")}
    )
    unique_expiries = sorted(
        {item["expires_ms"] for item in renew_results if item.get("ok")}
    )
    idempotent_bootstrap = sum(
        1 for item in bootstrap_results if item.get("idempotent")
    )
    idempotent_renew = sum(1 for item in renew_results if item.get("idempotent"))
    ok = (
        bootstrap_ok
        and renew_ok
        and winners == ["bravo"]
        and len(unique_election_ids) == 1
        and unique_expiries == ["1100"]
        and lease_count == 3
        and history_count == 3
        and renewal_count == 1
        and not generation_duplicates
        and prepared.get("COMMITTED", 0) >= 2
        and verification["verified"]
    )
    return {
        "ok": ok,
        "workers": workers,
        "spawn_method": "spawn",
        "concurrent_bootstrap": {
            "winner_values": winners,
            "unique_election_ids": unique_election_ids,
            "idempotent_workers": idempotent_bootstrap,
            "failed_workers": len(bootstrap_results) - sum(
                1 for item in bootstrap_results if item.get("ok")
            ),
        },
        "concurrent_renew": {
            "expires_ms_values": unique_expiries,
            "idempotent_workers": idempotent_renew,
            "committed_renewal_rows": renewal_count,
            "failed_workers": len(renew_results) - sum(
                1 for item in renew_results if item.get("ok")
            ),
        },
        "leases": {
            "current_rows": lease_count,
            "history_rows": history_count,
            "generation_duplicates": generation_duplicates,
            "rows": raw_leases,
        },
        "prepared_statuses": prepared,
        "verify": verification["verified"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default="workspace/runtime_concurrency")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    result = run_check(Path(args.workspace), workers=args.workers)
    print(answer_json(result, compact=args.compact))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
