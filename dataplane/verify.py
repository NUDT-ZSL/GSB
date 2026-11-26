from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

from .db import advance_clock, connect, get_meta, init_db
from .engine import _build_materialization, active_config_row, adjudicate_batch, row_to_dict
from .hashing import sha256_file
from .loader import load_manifest, load_and_validate_samples, load_patches


def status_report(conn: sqlite3.Connection) -> Dict[str, Any]:
    def count(table: str, where: str = "") -> int:
        return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table} {where}").fetchone()["n"])

    states = {
        row["state"]: int(row["n"])
        for row in conn.execute(
            "SELECT state, COUNT(*) AS n FROM block_task GROUP BY state"
        ).fetchall()
    }
    active_version = get_meta(conn, "active_config_version")
    active = None
    if active_version:
        row = conn.execute(
            "SELECT config_version,package_id,package_hash,classification,"
            "policy_generation_floor,merged_digest FROM config_snapshot WHERE config_version=?",
            (int(active_version),),
        ).fetchone()
        active = dict(row) if row else None
    latest_round = conn.execute(
        "SELECT round_id,scope,status,clock_ms,candidate_count,selected_count,"
        "completed_count,failed_count,not_started_count,abandoned_count,"
        "remaining_blocks,remaining_cost_ms FROM schedule_round ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    return {
        "clock_watermark_ms": int(get_meta(conn, "clock_watermark_ms", "0") or 0),
        "scheduler_watermark_clock_ms": int(
            get_meta(conn, "scheduler_watermark_clock_ms", "0") or 0
        ),
        "active_config_version": int(active_version) if active_version else None,
        "active_config": active,
        "pending_config": bool(get_meta(conn, "pending_config", "")),
        "counts": {
            "cases": count("cases"),
            "tensor_blocks": count("tensor_blocks"),
            "patches": count("patches"),
            "block_tasks": count("block_task"),
            "closures": count("invalidation_closure"),
            "rounds": count("schedule_round"),
            "decisions": count("inference_decision"),
            "error_ledger": count("error_ledger"),
        },
        "task_states": states,
        "latest_round": dict(latest_round) if latest_round else None,
    }


def verify_workspace(
    data_dir: Path | str,
    db_path: Path | str,
    deep_recompute: bool = True,
    validate_sources: bool = True,
    clock_ms: int | None = None,
    clock_skew_tolerance_ms: int = 5_000,
) -> Dict[str, Any]:
    data_dir = Path(data_dir)
    db_path = Path(db_path)
    source_summary = None
    if validate_sources:
        tasks = load_manifest(data_dir)
        samples = load_and_validate_samples(data_dir, tasks)
        patches = load_patches(data_dir, tasks)
        source_summary = {
            "cases": len(tasks),
            "tensor_descriptors": sum(len(v) for v in samples.values()),
            "patches": len(patches),
            "source_hashes_ok": True,
        }

    conn = connect(db_path)
    issues: List[Dict[str, Any]] = []
    checked_objects = 0
    recomputed = 0
    ledger_rows = 0
    try:
        init_db(conn)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            issues.append({"code": "E_STATE_CORRUPT", "message": f"SQLite integrity: {integrity}"})

        active = active_config_row(conn)
        if clock_ms is not None:
            clock = advance_clock(conn, int(clock_ms), clock_skew_tolerance_ms)
        else:
            clock = int(get_meta(conn, "clock_watermark_ms", "0") or 0)
        batch = adjudicate_batch(conn, clock, int(active["config_version"]))

        tasks = [
            row_to_dict(r)
            for r in conn.execute("SELECT * FROM block_task ORDER BY task_uid").fetchall()
        ]
        for task in tasks:
            if task["state"] not in {"committed", "materialized"}:
                continue
            relative = task.get("object_path")
            if not relative:
                issues.append(
                    {"code": "E_STATE_CORRUPT", "task_uid": task["task_uid"], "message": "missing object_path"}
                )
                continue
            path = data_dir / relative
            if not path.exists():
                issues.append(
                    {"code": "E_BLOCK_NOT_FOUND", "task_uid": task["task_uid"], "path": str(path)}
                )
                continue
            actual = sha256_file(path)
            checked_objects += 1
            if actual != task["content_digest"]:
                issues.append(
                    {
                        "code": "E_DIGEST_MISMATCH",
                        "task_uid": task["task_uid"],
                        "expected": task["content_digest"],
                        "actual": actual,
                    }
                )
            if task["state"] == "committed":
                decision = conn.execute(
                    "SELECT * FROM inference_decision WHERE task_uid=?", (task["task_uid"],)
                ).fetchone()
                error = conn.execute(
                    "SELECT * FROM error_ledger WHERE decision_id=?",
                    (decision["decision_id"],) if decision else ("",),
                ).fetchone()
                if decision is None or error is None:
                    issues.append(
                        {
                            "code": "E_STATE_CORRUPT",
                            "task_uid": task["task_uid"],
                            "message": "committed task lacks decision or error ledger row",
                        }
                    )
                else:
                    ledger_rows += 1
                    if decision["result_digest"] != task["content_digest"]:
                        issues.append(
                            {
                                "code": "E_DIGEST_MISMATCH",
                                "task_uid": task["task_uid"],
                                "message": "decision result_digest mismatch",
                            }
                        )
            if deep_recompute and task["state"] == "committed":
                try:
                    _data, digest, _decision, _error, _artifact = _build_materialization(conn, task)
                    recomputed += 1
                    if digest != task["content_digest"]:
                        issues.append(
                            {
                                "code": "E_DIGEST_MISMATCH",
                                "task_uid": task["task_uid"],
                                "message": "deterministic recomputation differs",
                                "expected": task["content_digest"],
                                "actual": digest,
                            }
                        )
                except BizError as exc:
                    issues.append(exc.to_dict())

        duplicate_decisions = conn.execute(
            "SELECT task_uid, COUNT(*) AS n FROM inference_decision GROUP BY task_uid HAVING n>1"
        ).fetchall()
        for row in duplicate_decisions:
            issues.append({"code": "E_STATE_CORRUPT", "message": "duplicate decision", "task_uid": row["task_uid"]})

        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM block_task WHERE state='pending'"
        ).fetchone()["n"]
        running = conn.execute(
            "SELECT COUNT(*) AS n FROM block_task WHERE state='running'"
        ).fetchone()["n"]
        materialized = conn.execute(
            "SELECT COUNT(*) AS n FROM block_task WHERE state='materialized'"
        ).fetchone()["n"]
        failed = conn.execute(
            "SELECT COUNT(*) AS n FROM block_task WHERE state='failed'"
        ).fetchone()["n"]
        complete = pending == running == materialized == failed == 0
        return {
            "ok": not issues,
            "complete": complete,
            "source": source_summary,
            "objects_checked": checked_objects,
            "deterministic_recomputations": recomputed,
            "ledger_rows_checked": ledger_rows,
            "incomplete": {
                "pending": pending,
                "running": running,
                "materialized": materialized,
                "failed": failed,
            },
            "batch": {
                "batch_id": batch["batch_id"],
                "config_version": batch["config_version"],
                "decision_count": batch["decision_count"],
                "total_error_bound": batch["total_error_bound"],
                "total_error_units": batch["total_error_units"],
                "precision_sensitive_count": batch["precision_sensitive_count"],
            },
            "issues": issues,
        }
    finally:
        conn.close()
