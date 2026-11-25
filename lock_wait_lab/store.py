"""SQLite persistence with atomic per-microstep checkpoints."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .canonical import canonical_json, sha256_value, stable_output
from .errors import ErrorCode, LabError
from .samples import SampleBundle, policy_payload


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    bundle_fingerprint TEXT NOT NULL,
    samples_root TEXT NOT NULL,
    status TEXT NOT NULL,
    logical_ns INTEGER NOT NULL,
    step INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    state_digest TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_snapshots (
    generation INTEGER PRIMARY KEY,
    effective_ms INTEGER NOT NULL,
    summary TEXT NOT NULL,
    source TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_versions (
    run_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    policy_generation INTEGER NOT NULL,
    policy_summary TEXT NOT NULL,
    created_step INTEGER NOT NULL,
    created_logical_ns INTEGER NOT NULL,
    source_commit_seq INTEGER,
    PRIMARY KEY (run_id, version)
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    run_id TEXT NOT NULL,
    ledger_version INTEGER NOT NULL,
    entry_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    tx_id TEXT NOT NULL,
    op_id TEXT NOT NULL,
    resource TEXT NOT NULL,
    mode TEXT NOT NULL,
    enqueue_ns INTEGER NOT NULL,
    dequeue_ns INTEGER,
    duration_ns INTEGER,
    scale INTEGER,
    rounded_ms TEXT,
    rounding TEXT,
    rounding_direction TEXT,
    discarded_remainder_ns INTEGER,
    residual_ns INTEGER,
    own_error_bound_ns INTEGER,
    dependency_error_bound_ns INTEGER,
    cumulative_error_bound_ns INTEGER,
    dependency_chain_json TEXT NOT NULL,
    policy_generation INTEGER NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (run_id, ledger_version, entry_id)
);

CREATE TABLE IF NOT EXISTS duration_conversions (
    run_id TEXT NOT NULL,
    conversion_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    tx_id TEXT NOT NULL,
    resource TEXT,
    raw_ns INTEGER NOT NULL,
    scale INTEGER NOT NULL,
    rounded_ms TEXT NOT NULL,
    rounding TEXT NOT NULL,
    rounding_direction TEXT NOT NULL,
    discarded_remainder_ns INTEGER NOT NULL,
    residual_ns INTEGER NOT NULL,
    error_bound_ns INTEGER NOT NULL,
    PRIMARY KEY (run_id, conversion_id)
);

CREATE TABLE IF NOT EXISTS precision_sensitive (
    run_id TEXT NOT NULL,
    record_id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL,
    kind TEXT NOT NULL,
    tx_ids_json TEXT NOT NULL,
    resources_json TEXT NOT NULL,
    error_bound_ns INTEGER NOT NULL,
    scale INTEGER NOT NULL,
    next_scale INTEGER NOT NULL,
    flip INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tx_states (
    run_id TEXT NOT NULL,
    tx_id TEXT NOT NULL,
    script_index INTEGER NOT NULL,
    state TEXT NOT NULL,
    final_status TEXT,
    reason TEXT,
    bound_generation INTEGER,
    start_at_ns INTEGER NOT NULL,
    retry_used INTEGER NOT NULL,
    operation_index INTEGER NOT NULL,
    waiting_resource TEXT,
    waiting_entry_id TEXT,
    ready_at_ns INTEGER,
    started_ns INTEGER,
    finished_ns INTEGER,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, tx_id)
);

CREATE TABLE IF NOT EXISTS lock_holdings (
    run_id TEXT NOT NULL,
    resource TEXT NOT NULL,
    owner_tx TEXT NOT NULL,
    op_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    acquired_ns INTEGER NOT NULL,
    hold_duration_ns INTEGER NOT NULL,
    PRIMARY KEY (run_id, resource, owner_tx)
);

CREATE TABLE IF NOT EXISTS wait_queue_snapshot (
    run_id TEXT NOT NULL,
    entry_id TEXT PRIMARY KEY,
    queue_pos INTEGER NOT NULL,
    resource TEXT NOT NULL,
    tx_id TEXT NOT NULL,
    op_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    enqueue_ns INTEGER NOT NULL,
    trigger_ns INTEGER NOT NULL,
    budget_ns INTEGER NOT NULL,
    scale INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tx_decisions (
    run_id TEXT NOT NULL,
    decision_seq INTEGER NOT NULL,
    tx_id TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    logical_ns INTEGER NOT NULL,
    policy_generation INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, decision_seq)
);

CREATE TABLE IF NOT EXISTS policy_commits (
    run_id TEXT NOT NULL,
    commit_seq INTEGER NOT NULL,
    from_generation INTEGER NOT NULL,
    to_generation INTEGER NOT NULL,
    effective_ms INTEGER NOT NULL,
    from_summary TEXT NOT NULL,
    to_summary TEXT NOT NULL,
    ledger_version INTEGER NOT NULL,
    logical_ns INTEGER NOT NULL,
    PRIMARY KEY (run_id, commit_seq)
);

CREATE TABLE IF NOT EXISTS active_policy (
    run_id TEXT PRIMARY KEY,
    generation INTEGER NOT NULL,
    ledger_version INTEGER NOT NULL,
    commit_seq INTEGER,
    updated_step INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS round_summaries (
    run_id TEXT NOT NULL,
    round_no INTEGER NOT NULL,
    start_ns INTEGER NOT NULL,
    end_ns INTEGER NOT NULL,
    pending_json TEXT NOT NULL,
    started_json TEXT NOT NULL,
    finalized_json TEXT NOT NULL,
    PRIMARY KEY (run_id, round_no)
);

CREATE TABLE IF NOT EXISTS step_journal (
    run_id TEXT NOT NULL,
    step INTEGER NOT NULL,
    logical_ns INTEGER NOT NULL,
    kind TEXT NOT NULL,
    event_json TEXT NOT NULL,
    event_digest TEXT NOT NULL,
    PRIMARY KEY (run_id, step)
);

CREATE TABLE IF NOT EXISTS recovery_events (
    run_id TEXT NOT NULL,
    resume_seq INTEGER NOT NULL,
    resumed_from_step INTEGER NOT NULL,
    journal_count INTEGER NOT NULL,
    PRIMARY KEY (run_id, resume_seq)
);
"""


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.connection = sqlite3.connect(
                self.path,
                isolation_level=None,
                timeout=30.0,
            )
        except sqlite3.Error as exc:
            raise LabError(
                ErrorCode.STATE_INVALID,
                f"cannot open state database: {exc}",
                path=str(self.path),
            ) from exc
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _begin(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self.connection.execute("COMMIT")

    def _rollback(self) -> None:
        try:
            self.connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def ensure_policies(self, bundle: SampleBundle) -> None:
        try:
            self._begin()
            for view in sorted(
                bundle.policy_views.values(), key=lambda item: item.generation
            ):
                self.connection.execute(
                    """
                    INSERT INTO policy_snapshots
                        (generation, effective_ms, summary, source, payload_json)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(generation) DO UPDATE SET
                        effective_ms = excluded.effective_ms,
                        summary = excluded.summary,
                        source = excluded.source,
                        payload_json = excluded.payload_json
                    """,
                    (
                        view.generation,
                        view.effective_ms,
                        view.summary,
                        str(bundle.root),
                        canonical_json(policy_payload(view)),
                    ),
                )
            self._commit()
        except Exception:
            self._rollback()
            raise

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def load_state(self, run_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT state_json FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise LabError(
                ErrorCode.STATE_MISSING,
                f"run {run_id!r} has not been initialized",
                field="run_id",
                path=str(self.path),
            )
        try:
            return json.loads(row["state_json"])
        except json.JSONDecodeError as exc:
            raise LabError(
                ErrorCode.STATE_INVALID,
                "persisted run state is not valid JSON",
                field="state_json",
                path=str(self.path),
            ) from exc

    def reset_run(self, run_id: str) -> None:
        try:
            self._begin()
            for table in (
                "ledger_entries",
                "ledger_versions",
                "duration_conversions",
                "precision_sensitive",
                "tx_states",
                "lock_holdings",
                "wait_queue_snapshot",
                "tx_decisions",
                "policy_commits",
                "active_policy",
                "round_summaries",
                "step_journal",
                "recovery_events",
                "runs",
            ):
                self.connection.execute(
                    f"DELETE FROM {table} WHERE run_id = ?", (run_id,)
                )
            self._commit()
        except Exception:
            self._rollback()
            raise

    @staticmethod
    def _entry_tuple(run_id: str, version: int, entry: dict[str, Any]) -> tuple[Any, ...]:
        return (
            run_id,
            version,
            entry["entry_id"],
            entry["sequence"],
            entry["tx_id"],
            entry["op_id"],
            entry["resource"],
            entry["mode"],
            entry["enqueue_ns"],
            entry.get("dequeue_ns"),
            entry.get("duration_ns"),
            entry.get("scale"),
            entry.get("rounded_ms"),
            entry.get("rounding"),
            entry.get("rounding_direction"),
            entry.get("discarded_remainder_ns"),
            entry.get("residual_ns"),
            entry.get("own_error_bound_ns", 0),
            entry.get("dependency_error_bound_ns", 0),
            entry.get("cumulative_error_bound_ns", 0),
            canonical_json(entry.get("dependency_chain", [])),
            entry["policy_generation"],
            entry["status"],
        )

    def save_checkpoint(
        self,
        run_id: str,
        state: dict[str, Any],
        event: dict[str, Any],
    ) -> None:
        state_json = canonical_json(state)
        state_digest = sha256_value(state)
        step = int(state["step"])
        event_json = canonical_json(event)
        event_digest = sha256_value(event)
        policy = state["policy"]
        new_version = int(policy["ledger_version"])
        try:
            self._begin()
            existing = self.connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            active = self.connection.execute(
                "SELECT * FROM active_policy WHERE run_id = ?", (run_id,)
            ).fetchone()
            old_version = int(active["ledger_version"]) if active is not None else 0
            if old_version and new_version != old_version:
                if new_version != old_version + 1:
                    raise LabError(
                        ErrorCode.STATE_INVALID,
                        "ledger version replacement must advance by exactly one",
                        field="policy.ledger_version",
                    )
                old_entries = list(
                    self.connection.execute(
                        "SELECT * FROM ledger_entries "
                        "WHERE run_id = ? AND ledger_version = ? ORDER BY sequence",
                        (run_id, old_version),
                    )
                )
                for row in old_entries:
                    self.connection.execute(
                        """
                        INSERT OR REPLACE INTO ledger_entries (
                            run_id, ledger_version, entry_id, sequence, tx_id, op_id,
                            resource, mode, enqueue_ns, dequeue_ns, duration_ns, scale,
                            rounded_ms, rounding, rounding_direction,
                            discarded_remainder_ns, residual_ns, own_error_bound_ns,
                            dependency_error_bound_ns, cumulative_error_bound_ns,
                            dependency_chain_json, policy_generation, status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            row["run_id"],
                            new_version,
                            row["entry_id"],
                            row["sequence"],
                            row["tx_id"],
                            row["op_id"],
                            row["resource"],
                            row["mode"],
                            row["enqueue_ns"],
                            row["dequeue_ns"],
                            row["duration_ns"],
                            row["scale"],
                            row["rounded_ms"],
                            row["rounding"],
                            row["rounding_direction"],
                            row["discarded_remainder_ns"],
                            row["residual_ns"],
                            row["own_error_bound_ns"],
                            row["dependency_error_bound_ns"],
                            row["cumulative_error_bound_ns"],
                            row["dependency_chain_json"],
                            row["policy_generation"],
                            row["status"],
                        ),
                    )
            self.connection.execute(
                """
                INSERT INTO ledger_versions (
                    run_id, version, policy_generation, policy_summary,
                    created_step, created_logical_ns, source_commit_seq
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, version) DO UPDATE SET
                    policy_generation = excluded.policy_generation,
                    policy_summary = excluded.policy_summary,
                    created_step = excluded.created_step,
                    created_logical_ns = excluded.created_logical_ns,
                    source_commit_seq = excluded.source_commit_seq
                """,
                (
                    run_id,
                    new_version,
                    policy["generation"],
                    policy["summary"],
                    int(policy.get("ledger_created_step", step)),
                    int(policy.get("ledger_created_ns", state["logical_ns"])),
                    policy.get("last_commit_seq"),
                ),
            )
            self.connection.execute(
                "DELETE FROM ledger_entries WHERE run_id = ? AND ledger_version = ?",
                (run_id, new_version),
            )
            for entry in sorted(
                state["ledger_entries"], key=lambda item: item["sequence"]
            ):
                self.connection.execute(
                    """
                    INSERT INTO ledger_entries (
                        run_id, ledger_version, entry_id, sequence, tx_id, op_id,
                        resource, mode, enqueue_ns, dequeue_ns, duration_ns, scale,
                        rounded_ms, rounding, rounding_direction,
                        discarded_remainder_ns, residual_ns, own_error_bound_ns,
                        dependency_error_bound_ns, cumulative_error_bound_ns,
                        dependency_chain_json, policy_generation, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._entry_tuple(run_id, new_version, entry),
                )

            self.connection.execute(
                "DELETE FROM duration_conversions WHERE run_id = ?", (run_id,)
            )
            for conversion in sorted(
                state["conversions"], key=lambda item: item["sequence"]
            ):
                self.connection.execute(
                    """
                    INSERT INTO duration_conversions (
                        run_id, conversion_id, sequence, source_kind, source_id,
                        tx_id, resource, raw_ns, scale, rounded_ms, rounding,
                        rounding_direction, discarded_remainder_ns, residual_ns,
                        error_bound_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        conversion["conversion_id"],
                        conversion["sequence"],
                        conversion["source_kind"],
                        conversion["source_id"],
                        conversion["tx_id"],
                        conversion.get("resource"),
                        conversion["raw_ns"],
                        conversion["scale"],
                        conversion["rounded_ms"],
                        conversion["rounding"],
                        conversion["rounding_direction"],
                        conversion["discarded_remainder_ns"],
                        conversion["residual_ns"],
                        conversion["error_bound_ns"],
                    ),
                )

            self.connection.execute(
                "DELETE FROM precision_sensitive WHERE run_id = ?", (run_id,)
            )
            for record in sorted(
                state["sensitive_records"], key=lambda item: item["sequence"]
            ):
                self.connection.execute(
                    """
                    INSERT INTO precision_sensitive (
                        run_id, record_id, sequence, kind, tx_ids_json,
                        resources_json, error_bound_ns, scale, next_scale, flip,
                        payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        record["record_id"],
                        record["sequence"],
                        record["kind"],
                        canonical_json(record["tx_ids"]),
                        canonical_json(record["resources"]),
                        record["error_bound_ns"],
                        record["scale"],
                        record["next_scale"],
                        1 if record["flip"] else 0,
                        canonical_json(record),
                    ),
                )

            self.connection.execute("DELETE FROM tx_states WHERE run_id = ?", (run_id,))
            for tx in sorted(state["txs"].values(), key=lambda item: item["script_index"]):
                self.connection.execute(
                    """
                    INSERT INTO tx_states (
                        run_id, tx_id, script_index, state, final_status, reason,
                        bound_generation, start_at_ns, retry_used, operation_index,
                        waiting_resource, waiting_entry_id, ready_at_ns, started_ns,
                        finished_ns, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        tx["tx_id"],
                        tx["script_index"],
                        tx["state"],
                        tx.get("final_status"),
                        tx.get("reason"),
                        tx.get("bound_generation"),
                        tx["start_at_ns"],
                        tx["retry_used"],
                        tx["pc"],
                        tx.get("waiting_resource"),
                        tx.get("waiting_entry_id"),
                        tx.get("ready_at_ns"),
                        tx.get("started_ns"),
                        tx.get("finished_ns"),
                        canonical_json(tx),
                    ),
                )

            self.connection.execute(
                "DELETE FROM lock_holdings WHERE run_id = ?", (run_id,)
            )
            for resource in sorted(state["locks"]):
                lock = state["locks"][resource]
                for owner in sorted(lock["owners"], key=lambda item: item["tx_id"]):
                    self.connection.execute(
                        """
                        INSERT INTO lock_holdings (
                            run_id, resource, owner_tx, op_id, mode, acquired_ns,
                            hold_duration_ns
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            resource,
                            owner["tx_id"],
                            owner["op_id"],
                            owner["mode"],
                            owner["acquired_ns"],
                            owner["hold_duration_ns"],
                        ),
                    )

            self.connection.execute(
                "DELETE FROM wait_queue_snapshot WHERE run_id = ?", (run_id,)
            )
            ledger_by_id = {
                entry["entry_id"]: entry for entry in state["ledger_entries"]
            }
            for resource in sorted(state["queues"]):
                for queue_pos, entry_ref in enumerate(state["queues"][resource]):
                    entry = (
                        ledger_by_id[entry_ref]
                        if isinstance(entry_ref, str)
                        else entry_ref
                    )
                    self.connection.execute(
                        """
                        INSERT INTO wait_queue_snapshot (
                            run_id, entry_id, queue_pos, resource, tx_id, op_id,
                            mode, enqueue_ns, trigger_ns, budget_ns, scale
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            entry["entry_id"],
                            queue_pos,
                            resource,
                            entry["tx_id"],
                            entry["op_id"],
                            entry["mode"],
                            entry["enqueue_ns"],
                            entry["trigger_ns"],
                            entry["budget_ns"],
                            entry["scale"],
                        ),
                    )

            self.connection.execute(
                "DELETE FROM tx_decisions WHERE run_id = ?", (run_id,)
            )
            for decision in sorted(
                state["decisions"], key=lambda item: item["decision_seq"]
            ):
                self.connection.execute(
                    """
                    INSERT INTO tx_decisions (
                        run_id, decision_seq, tx_id, status, reason, logical_ns,
                        policy_generation, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        decision["decision_seq"],
                        decision["tx_id"],
                        decision["status"],
                        decision["reason"],
                        decision["logical_ns"],
                        decision["policy_generation"],
                        canonical_json(decision),
                    ),
                )

            self.connection.execute(
                "DELETE FROM policy_commits WHERE run_id = ?", (run_id,)
            )
            for commit in sorted(
                state["policy"]["switches"], key=lambda item: item["commit_seq"]
            ):
                self.connection.execute(
                    """
                    INSERT INTO policy_commits (
                        run_id, commit_seq, from_generation, to_generation,
                        effective_ms, from_summary, to_summary, ledger_version,
                        logical_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        commit["commit_seq"],
                        commit["from_generation"],
                        commit["to_generation"],
                        commit["effective_ms"],
                        commit["from_summary"],
                        commit["to_summary"],
                        commit["ledger_version"],
                        commit["logical_ns"],
                    ),
                )

            self.connection.execute(
                "DELETE FROM round_summaries WHERE run_id = ?", (run_id,)
            )
            for round_summary in sorted(
                state.get("round_summaries", []),
                key=lambda item: item["round_no"],
            ):
                self.connection.execute(
                    """
                    INSERT INTO round_summaries (
                        run_id, round_no, start_ns, end_ns, pending_json,
                        started_json, finalized_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        round_summary["round_no"],
                        round_summary["start_ns"],
                        round_summary["end_ns"],
                        canonical_json(round_summary["pending"]),
                        canonical_json(round_summary["started"]),
                        canonical_json(round_summary["finalized"]),
                    ),
                )

            existing_journal = self.connection.execute(
                "SELECT event_digest FROM step_journal WHERE run_id = ? AND step = ?",
                (run_id, step),
            ).fetchone()
            if existing_journal is None:
                self.connection.execute(
                    """
                    INSERT INTO step_journal (
                        run_id, step, logical_ns, kind, event_json, event_digest
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        step,
                        state["logical_ns"],
                        event["kind"],
                        event_json,
                        event_digest,
                    ),
                )
            elif existing_journal["event_digest"] != event_digest:
                raise LabError(
                    ErrorCode.STATE_INVALID,
                    "attempted to replace a committed journal step",
                    field="step",
                )

            if existing is None:
                self.connection.execute(
                    """
                    INSERT INTO runs (
                        run_id, bundle_fingerprint, samples_root, status,
                        logical_ns, step, state_json, state_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        state["bundle_fingerprint"],
                        state["samples_root"],
                        state["run_status"],
                        state["logical_ns"],
                        step,
                        state_json,
                        state_digest,
                    ),
                )
            else:
                self.connection.execute(
                    """
                    UPDATE runs SET
                        bundle_fingerprint = ?, samples_root = ?, status = ?,
                        logical_ns = ?, step = ?, state_json = ?, state_digest = ?
                    WHERE run_id = ?
                    """,
                    (
                        state["bundle_fingerprint"],
                        state["samples_root"],
                        state["run_status"],
                        state["logical_ns"],
                        step,
                        state_json,
                        state_digest,
                        run_id,
                    ),
                )

            self.connection.execute(
                """
                INSERT INTO active_policy (
                    run_id, generation, ledger_version, commit_seq, updated_step
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    generation = excluded.generation,
                    ledger_version = excluded.ledger_version,
                    commit_seq = excluded.commit_seq,
                    updated_step = excluded.updated_step
                """,
                (
                    run_id,
                    policy["generation"],
                    new_version,
                    policy.get("last_commit_seq"),
                    step,
                ),
            )
            self._commit()
        except Exception:
            self._rollback()
            raise

    def save_resume_marker(self, run_id: str, state: dict[str, Any]) -> None:
        state_json = canonical_json(state)
        state_digest = sha256_value(state)
        resume_seq = int(state.get("resume_count", 0))
        try:
            self._begin()
            row = self.connection.execute(
                "SELECT step FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise LabError(
                    ErrorCode.STATE_MISSING,
                    "cannot resume an uninitialized run",
                    field="run_id",
                )
            journal_count = self.connection.execute(
                "SELECT COUNT(*) FROM step_journal WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            self.connection.execute(
                """
                UPDATE runs SET
                    status = ?, logical_ns = ?, step = ?, state_json = ?,
                    state_digest = ?
                WHERE run_id = ?
                """,
                (
                    state["run_status"],
                    state["logical_ns"],
                    state["step"],
                    state_json,
                    state_digest,
                    run_id,
                ),
            )
            self.connection.execute(
                """
                INSERT OR REPLACE INTO recovery_events (
                    run_id, resume_seq, resumed_from_step, journal_count
                ) VALUES (?, ?, ?, ?)
                """,
                (run_id, resume_seq, int(state["step"]), journal_count),
            )
            self._commit()
        except Exception:
            self._rollback()
            raise

    def fetch_status(self, run_id: str) -> dict[str, Any] | None:
        run = self.get_run(run_id)
        if run is None:
            return None
        active = self.connection.execute(
            "SELECT * FROM active_policy WHERE run_id = ?", (run_id,)
        ).fetchone()
        ledger_versions = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM ledger_versions WHERE run_id = ? ORDER BY version",
                (run_id,),
            )
        ]
        commits = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM policy_commits WHERE run_id = ? ORDER BY commit_seq",
                (run_id,),
            )
        ]
        decisions = [
            json.loads(row["payload_json"])
            for row in self.connection.execute(
                "SELECT payload_json FROM tx_decisions "
                "WHERE run_id = ? ORDER BY decision_seq",
                (run_id,),
            )
        ]
        sensitive = [
            json.loads(row["payload_json"])
            for row in self.connection.execute(
                "SELECT payload_json FROM precision_sensitive "
                "WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            )
        ]
        journal_count = self.connection.execute(
            "SELECT COUNT(*) FROM step_journal WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        lock_count = self.connection.execute(
            "SELECT COUNT(*) FROM lock_holdings WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        queue_count = self.connection.execute(
            "SELECT COUNT(*) FROM wait_queue_snapshot WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        return {
            "run": run,
            "active_policy": dict(active) if active is not None else None,
            "ledger_versions": ledger_versions,
            "policy_commits": commits,
            "decisions": decisions,
            "precision_sensitive": sensitive,
            "journal_count": journal_count,
            "lock_count": lock_count,
            "wait_queue_count": queue_count,
        }

    def journal(self, run_id: str) -> list[dict[str, Any]]:
        return [
            {
                "step": row["step"],
                "logical_ns": row["logical_ns"],
                "kind": row["kind"],
                "event": json.loads(row["event_json"]),
                "event_digest": row["event_digest"],
            }
            for row in self.connection.execute(
                "SELECT * FROM step_journal WHERE run_id = ? ORDER BY step",
                (run_id,),
            )
        ]

    def ledger_rows(self, run_id: str) -> list[dict[str, Any]]:
        rows = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM ledger_entries WHERE run_id = ? "
                "ORDER BY ledger_version, sequence",
                (run_id,),
            )
        ]
        for row in rows:
            row["dependency_chain"] = json.loads(row.pop("dependency_chain_json"))
        return rows

    def conversion_rows(self, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM duration_conversions WHERE run_id = ? "
                "ORDER BY sequence",
                (run_id,),
            )
        ]

    def tx_rows(self, run_id: str) -> list[dict[str, Any]]:
        return [
            json.loads(row["payload_json"])
            for row in self.connection.execute(
                "SELECT payload_json FROM tx_states WHERE run_id = ? "
                "ORDER BY script_index",
                (run_id,),
            )
        ]

    def lock_rows(self, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM lock_holdings WHERE run_id = ? "
                "ORDER BY resource, owner_tx",
                (run_id,),
            )
        ]

    def wait_queue_rows(self, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM wait_queue_snapshot WHERE run_id = ? "
                "ORDER BY resource, queue_pos",
                (run_id,),
            )
        ]

    def sensitive_rows(self, run_id: str) -> list[dict[str, Any]]:
        return [
            json.loads(row["payload_json"])
            for row in self.connection.execute(
                "SELECT payload_json FROM precision_sensitive "
                "WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            )
        ]

    def decision_rows(self, run_id: str) -> list[dict[str, Any]]:
        return [
            json.loads(row["payload_json"])
            for row in self.connection.execute(
                "SELECT payload_json FROM tx_decisions "
                "WHERE run_id = ? ORDER BY decision_seq",
                (run_id,),
            )
        ]

    def round_rows(self, run_id: str) -> list[dict[str, Any]]:
        return [
            {
                "round_no": row["round_no"],
                "start_ns": row["start_ns"],
                "end_ns": row["end_ns"],
                "pending": json.loads(row["pending_json"]),
                "started": json.loads(row["started_json"]),
                "finalized": json.loads(row["finalized_json"]),
            }
            for row in self.connection.execute(
                "SELECT * FROM round_summaries WHERE run_id = ? ORDER BY round_no",
                (run_id,),
            )
        ]

    def dump_json(self, run_id: str) -> str:
        state = self.load_state(run_id)
        return stable_output(state)
