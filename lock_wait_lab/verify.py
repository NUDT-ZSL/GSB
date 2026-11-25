"""Repeatable ledger, policy-pointer, determinism and recovery verification."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from .canonical import convert_duration, sha256_value
from .engine import CrashInjected, Engine
from .errors import ErrorCode, LabError
from .samples import SampleBundle
from .store import Store


def _check(name: str, ok: bool, **details: Any) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "details": details}


def _decision_digest(decisions: list[dict[str, Any]]) -> str:
    return sha256_value(
        [
            {
                "decision_seq": decision.get("decision_seq"),
                "tx_id": decision.get("tx_id"),
                "status": decision.get("status"),
                "reason": decision.get("reason"),
                "logical_ns": decision.get("logical_ns"),
                "policy_generation": decision.get("policy_generation"),
            }
            for decision in decisions
        ]
    )


def _ledger_digest(entries: list[dict[str, Any]]) -> str:
    keys = (
        "entry_id",
        "tx_id",
        "op_id",
        "resource",
        "mode",
        "enqueue_ns",
        "dequeue_ns",
        "duration_ns",
        "scale",
        "rounded_ms",
        "rounding_direction",
        "discarded_remainder_ns",
        "residual_ns",
        "own_error_bound_ns",
        "dependency_error_bound_ns",
        "cumulative_error_bound_ns",
        "status",
        "policy_generation",
    )
    return sha256_value([{key: entry.get(key) for key in keys} for entry in entries])


def _verify_ledger(
    state: dict[str, Any],
    store: Store,
    run_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    entries = state["ledger_entries"]
    store_entries = store.ledger_rows(run_id)
    active_version = int(state["policy"]["ledger_version"])
    active_store_entries = [
        row for row in store_entries if row["ledger_version"] == active_version
    ]
    checks.append(
        _check(
            "ledger_state_matches_sqlite",
            len(entries) == len(active_store_entries)
            and [
                (
                    entry["entry_id"],
                    entry.get("duration_ns"),
                    entry["status"],
                )
                for entry in entries
            ]
            == [
                (
                    row["entry_id"],
                    row["duration_ns"],
                    row["status"],
                )
                for row in active_store_entries
            ],
            state_entries=len(entries),
            sqlite_entries=len(active_store_entries),
        )
    )
    all_closed = all(entry.get("dequeue_ns") is not None for entry in entries)
    checks.append(_check("ledger_entries_closed", all_closed))
    recalculated = True
    mismatch: dict[str, Any] | None = None
    for entry in entries:
        if entry.get("dequeue_ns") is None:
            recalculated = False
            mismatch = {"entry_id": entry["entry_id"], "reason": "open"}
            break
        duration = entry["dequeue_ns"] - entry["enqueue_ns"]
        fixed = convert_duration(duration, entry["scale"])
        if (
            duration != entry["duration_ns"]
            or fixed.rounded_ms != entry["rounded_ms"]
            or fixed.direction != entry["rounding_direction"]
            or fixed.discarded_remainder_ns != entry["discarded_remainder_ns"]
            or (
                fixed.error_bound_ns + entry["dependency_error_bound_ns"]
                != entry["cumulative_error_bound_ns"]
            )
        ):
            recalculated = False
            mismatch = {
                "entry_id": entry["entry_id"],
                "expected_duration_ns": duration,
                "stored_duration_ns": entry.get("duration_ns"),
            }
            break
    checks.append(
        _check(
            "fixed_point_recalculation",
            recalculated,
            mismatch=mismatch,
        )
    )
    open_entries = [entry for entry in entries if entry.get("dequeue_ns") is None]
    checks.append(
        _check(
            "no_open_ledger_at_completion",
            not open_entries,
            open_entry_ids=[entry["entry_id"] for entry in open_entries],
        )
    )
    total_wait_ns = sum(int(entry.get("duration_ns") or 0) for entry in entries)
    max_error = max(
        (int(entry.get("cumulative_error_bound_ns") or 0) for entry in entries),
        default=0,
    )
    return checks, {
        "entry_count": len(entries),
        "total_wait_ns": total_wait_ns,
        "max_cumulative_error_bound_ns": max_error,
        "ledger_digest": _ledger_digest(entries),
    }


def _verify_policy(
    bundle: SampleBundle,
    state: dict[str, Any],
    store: Store,
    run_id: str,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    status = store.fetch_status(run_id)
    assert status is not None
    active = status["active_policy"]
    policy = state["policy"]
    checks.append(
        _check(
            "active_policy_pointer",
            active is not None
            and active["generation"] == policy["generation"]
            and active["ledger_version"] == policy["ledger_version"],
            pointer=active,
            state={
                "generation": policy["generation"],
                "ledger_version": policy["ledger_version"],
            },
        )
    )
    versions = status["ledger_versions"]
    expected_versions = list(range(1, int(policy["ledger_version"]) + 1))
    checks.append(
        _check(
            "ledger_version_chain",
            [row["version"] for row in versions] == expected_versions,
            expected=expected_versions,
            actual=[row["version"] for row in versions],
        )
    )
    commits = state["policy"]["switches"]
    commits_valid = True
    previous_generation = (
        bundle.policy_views[min(bundle.policy_views)].generation
        if commits
        else int(policy["generation"])
    )
    previous_version = 1 if commits else int(policy["ledger_version"])
    for commit in sorted(commits, key=lambda item: item["commit_seq"]):
        if (
            commit["from_generation"] != previous_generation
            or commit["to_generation"] <= commit["from_generation"]
            or commit["ledger_version"] != previous_version + 1
        ):
            commits_valid = False
            break
        previous_generation = commit["to_generation"]
        previous_version = commit["ledger_version"]
    checks.append(
        _check(
            "policy_commit_monotonicity",
            commits_valid,
            commits=commits,
        )
    )
    bound_ok = True
    for tx in state["txs"].values():
        generation = tx.get("bound_generation")
        if generation is not None and generation not in bundle.policy_views:
            bound_ok = False
            break
    checks.append(_check("transaction_policy_bindings_valid", bound_ok))
    summaries_ok = all(
        bundle.policy(view.generation).summary == view.summary
        for view in bundle.policy_views.values()
    )
    checks.append(_check("policy_summaries_recomputed", summaries_ok))
    return checks


def _fresh_run_digest(
    bundle: SampleBundle,
    db_path: Path,
    *,
    parallelism: int,
    round_budget_ms: int,
    max_logical_ms: int,
) -> dict[str, Any]:
    store = Store(db_path)
    try:
        engine = Engine.initialize(
            bundle,
            store,
            run_id="verify",
            reset=True,
            parallelism=parallelism,
            round_budget_ms=round_budget_ms,
            max_logical_ms=max_logical_ms,
        )
        summary = engine.run()
        return summary
    finally:
        store.close()


def _recovery_probe(
    bundle: SampleBundle,
    db_path: Path,
    crash_step: int,
    *,
    parallelism: int,
    round_budget_ms: int,
    max_logical_ms: int,
) -> dict[str, Any]:
    store = Store(db_path)
    engine = Engine.initialize(
        bundle,
        store,
        run_id="probe",
        reset=True,
        parallelism=parallelism,
        round_budget_ms=round_budget_ms,
        max_logical_ms=max_logical_ms,
    )
    persisted_step = 0
    try:
        try:
            engine.run(crash_after_step=crash_step, simulate_crash=True)
        except CrashInjected as exc:
            persisted_step = exc.step
    finally:
        store.close()
    store = Store(db_path)
    try:
        engine = Engine(bundle, store, "probe")
        resumed_from_step = engine.state["step"]
        engine.mark_resumed()
        summary = engine.run()
        journal = store.journal("probe")
        return {
            "crash_after_step": crash_step,
            "persisted_step": persisted_step,
            "resumed_from_step": resumed_from_step,
            "journal_entries": len(journal),
            "journal_contiguous": [row["step"] for row in journal]
            == list(range(len(journal))),
            "resume_count": engine.state["resume_count"],
            "state_digest": summary["state_digest"],
            "ledger_digest": summary["ledger_digest"],
            "decision_digest": _decision_digest(summary["decisions"]),
        }
    finally:
        store.close()


def verify_run(
    bundle: SampleBundle,
    db_path: Path,
    run_id: str = "main",
    *,
    run_recovery: bool = True,
) -> dict[str, Any]:
    store = Store(db_path)
    try:
        run = store.get_run(run_id)
        if run is None:
            raise LabError(
                ErrorCode.STATE_MISSING,
                f"run {run_id!r} has not been initialized",
                field="run_id",
                path=str(db_path),
            )
        state = store.load_state(run_id)
        engine = Engine(bundle, store, run_id)
        persisted_summary = engine.summary()
        journal = store.journal(run_id)
        checks: list[dict[str, Any]] = []
        checks.append(
            _check(
                "run_completed",
                state["run_status"] == "COMPLETED",
                actual=state["run_status"],
            )
        )
        contiguous = [row["step"] for row in journal] == list(range(len(journal)))
        checks.append(
            _check(
                "journal_contiguous",
                contiguous,
                journal_entries=len(journal),
                first_step=journal[0]["step"] if journal else None,
                last_step=journal[-1]["step"] if journal else None,
            )
        )
        checks.append(
            _check(
                "snapshot_bundle_fingerprint",
                state["bundle_fingerprint"] == bundle.fingerprint,
                state_fingerprint=state["bundle_fingerprint"],
                sample_fingerprint=bundle.fingerprint,
            )
        )
        ledger_checks, ledger_stats = _verify_ledger(state, store, run_id)
        checks.extend(ledger_checks)
        checks.extend(_verify_policy(bundle, state, store, run_id))
        decisions = state["decisions"]
        decision_sequence_ok = [
            decision["decision_seq"] for decision in decisions
        ] == list(range(1, len(decisions) + 1))
        checks.append(
            _check(
                "decision_sequence_contiguous",
                decision_sequence_ok,
                decision_count=len(decisions),
            )
        )
        final_tx = {
            tx["tx_id"]: tx["final_status"] for tx in state["txs"].values()
        }
        expected_terminal = all(
            status in {
                "COMMITTED",
                "DEADLOCK_VICTIM_ROLLBACK",
                "ABANDONED_WAIT_TIMEOUT",
                "MANUAL_ROLLBACK",
                "NOT_STARTED_WITHIN_BUDGET",
                "ABANDONED_AT_HORIZON",
                "ACTIVE_ROLLBACK_AT_HORIZON",
            }
            for status in final_tx.values()
        )
        checks.append(
            _check(
                "all_transactions_terminal",
                expected_terminal,
                transactions=final_tx,
            )
        )
        checks.append(
            _check(
                "no_locks_after_completion",
                not state["locks"] and not state["queues"],
                locks=len(state["locks"]),
                wait_queues=len(state["queues"]),
            )
        )
        sqlite_txs = store.tx_rows(run_id)
        checks.append(
            _check(
                "normalized_transaction_snapshot",
                [tx["tx_id"] for tx in sqlite_txs]
                == [
                    tx["tx_id"]
                    for tx in sorted(
                        state["txs"].values(),
                        key=lambda item: (item["script_index"], item["tx_id"]),
                    )
                ]
                and all(
                    sqlite_tx["final_status"] == state["txs"][sqlite_tx["tx_id"]][
                        "final_status"
                    ]
                    for sqlite_tx in sqlite_txs
                ),
                transaction_count=len(sqlite_txs),
            )
        )
        sqlite_sensitive = store.sensitive_rows(run_id)
        checks.append(
            _check(
                "normalized_precision_snapshot",
                sha256_value(sqlite_sensitive)
                == sha256_value(state["sensitive_records"]),
                precision_record_count=len(sqlite_sensitive),
            )
        )
        status_rows = store.fetch_status(run_id)
        assert status_rows is not None
        commit_keys = (
            "commit_seq",
            "from_generation",
            "to_generation",
            "effective_ms",
            "from_summary",
            "to_summary",
            "ledger_version",
            "logical_ns",
        )
        normalized_commits = [
            {key: row[key] for key in commit_keys}
            for row in status_rows["policy_commits"]
        ]
        state_commits = [
            {key: commit[key] for key in commit_keys}
            for commit in sorted(
                state["policy"]["switches"],
                key=lambda item: item["commit_seq"],
            )
        ]
        checks.append(
            _check(
                "normalized_policy_commit_snapshot",
                normalized_commits == state_commits
                and store.round_rows(run_id)
                == sorted(
                    state["round_summaries"],
                    key=lambda item: item["round_no"],
                ),
                policy_commits=len(status_rows["policy_commits"]),
                round_summaries=len(store.round_rows(run_id)),
            )
        )
        statuses = {
            tx["tx_id"]: tx["final_status"] for tx in state["txs"].values()
        }
        decisions_by_tx: dict[str, list[dict[str, Any]]] = {}
        for decision in decisions:
            decisions_by_tx.setdefault(decision["tx_id"], []).append(decision)
        ledger_by_tx: dict[str, list[dict[str, Any]]] = {}
        for entry in state["ledger_entries"]:
            ledger_by_tx.setdefault(entry["tx_id"], []).append(entry)
        rollback_consistent = True
        rollback_issues: list[dict[str, Any]] = []
        for tx_id, status in statuses.items():
            tx_decisions = decisions_by_tx.get(tx_id, [])
            if not tx_decisions:
                rollback_consistent = False
                rollback_issues.append({"tx_id": tx_id, "issue": "missing_decision"})
                continue
            decision = tx_decisions[-1]
            if status == "DEADLOCK_VICTIM_ROLLBACK":
                if decision.get("reason") != "DEADLOCK_CYCLE" or not decision.get("cycle"):
                    rollback_consistent = False
                    rollback_issues.append(
                        {"tx_id": tx_id, "issue": "deadlock_cycle_missing"}
                    )
            elif status == "ABANDONED_WAIT_TIMEOUT":
                if not any(
                    entry["status"] == "WAIT_TIMEOUT"
                    for entry in ledger_by_tx.get(tx_id, [])
                ):
                    rollback_consistent = False
                    rollback_issues.append(
                        {"tx_id": tx_id, "issue": "timeout_ledger_missing"}
                    )
            elif status == "MANUAL_ROLLBACK":
                if decision.get("reason") != "EXPLICIT_ROLLBACK":
                    rollback_consistent = False
                    rollback_issues.append(
                        {"tx_id": tx_id, "issue": "manual_reason_mismatch"}
                    )
            elif status == "NOT_STARTED_WITHIN_BUDGET":
                tx = state["txs"][tx_id]
                if tx.get("started_ns") is not None:
                    rollback_consistent = False
                    rollback_issues.append(
                        {"tx_id": tx_id, "issue": "budget_classified_but_started"}
                    )
        checks.append(
            _check(
                "rollback_classification_consistency",
                rollback_consistent,
                issues=rollback_issues,
                statuses=statuses,
            )
        )
        precision = state["sensitive_records"]
        precision_by_id = {
            record["record_id"]: record for record in precision
        }
        precision_consistent = True
        precision_issues: list[dict[str, Any]] = []
        for record in precision:
            if not (
                record.get("tx_ids")
                and record.get("resources")
                and int(record.get("next_scale", 0)) > int(record.get("scale", 0))
                and int(record.get("error_bound_ns", -1)) >= 0
            ):
                precision_consistent = False
                precision_issues.append(
                    {"record_id": record.get("record_id"), "issue": "invalid_fields"}
                )
            if record["kind"] == "WAIT_BUDGET_BOUNDARY":
                duration = int(record["duration_ns"])
                budget = int(record["budget_ns"])
                if abs(duration - budget) >= int(record["tolerance_ns"]):
                    precision_consistent = False
                    precision_issues.append(
                        {
                            "record_id": record["record_id"],
                            "issue": "outside_tolerance",
                        }
                    )
        for entry in state["ledger_entries"]:
            if entry.get("duration_ns") is None:
                continue
            if abs(int(entry["duration_ns"]) - int(entry["budget_ns"])) < int(
                entry["tolerance_ns"]
            ):
                expected_id = f"P:WAIT:{entry['entry_id']}"
                if expected_id not in precision_by_id:
                    precision_consistent = False
                    precision_issues.append(
                        {"entry_id": entry["entry_id"], "issue": "missing_record"}
                    )
        for decision in decisions:
            record_id = decision.get("precision_record_id")
            if record_id is not None and record_id not in precision_by_id:
                precision_consistent = False
                precision_issues.append(
                    {
                        "decision_seq": decision["decision_seq"],
                        "issue": "missing_deadlock_record",
                    }
                )
        checks.append(
            _check(
                "precision_record_consistency",
                precision_consistent,
                issues=precision_issues,
                record_count=len(precision),
                flipped=sum(1 for record in precision if record["flip"]),
            )
        )
    finally:
        store.close()

    replay: dict[str, Any] | None = None
    recovery: dict[str, Any] | None = None
    with tempfile.TemporaryDirectory(prefix="lock-wait-verify-") as temp_dir:
        temp_root = Path(temp_dir)
        replay = _fresh_run_digest(
            bundle,
            temp_root / "replay.sqlite3",
            parallelism=state["parallelism"],
            round_budget_ms=state["round_budget_ns"] // 1_000_000,
            max_logical_ms=state["max_logical_ns"] // 1_000_000,
        )
        checks.append(
            _check(
                "deterministic_fresh_replay",
                replay["state_digest"] == persisted_summary["state_digest"]
                and replay["ledger_digest"] == persisted_summary["ledger_digest"]
                and _decision_digest(replay["decisions"])
                == _decision_digest(persisted_summary["decisions"]),
                persisted_state_digest=persisted_summary["state_digest"],
                replay_state_digest=replay["state_digest"],
                persisted_ledger_digest=persisted_summary["ledger_digest"],
                replay_ledger_digest=replay["ledger_digest"],
            )
        )
        if run_recovery:
            journal_count = len(journal)
            raw_steps = {
                2,
                max(3, journal_count // 4),
                max(4, journal_count // 2),
                max(5, (3 * journal_count) // 4),
                max(6, journal_count - 1),
            }
            probe_steps = sorted(
                step for step in raw_steps if 1 < step < journal_count
            )
            probes: list[dict[str, Any]] = []
            for index, step in enumerate(probe_steps):
                probe = _recovery_probe(
                    bundle,
                    temp_root / f"recovery-{index}.sqlite3",
                    step,
                    parallelism=state["parallelism"],
                    round_budget_ms=state["round_budget_ns"] // 1_000_000,
                    max_logical_ms=state["max_logical_ns"] // 1_000_000,
                )
                probe["matches_fresh_replay"] = (
                    probe["state_digest"] == replay["state_digest"]
                    and probe["ledger_digest"] == replay["ledger_digest"]
                    and probe["decision_digest"]
                    == _decision_digest(replay["decisions"])
                )
                probes.append(probe)
            recovery = {
                "probes": probes,
                "all_match": all(probe["matches_fresh_replay"] for probe in probes),
                "probe_count": len(probes),
            }
            checks.append(
                _check(
                    "durable_checkpoint_recovery",
                    bool(probes) and recovery["all_match"],
                    probe_count=recovery["probe_count"],
                    crashed_steps=[probe["crash_after_step"] for probe in probes],
                )
            )
    overall = all(check["ok"] for check in checks)
    return {
        "ok": overall,
        "checks": checks,
        "pending_transactions": persisted_summary["pending_transactions"],
        "ledger_summary": persisted_summary["ledger_summary"],
        "actual_policy_versions": persisted_summary["actual_policy_versions"],
        "ledger": ledger_stats,
        "policy": {
            "active_generation": state["policy"]["generation"],
            "ledger_version": state["policy"]["ledger_version"],
            "actual_policy_versions": persisted_summary["actual_policy_versions"],
            "switches": state["policy"]["switches"],
        },
        "recovery": recovery,
        "persisted": {
            "state_digest": persisted_summary["state_digest"],
            "ledger_digest": persisted_summary["ledger_digest"],
            "decision_digest": _decision_digest(persisted_summary["decisions"]),
            "resume_count": state["resume_count"],
            "journal_entries": len(journal),
        },
        "fresh_replay": {
            "state_digest": replay["state_digest"],
            "ledger_digest": replay["ledger_digest"],
            "decision_digest": _decision_digest(replay["decisions"]),
        },
        "decisions": persisted_summary["decisions"],
        "precision_sensitive_records": persisted_summary[
            "precision_sensitive_records"
        ],
        "error_bound": persisted_summary["error_bound"],
    }
