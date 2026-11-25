from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from .engine import SchedulerEngine
from .errors import FAULT_ERROR_CODES, SchedulerError
from .models import load_bundle
from .storage import SchedulerStore
from .utils import digest_value


def run_verification(
    *,
    db_path: str | Path,
    sample_dir: str | Path,
    self_test: bool = False,
) -> Dict[str, Any]:
    bundle = load_bundle(Path(sample_dir))
    store = SchedulerStore(db_path)
    checks: List[Dict[str, Any]] = []
    report: Dict[str, Any] = {"ok": True, "db_path": str(db_path), "checks": checks}

    db_file = Path(db_path)
    if not db_file.exists():
        report["ok"] = False
        report["error_code"] = "E_LEDGER_DB_MISSING"
        checks.append({"name": "database_exists", "ok": False})
        return report

    chain_check = verify_chain(store)
    checks.append(chain_check)
    report["ok"] &= chain_check["ok"]

    if chain_check.get("ledger_version", 0) > 0:
        invariant_check = verify_snapshot_invariants(store)
        checks.append(invariant_check)
        report["ok"] &= invariant_check["ok"]

        occupancy_check = verify_occupancy_table(store)
        checks.append(occupancy_check)
        report["ok"] &= occupancy_check["ok"]

        idempotency_check = verify_idempotent_replay(store, bundle)
        checks.append(idempotency_check)
        report["ok"] &= idempotency_check["ok"]

        injection_check = verify_injection_isolation(store, bundle)
        checks.append(injection_check)
        # Missing experiment evidence is a verification failure because the
        # delivered sample contract includes all four controlled faults.
        report["ok"] &= injection_check["ok"]
        report["injection_isolation"] = injection_check

    if self_test:
        self_test_check = run_deterministic_self_test(bundle)
        checks.append(self_test_check)
        report["ok"] &= self_test_check["ok"]

    report["status_digest"] = digest_value(SchedulerEngine(store, bundle).status())
    return report


def verify_chain(store: SchedulerStore) -> Dict[str, Any]:
    bad: List[Dict[str, Any]] = []
    versions = 0
    pointer = 0
    with store.connection() as conn:
        pointer = store.current_version(conn)
        rows = conn.execute("SELECT * FROM ledger_versions ORDER BY version").fetchall()
        previous_chain = "0" * 64
        for expected_version, row in enumerate(rows, start=1):
            versions += 1
            if row["version"] != expected_version:
                bad.append({"type": "version_gap", "expected": expected_version, "actual": row["version"]})
            snapshot = json.loads(row["snapshot_json"])
            actual_digest = digest_value(snapshot)
            if actual_digest != row["digest"]:
                bad.append({"type": "snapshot_digest_mismatch", "version": row["version"]})
            expected_chain = digest_value(
                {
                    "parent_chain_digest": previous_chain,
                    "snapshot_digest": row["digest"],
                    "version": row["version"],
                }
            )
            if expected_chain != row["chain_digest"]:
                bad.append({"type": "chain_digest_mismatch", "version": row["version"]})
            if row["parent_version"] != row["version"] - 1:
                bad.append({"type": "parent_version_mismatch", "version": row["version"]})
            previous_chain = row["chain_digest"]

    if pointer != versions:
        bad.append({"type": "pointer_mismatch", "pointer": pointer, "versions": versions})
    return {
        "name": "hash_chain_and_pointer",
        "ok": not bad,
        "ledger_version": pointer,
        "version_count": versions,
        "errors": bad,
    }


def verify_snapshot_invariants(store: SchedulerStore) -> Dict[str, Any]:
    engine = SchedulerEngine(store, {"cards": {}, "jobs": {}, "config": {}, "quotas": {}, "injections": []})
    result = engine.verify_current_invariants()
    return {
        "name": "share_conservation_no_overlap",
        "ok": result["ok"],
        "ledger_version": result["ledger_version"],
        "errors": result["errors"],
    }


def verify_occupancy_table(store: SchedulerStore) -> Dict[str, Any]:
    errors: List[Dict[str, Any]] = []
    with store.connection() as conn:
        version = store.current_version(conn)
        snapshot = store.load_snapshot(conn, version)
        rows = conn.execute(
            "SELECT * FROM occupancy_intervals WHERE ledger_version=?", (version,)
        ).fetchall()
        active_leases = [
            lease for lease in snapshot["leases"].values() if lease.get("status") == "active"
        ]
        expected_rows = len(active_leases) * 2
        if len(rows) != expected_rows:
            errors.append(
                {"type": "occupancy_row_count", "expected": expected_rows, "actual": len(rows)}
            )
        for row in rows:
            lease = snapshot["leases"].get(row["lease_id"])
            if lease is None:
                errors.append({"type": "orphan_occupancy_row", "lease_id": row["lease_id"]})
                continue
            interval_field = "compute_interval" if row["resource"] == "compute" else "memory_interval"
            if [row["start_share"], row["end_share"]] != lease[interval_field]:
                errors.append(
                    {
                        "type": "occupancy_interval_mismatch",
                        "lease_id": row["lease_id"],
                        "resource": row["resource"],
                    }
                )
    return {
        "name": "occupancy_mapping_matches_ledger",
        "ok": not errors,
        "row_count": len(rows) if 'rows' in locals() else 0,
        "errors": errors,
    }


