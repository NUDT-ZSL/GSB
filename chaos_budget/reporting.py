from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from .errors import ChaosError
from .storage import Database
from .utils import sha256_json


def _decision_summary(conn: sqlite3.Connection, experiment_id: str) -> Dict[str, Any]:
    count_row = conn.execute(
        "SELECT COUNT(*) AS count, MAX(decision_seq) AS latest_seq FROM decisions WHERE experiment_id=?",
        (experiment_id,),
    ).fetchone()
    latest = None
    if count_row["latest_seq"] is not None:
        latest = conn.execute(
            """
            SELECT decision_seq, action, result, constraint_code, evidence_hash
              FROM decisions WHERE decision_seq=?
            """,
            (count_row["latest_seq"],),
        ).fetchone()
    return {
        "decision_count": int(count_row["count"]),
        "latest_decision_seq": count_row["latest_seq"],
        "latest_action": latest["action"] if latest else None,
        "latest_result": latest["result"] if latest else None,
        "first_constraint_code": latest["constraint_code"] if latest else None,
        "latest_evidence_hash": latest["evidence_hash"] if latest else None,
    }


def build_database_status(db: Database) -> Dict[str, Any]:
    if db.conn is None:
        db.connect()
    conn = db.conn
    assert conn is not None

    watermark = db.watermark()
    config = json.loads(db.meta("budget_config_json", "{}"))
    experiments: List[Dict[str, Any]] = []
    account_rows = conn.execute("SELECT * FROM budget_accounts ORDER BY account_id").fetchall()
    accounts_by_owner: Dict[str, Dict[str, Any]] = {}
    round_accounts: List[Dict[str, Any]] = []
    for row in account_rows:
        account = dict(row)
        if account["account_type"] == "experiment":
            accounts_by_owner[account["owner_id"]] = account
        else:
            round_accounts.append(account)

    exp_rows = conn.execute("SELECT * FROM experiments ORDER BY experiment_id ASC").fetchall()
    state_counts: Dict[str, int] = {}
    outcome_counts: Dict[str, int] = {}
    for row in exp_rows:
        exp = dict(row)
        state_counts[exp["state"]] = state_counts.get(exp["state"], 0) + 1
        outcome_counts[exp["outcome_category"]] = outcome_counts.get(exp["outcome_category"], 0) + 1
        account = accounts_by_owner.get(exp["experiment_id"], {})
        experiments.append(
            {
                "experiment_id": exp["experiment_id"],
                "state": exp["state"],
                "disposition": exp["disposition"],
                "outcome_category": exp["outcome_category"],
                "priority": exp["priority"],
                "start_clock_ms": exp["start_clock_ms"],
                "generation": exp["generation"],
                "consecutive_failures": exp["consecutive_failures"],
                "backoff_until_ms": exp["backoff_until_ms"],
                "first_constraint_code": exp["first_constraint_code"],
                "remaining_budget_points": account.get("balance"),
                "budget_account": {
                    "account_id": account.get("account_id"),
                    "initial_balance": account.get("initial_balance"),
                    "balance": account.get("balance"),
                    "total_debited": account.get("total_debited"),
                    "total_credited": account.get("total_credited"),
                },
                "decision_summary": _decision_summary(conn, exp["experiment_id"]),
            }
        )

    policy_rows = conn.execute("SELECT policy_key, selected_json FROM policy_view ORDER BY policy_key").fetchall()
    policies = {row["policy_key"]: json.loads(row["selected_json"]) for row in policy_rows}
    inbox_row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN status='received' THEN 1 ELSE 0 END) AS received,
          SUM(CASE WHEN status='processed' THEN 1 ELSE 0 END) AS processed,
          SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected
        FROM observation_inbox
        """
    ).fetchone()
    decision_count = conn.execute("SELECT COUNT(*) AS count FROM decisions").fetchone()["count"]

    return {
        "ok": True,
        "persisted": True,
        "db_path": str(db.path),
        "bundle_fingerprint": db.meta("bundle_fingerprint"),
        "clock_watermark_ms": watermark,
        "budget_config": config,
        "state_counts": dict(sorted(state_counts.items())),
        "outcome_counts": dict(sorted(outcome_counts.items())),
        "experiments": experiments,
        "round_accounts": round_accounts,
        "policy_view": policies,
        "inbox_counts": {
            "received": int(inbox_row["received"] or 0),
            "processed": int(inbox_row["processed"] or 0),
            "rejected": int(inbox_row["rejected"] or 0),
        },
        "decision_count": int(decision_count),
    }


def build_baseline_status(bundle: Dict[str, Any]) -> Dict[str, Any]:
    config = bundle["budget_config"]
    experiments: List[Dict[str, Any]] = []
    for exp in sorted(bundle["experiments"], key=lambda item: item["experiment_id"]):
        experiments.append(
            {
                "experiment_id": exp["experiment_id"],
                "state": "pending_admission",
                "disposition": "budgeted_not_started",
                "outcome_category": "within_budget_not_started",
                "priority": exp["priority"],
                "start_clock_ms": exp["start_clock_ms"],
                "generation": 1,
                "consecutive_failures": 0,
                "backoff_until_ms": 0,
                "first_constraint_code": None,
                "remaining_budget_points": config["per_experiment_budget"],
                "budget_account": {
                    "account_id": f"experiment:{exp['experiment_id']}:points",
                    "initial_balance": config["per_experiment_budget"],
                    "balance": config["per_experiment_budget"],
                    "total_debited": 0,
                    "total_credited": 0,
                },
                "decision_summary": {
                    "decision_count": 0,
                    "latest_decision_seq": None,
                    "latest_action": None,
                    "latest_result": None,
                    "first_constraint_code": None,
                    "latest_evidence_hash": None,
                },
            }
        )
    rounds = sorted({exp["round_id"] for exp in bundle["experiments"]})
    round_accounts = [
        {
            "account_id": f"round:{round_id}:logical_time_ms",
            "initial_balance": config["round_logical_time_budget_ms"],
            "balance": config["round_logical_time_budget_ms"],
            "round_id": round_id,
            "currency": "logical_ms",
        }
        for round_id in rounds
    ]
    return {
        "ok": True,
        "persisted": False,
        "bundle_fingerprint": bundle["fingerprint"],
        "clock_watermark_ms": bundle["injection_clock_ms"],
        "budget_config": config,
        "state_counts": {"pending_admission": len(experiments)},
        "outcome_counts": {"within_budget_not_started": len(experiments)},
        "experiments": experiments,
        "round_accounts": round_accounts,
        "policy_view": bundle["policy_view"]["policies"],
        "inbox_counts": {"received": 0, "processed": 0, "rejected": 0},
        "decision_count": 0,
    }


def verify_database(db: Database, expected_fingerprint: Optional[str] = None) -> Dict[str, Any]:
    if db.conn is None:
        db.connect()
    conn = db.conn
    assert conn is not None
    checks: List[Dict[str, Any]] = []

    def add_check(name: str, passed: bool, details: Any = None) -> bool:
        checks.append({"name": name, "passed": passed, "details": details})
        return passed

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    all_passed = add_check("sqlite_integrity", integrity == "ok", {"integrity_check": integrity})

    fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    all_passed = add_check("foreign_key_integrity", len(fk_violations) == 0, {"violations": len(fk_violations)}) and all_passed

    negative_rows = conn.execute(
        "SELECT account_id, balance FROM budget_accounts WHERE balance < 0"
    ).fetchall()
    all_passed = add_check("no_negative_balance", len(negative_rows) == 0, {"negative_accounts": len(negative_rows)}) and all_passed

    account_mismatches: List[Dict[str, Any]] = []
    total_initial = total_balance = total_debited = total_credited = 0
    account_rows = conn.execute("SELECT * FROM budget_accounts ORDER BY account_id").fetchall()
    for row in account_rows:
        account = dict(row)
        ledger = conn.execute(
            """
            SELECT COALESCE(SUM(delta_amount), 0) AS delta_sum,
                   COALESCE(SUM(CASE WHEN delta_amount < 0 THEN -delta_amount ELSE 0 END), 0) AS debited,
                   COALESCE(SUM(CASE WHEN delta_amount > 0 THEN delta_amount ELSE 0 END), 0) AS credited
              FROM budget_ledger WHERE account_id=?
            """,
            (account["account_id"],),
        ).fetchone()
        ledger = dict(ledger)
        identity_balance = int(account["initial_balance"]) + int(ledger["delta_sum"])
        totals_match = (
            identity_balance == int(account["balance"])
            and int(ledger["debited"]) == int(account["total_debited"])
            and int(ledger["credited"]) == int(account["total_credited"])
        )
        if not totals_match:
            account_mismatches.append(
                {
                    "account_id": account["account_id"],
                    "stored_balance": account["balance"],
                    "ledger_identity_balance": identity_balance,
                    "stored_debited": account["total_debited"],
                    "ledger_debited": ledger["debited"],
                    "stored_credited": account["total_credited"],
                    "ledger_credited": ledger["credited"],
                }
            )
        total_initial += int(account["initial_balance"])
        total_balance += int(account["balance"])
        total_debited += int(account["total_debited"])
        total_credited += int(account["total_credited"])
    all_passed = add_check(
        "budget_ledger_conservation",
        not account_mismatches,
        {
            "mismatches": account_mismatches,
            "total_initial": total_initial,
            "total_debited": total_debited,
            "total_credited": total_credited,
            "total_balance": total_balance,
            "identity": "total_balance = total_initial - total_debited + total_credited",
            "conserved": total_balance == total_initial - total_debited + total_credited,
        },
    ) and all_passed

    decisions = conn.execute("SELECT * FROM decisions ORDER BY decision_seq ASC").fetchall()
    chain_errors: List[Dict[str, Any]] = []
    previous_hash: Optional[str] = None
    expected_seq = 1
    for row in decisions:
        decision = dict(row)
        if decision["decision_seq"] != expected_seq:
            chain_errors.append({"decision_seq": decision["decision_seq"], "problem": "sequence_gap", "expected_seq": expected_seq})
        if decision["prev_hash"] != previous_hash:
            chain_errors.append({"decision_seq": decision["decision_seq"], "problem": "prev_hash_mismatch"})
        evidence = json.loads(decision["evidence_json"])
        stored_hash = evidence.pop("evidence_hash", None)
        recomputed = sha256_json(evidence)
        if stored_hash != decision["evidence_hash"] or recomputed != decision["evidence_hash"]:
            chain_errors.append({"decision_seq": decision["decision_seq"], "problem": "evidence_hash_mismatch"})
        previous_hash = decision["evidence_hash"]
        expected_seq += 1
    all_passed = add_check(
        "decision_hash_chain",
        not chain_errors,
        {"decision_count": len(decisions), "errors": chain_errors[:10]},
    ) and all_passed

    orphan_ledger = conn.execute(
        """
        SELECT l.ledger_seq FROM budget_ledger l
         LEFT JOIN decisions d ON d.decision_seq=l.decision_seq
        WHERE d.decision_seq IS NULL
        """
    ).fetchall()
    all_passed = add_check("ledger_decision_foreign_key", len(orphan_ledger) == 0, {"orphan_count": len(orphan_ledger)}) and all_passed

    duplicate_admissions = conn.execute(
        """
        SELECT experiment_id, generation, COUNT(*) AS count
          FROM admissions GROUP BY experiment_id, generation HAVING COUNT(*) > 1
        """
    ).fetchall()
    all_passed = add_check("admission_version_uniqueness", len(duplicate_admissions) == 0, {"duplicates": len(duplicate_admissions)}) and all_passed

    pending_inbox = conn.execute(
        "SELECT experiment_id, sequence FROM observation_inbox WHERE status='received' ORDER BY experiment_id, sequence"
    ).fetchall()
    all_passed = add_check(
        "no_unresolved_inbox",
        len(pending_inbox) == 0,
        {"pending": [dict(row) for row in pending_inbox]},
    ) and all_passed

    fingerprint = db.meta("bundle_fingerprint")
    fingerprint_ok = expected_fingerprint is None or fingerprint == expected_fingerprint
    all_passed = add_check(
        "input_fingerprint",
        fingerprint_ok,
        {"stored": fingerprint, "expected": expected_fingerprint},
    ) and all_passed

    state_rows = conn.execute("SELECT state, COUNT(*) AS count FROM experiments GROUP BY state ORDER BY state").fetchall()
    return {
        "ok": all_passed,
        "checks": checks,
        "summary": {
            "experiment_states": {row["state"]: row["count"] for row in state_rows},
            "decision_count": len(decisions),
            "account_count": len(account_rows),
            "total_initial_balance": total_initial,
            "total_debited": total_debited,
            "total_credited": total_credited,
            "total_balance": total_balance,
            "clock_watermark_ms": db.watermark(),
        },
    }


def assert_verified(report: Dict[str, Any]) -> None:
    if not report.get("ok"):
        failed = [check["name"] for check in report.get("checks", []) if not check["passed"]]
        raise ChaosError("VERIFICATION_FAILED", "consistency verification failed", {"failed_checks": failed}, exit_code=6)
