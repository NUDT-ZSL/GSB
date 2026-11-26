from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from .hashing import sha256_object


def verify_output_directory(out_dir: str | Path) -> dict[str, Any]:
    out = Path(out_dir)
    summary_path = out / "run_summary.json"
    if not summary_path.exists():
        return {"ok": False, "error_code": "CAP-1000-INPUT-NOT-FOUND", "message": "run_summary.json missing"}
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    db_path = out / "capacity_state.sqlite"
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, details: Any = None) -> None:
        checks.append({"name": name, "ok": bool(ok), "details": details})

    with sqlite3.connect(db_path) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        add("sqlite_integrity", integrity == "ok", integrity)
        revisions = [
            row[0]
            for row in conn.execute("SELECT revision_id FROM revisions WHERE state='committed' ORDER BY rowid")
        ]
        add("all_summary_revisions_committed", len(revisions) == summary["revision_count"], revisions)
        for revision_id in revisions:
            duplicate_seqs = conn.execute(
                """
                SELECT seq, COUNT(*) AS count
                  FROM ledger_entries
                 WHERE revision_id=?
                 GROUP BY seq
                HAVING COUNT(*) > 1
                """,
                (revision_id,),
            ).fetchall()
            add(f"ledger_seq_unique:{revision_id}", not duplicate_seqs, duplicate_seqs)
            report_row = conn.execute(
                "SELECT report_json, report_hash FROM revision_reports WHERE revision_id=?",
                (revision_id,),
            ).fetchone()
            report = json.loads(report_row[0])
            expected_hash = sha256_object({k: v for k, v in report.items() if k != "report_digest"})
            add(f"report_digest:{revision_id}", expected_hash == report["report_digest"] == report_row[1])
            failed_reuse = [
                check
                for check in report.get("reuse_checks", [])
                if not check["unchanged"]
            ]
            add(f"reuse_digests:{revision_id}", not failed_reuse, failed_reuse)
            violations = []
            for value in report["evaluation"]["values"]:
                from decimal import Decimal

                if Decimal(value["absolute_deviation"]) > Decimal(value["error_upper_bound_physical"]) + Decimal("1e-48"):
                    violations.append(value["obs_key"])
            add(f"precision_budget:{revision_id}", not violations, violations)

    all_ok = all(check["ok"] for check in checks)
    return {"ok": all_ok, "checks": checks, "run_summary": summary}


def compare_directories(first: str | Path, second: str | Path) -> dict[str, Any]:
    first = Path(first)
    second = Path(second)
    mismatches: list[str] = []
    compared: list[str] = []
    for path in sorted((first / "reports").glob("*.json")):
        other = second / "reports" / path.name
        if not other.exists():
            mismatches.append(f"missing {path.name}")
            continue
        left = json.loads(path.read_text(encoding="utf-8"))
        right = json.loads(other.read_text(encoding="utf-8"))
        if left != right:
            mismatches.append(path.name)
        compared.append(path.name)
    first_summary = json.loads((first / "run_summary.json").read_text())
    second_summary = json.loads((second / "run_summary.json").read_text())
    if first_summary != second_summary:
        mismatches.append("run_summary.json")
    return {
        "ok": not mismatches,
        "compared_reports": compared,
        "mismatches": mismatches,
        "first_final_digest": first_summary.get("revisions", [{}])[-1].get("report_digest"),
        "second_final_digest": second_summary.get("revisions", [{}])[-1].get("report_digest"),
    }