def verify_idempotent_replay(store: SchedulerStore, bundle: Dict[str, Any]) -> Dict[str, Any]:
    engine = SchedulerEngine(store, bundle)
    replayed = 0
    mismatches: List[Dict[str, Any]] = []
    with store.connection() as conn:
        before_version = store.current_version(conn)
        rows = conn.execute("SELECT * FROM idempotent_results ORDER BY rowid").fetchall()
        requests = {
            row["request_key"]: json.loads(row["request_json"])
            for row in conn.execute("SELECT request_key, request_json FROM operations").fetchall()
        }
        stored_results = {row["request_key"]: json.loads(row["result_json"]) for row in rows}

    for request_key, stored_result in stored_results.items():
        request = requests.get(request_key)
        if request is None:
            mismatches.append({"request_key": request_key, "type": "missing_operation_request"})
            continue
        replayed_result = engine.submit(request)
        comparable = {
            key: value
            for key, value in replayed_result.items()
            if key not in {"idempotent_replay", "recovered_applied_operation"}
        }
        if digest_value(comparable) != digest_value(stored_result):
            mismatches.append(
                {
                    "request_key": request_key,
                    "type": "idempotent_result_digest_mismatch",
                    "stored_digest": digest_value(stored_result),
                    "replayed_digest": digest_value(comparable),
                }
            )
        else:
            replayed += 1

    with store.connection() as conn:
        after_version = store.current_version(conn)
    if after_version != before_version:
        mismatches.append(
            {"type": "version_changed_during_replay", "before": before_version, "after": after_version}
        )
    return {
        "name": "idempotent_request_replay",
        "ok": not mismatches,
        "replayed": replayed,
        "version": before_version,
        "errors": mismatches,
    }


def verify_injection_isolation(store: SchedulerStore, bundle: Dict[str, Any]) -> Dict[str, Any]:
    expected = {item["injection_id"]: item for item in bundle["injections"] if item.get("enabled", True)}
    observed: Dict[str, Any] = {}
    errors: List[Dict[str, Any]] = []
    with store.connection() as conn:
        rows = conn.execute("SELECT * FROM injection_runs").fetchall()
        for row in rows:
            observed[row["injection_id"]] = dict(row)
        for injection_id, injection in expected.items():
            row = observed.get(injection_id)
            expected_code = FAULT_ERROR_CODES[injection["fault_type"]]
            if row is None:
                errors.append(
                    {"injection_id": injection_id, "type": "missing_injection_run", "expected_code": expected_code}
                )
                continue
            if row["error_code"] != expected_code:
                errors.append(
                    {
                        "injection_id": injection_id,
                        "type": "error_code_mismatch",
                        "expected": expected_code,
                        "actual": row["error_code"],
                    }
                )
            op = conn.execute(
                "SELECT status, ledger_version FROM operations WHERE round_id=? AND action IN ('grant','renew','recover','reorganize','preempt_execute')",
                (injection["target"].get("round_id"),),
            ).fetchone()
            if op is not None and (op["status"] != "failed" or op["ledger_version"] is not None):
                errors.append(
                    {
                        "injection_id": injection_id,
                        "type": "fault_created_ledger_version",
                        "operation_status": op["status"],
                        "ledger_version": op["ledger_version"],
                    }
                )
    return {
        "name": "controlled_fault_isolation",
        "ok": not errors,
        "expected_count": len(expected),
        "observed_count": len(observed),
        "observed_error_codes": {key: row["error_code"] for key, row in observed.items()},
        "errors": errors,
    }


def run_deterministic_self_test(bundle: Dict[str, Any]) -> Dict[str, Any]:
    digests: List[str] = []
    invariant_ok = True
    errors: List[Dict[str, Any]] = []
    try:
        for suffix in ("a", "b"):
            with tempfile.TemporaryDirectory(prefix=f"gpu-verify-{suffix}-") as tmp:
                db_path = Path(tmp) / "self.db"
                engine = SchedulerEngine(SchedulerStore(db_path), bundle)
                engine.initialize_bundle()
                _canonical_core_chain(engine)
                status = engine.status()
                digests.append(digest_value(status))
                invariant_ok &= engine.verify_current_invariants()["ok"]
        if digests[0] != digests[1]:
            errors.append({"type": "deterministic_status_digest_mismatch", "digests": digests})
    except SchedulerError as exc:
        errors.append(exc.to_dict())
    return {
        "name": "fresh_db_deterministic_replay",
        "ok": not errors and invariant_ok and len(set(digests)) == 1,
        "status_digests": digests,
        "errors": errors,
    }


def _canonical_core_chain(engine: SchedulerEngine) -> None:
    for index in range(1, 6):
        engine.submit(
            {
                "action": "grant",
                "stage": "grant",
                "round_id": f"self-grant-{index}",
                "now_ms": 100,
                "job_id": f"job-frag-{index:02d}",
            }
        )
    engine.submit(
        {
            "action": "renew",
            "stage": "renew",
            "round_id": "self-renew",
            "now_ms": 150,
            "job_id": "job-frag-01",
            "generation": 1,
        }
    )
    engine.submit(
        {
            "action": "recover",
            "stage": "recover",
            "round_id": "self-recover",
            "now_ms": 300,
            "reason": "normal_expiry",
        }
    )
    engine.submit(
        {
            "action": "reorganize",
            "stage": "reorganize",
            "round_id": "self-reorg",
            "now_ms": 300,
            "job_id": "job-frag-target",
            "card_id": "gpu-frag",
        }
    )
