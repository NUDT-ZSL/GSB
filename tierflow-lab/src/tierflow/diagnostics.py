from __future__ import annotations

import json
import sqlite3
from fractions import Fraction
from pathlib import Path
from typing import Any

from .errors import VerifyError
from .storage import StateStore
from .validation import validate_config, validate_events, validate_manifest


def add_check(checks: list[dict[str, Any]], name: str, ok: bool, details: dict[str, Any] | None = None) -> None:
    checks.append({"name": name, "ok": bool(ok), "details": details or {}})


def verify_project(samples_dir: str | Path, db_path: str | Path) -> dict[str, Any]:
    samples = Path(samples_dir)
    db_path = Path(db_path)
    checks: list[dict[str, Any]] = []
    input_summary: dict[str, Any] = {}

    manifest = validate_manifest(samples / "manifest.json")
    add_check(checks, "manifest_digests_and_byte_counts", True, {"objects": len(manifest["objects"])})
    config = validate_config(samples / "config.json")
    add_check(checks, "tier_config", True, config)
    events = validate_events(manifest, samples / "migration_events.json")
    add_check(checks, "migration_event_sequence", True, {"events": events["event_count"]})
    input_summary = {
        "object_count": len(manifest["objects"]),
        "event_count": events["event_count"],
    }

    if not db_path.exists():
        add_check(checks, "sqlite_state_exists", False, {"db_path": str(db_path)})
        return {"ok": False, "checks": checks, "input_summary": input_summary}

    store = StateStore(db_path)
    try:
        integrity = store.conn.execute("PRAGMA integrity_check").fetchone()[0]
        add_check(checks, "sqlite_integrity", integrity == "ok", {"integrity_check": integrity})
        fk_violations = [dict(row) for row in store.conn.execute("PRAGMA foreign_key_check")]
        add_check(checks, "foreign_keys", not fk_violations, {"violations": fk_violations})

        objects = store.get_objects()
        manifest_by_key = {obj["key"]: obj for obj in manifest["objects"]}
        static_mismatches: list[str] = []
        for obj in objects:
            source = manifest_by_key.get(obj["object_key"])
            if source is None:
                static_mismatches.append(f"missing manifest object {obj['object_key']}")
                continue
            if int(obj["size_bytes"]) != int(source["size_bytes"]):
                static_mismatches.append(f"size mismatch {obj['object_key']}")
            if obj["content_digest"] != source["content_digest"]:
                static_mismatches.append(f"digest mismatch {obj['object_key']}")
        add_check(checks, "state_objects_match_manifest", not static_mismatches, {"mismatches": static_mismatches})

        balance_errors: list[str] = []
        for account in store.accounts():
            tier = account["tier"]
            expected_used = sum(int(obj["size_bytes"]) for obj in objects if obj["current_tier"] == tier)
            active_reserved = store.conn.execute(
                """
                SELECT COALESCE(SUM(j.size_bytes), 0) AS total
                FROM migration_jobs j
                WHERE j.target_tier = ? AND j.status IN ('claimed', 'running', 'committing')
                """,
                (tier,),
            ).fetchone()["total"]
            if int(account["used_bytes"]) != expected_used:
                balance_errors.append(
                    f"{tier}: used={account['used_bytes']} expected={expected_used}"
                )
            if int(account["reserved_bytes"]) != int(active_reserved):
                balance_errors.append(
                    f"{tier}: reserved={account['reserved_bytes']} expected_active_jobs={active_reserved}"
                )
            if int(account["used_bytes"]) + int(account["reserved_bytes"]) > int(account["capacity_bytes"]):
                balance_errors.append(f"{tier}: capacity overcommitted")
        add_check(checks, "capacity_accounts", not balance_errors, {"errors": balance_errors, "accounts": store.accounts()})

        duplicate_rows = store.conn.execute(
            """
            SELECT round_id, policy_version, object_key, COUNT(*) AS count
            FROM migration_jobs
            GROUP BY round_id, policy_version, object_key
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        add_check(checks, "same_object_round_version_unique", not duplicate_rows, {"duplicates": [dict(r) for r in duplicate_rows]})

        succeeded_count = store.conn.execute(
            "SELECT COUNT(*) AS count FROM migration_jobs WHERE status='succeeded'"
        ).fetchone()["count"]
        watermarks = store.watermarks()
        committed_watermark = int(watermarks.get("migration_committed", 0))
        add_check(
            checks,
            "migration_watermark_matches_committed_jobs",
            committed_watermark == int(succeeded_count),
            {"watermark": committed_watermark, "succeeded_jobs": succeeded_count},
        )
        event_watermark = int(watermarks.get("event_seq", -1))
        max_event_seq = store.conn.execute("SELECT COALESCE(MAX(seq), -1) AS seq FROM events").fetchone()["seq"]
        add_check(
            checks,
            "event_watermark",
            event_watermark == int(max_event_seq),
            {"watermark": event_watermark, "max_seq": max_event_seq},
        )

        cost_errors: list[str] = []
        cost_rows = store.conn.execute("SELECT * FROM cost_ledgers").fetchall()
        for row in cost_rows:
            ledger = json.loads(row["ledger_json"])
            if int(ledger["total_cost_scaled"]) != int(row["total_cost_scaled"]):
                cost_errors.append(f"cost total mismatch for {row['ledger_key']}")
            Fraction(
                int(ledger["error_bound_numerator"]),
                int(ledger["error_bound_denominator"]),
            )
        add_check(checks, "cost_ledgers_parse", not cost_errors, {"errors": cost_errors, "ledgers": len(cost_rows)})

        policy = store.current_policy()
        if policy is None:
            add_check(checks, "policy_pointer", False, {"reason": "no current policy"})
        else:
            entry_count = store.conn.execute(
                "SELECT COUNT(*) AS count FROM policy_entries WHERE manifest_version=?",
                (policy["manifest_version"],),
            ).fetchone()["count"]
            add_check(
                checks,
                "policy_pointer_and_snapshot",
                entry_count > 0 and bool(policy["snapshot_digest"]),
                {
                    "manifest_version": policy["manifest_version"],
                    "generation": policy["generation"],
                    "decision": policy["decision"],
                    "entries": entry_count,
                },
            )

        clock_row = store.conn.execute("SELECT clock_ms FROM clock_watermark WHERE id=1").fetchone()
        add_check(
            checks,
            "clock_watermark",
            clock_row is not None and int(clock_row["clock_ms"]) >= 0,
            {"clock_ms": None if clock_row is None else clock_row["clock_ms"]},
        )
    finally:
        store.close()

    ok = all(check["ok"] for check in checks)
    result = {"ok": ok, "checks": checks, "input_summary": input_summary}
    if not ok:
        raise VerifyError("verification failed", {"report": result})
    return result
