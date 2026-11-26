from __future__ import annotations

import os
import signal
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import RecoveryConsistencyError
from .utils import canonical_json, read_json, write_json


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS functions (
    name TEXT PRIMARY KEY,
    spec_json TEXT NOT NULL,
    state_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instances (
    id TEXT PRIMARY KEY,
    function_name TEXT NOT NULL,
    phase TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    state_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calls (
    seq INTEGER PRIMARY KEY,
    function_name TEXT NOT NULL,
    instance_id TEXT,
    status TEXT NOT NULL,
    state_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id INTEGER PRIMARY KEY,
    entry_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_charges (
    charge_id TEXT PRIMARY KEY,
    step_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    amount_ms INTEGER NOT NULL,
    created_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    step_id TEXT,
    logical_clock_ms INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS step_journal (
    step_id TEXT PRIMARY KEY,
    step_order INTEGER NOT NULL,
    input_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    planned_json TEXT NOT NULL,
    result_json TEXT,
    created_clock_ms INTEGER NOT NULL,
    committed_clock_ms INTEGER
);

CREATE TABLE IF NOT EXISTS policy_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_records (
    snapshot_id TEXT NOT NULL,
    policy_key TEXT NOT NULL,
    generation INTEGER NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, policy_key)
);

CREATE INDEX IF NOT EXISTS idx_instances_function ON instances(function_name);
CREATE INDEX IF NOT EXISTS idx_charges_step ON budget_charges(step_id);
CREATE INDEX IF NOT EXISTS idx_journal_status ON step_journal(status, step_order);
"""


@dataclass
class RecoveryReport:
    database: str
    integrity_check: str = "ok"
    last_committed_step_id: str | None = None
    last_committed_step_order: int = 0
    logical_clock_watermark_ms: int = 0
    processed_request_seq: int = 0
    active_policy_snapshot: str | None = None
    cold_start_watermark: dict[str, Any] = field(default_factory=dict)
    committed_steps: list[str] = field(default_factory=list)
    half_committed_steps: list[dict[str, Any]] = field(default_factory=list)
    orphan_charges: list[dict[str, Any]] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "integrity_check": self.integrity_check,
            "last_committed_step_id": self.last_committed_step_id,
            "last_committed_step_order": self.last_committed_step_order,
            "logical_clock_watermark_ms": self.logical_clock_watermark_ms,
            "processed_request_seq": self.processed_request_seq,
            "active_policy_snapshot": self.active_policy_snapshot,
            "cold_start_watermark": self.cold_start_watermark,
            "committed_steps": self.committed_steps,
            "half_committed_steps": self.half_committed_steps,
            "orphan_charges": self.orphan_charges,
            "actions": self.actions,
        }


class StateStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = ":memory:" if str(path) == ":memory:" else Path(path)
        if self.path != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.kill_before_commit_step: str | None = None

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def set_kill_before_commit(self, step_id: str | None) -> None:
        self.kill_before_commit_step = step_id

    def prepare_step(
        self,
        step_id: str,
        step_order: int,
        input_digest: str,
        planned: dict[str, Any],
        clock_ms: int,
    ) -> str:
        row = self.conn.execute(
            "SELECT status FROM step_journal WHERE step_id=?", (step_id,)
        ).fetchone()
        if row is not None:
            return str(row["status"])
        self.conn.execute(
            """
            INSERT INTO step_journal(
                step_id, step_order, input_digest, status, planned_json,
                created_clock_ms
            ) VALUES (?, ?, ?, 'prepared', ?, ?)
            """,
            (step_id, step_order, input_digest, canonical_json(planned), clock_ms),
        )
        self.conn.commit()
        return "prepared"

    def is_step_committed(self, step_id: str) -> bool:
        row = self.conn.execute(
            "SELECT status FROM step_journal WHERE step_id=?", (step_id,)
        ).fetchone()
        return bool(row and row["status"] == "committed")

    def get_step(self, step_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM step_journal WHERE step_id=?", (step_id,)
        ).fetchone()
        return dict(row) if row else None

    def commit_snapshot(
        self,
        *,
        step_id: str,
        step_order: int,
        clock_watermark_ms: int,
        processed_request_seq: int,
        engine_state: dict[str, Any],
        functions: list[dict[str, Any]],
        instances: list[dict[str, Any]],
        calls: list[dict[str, Any]],
        events: list[dict[str, Any]],
        ledger_entries: list[dict[str, Any]],
        budget_charges: list[dict[str, Any]],
        policy_pointer: dict[str, Any] | None,
        result: dict[str, Any],
    ) -> None:
        if self.kill_before_commit_step == step_id:
            self._write_snapshot_tx(
                step_id=step_id,
                step_order=step_order,
                clock_watermark_ms=clock_watermark_ms,
                processed_request_seq=processed_request_seq,
                engine_state=engine_state,
                functions=functions,
                instances=instances,
                calls=calls,
                events=events,
                ledger_entries=ledger_entries,
                budget_charges=budget_charges,
                policy_pointer=policy_pointer,
                result=result,
                kill_before_commit=True,
            )
            os.kill(os.getpid(), signal.SIGKILL)
            raise RuntimeError("SIGKILL was not delivered")
        self._write_snapshot_tx(
            step_id=step_id,
            step_order=step_order,
            clock_watermark_ms=clock_watermark_ms,
            processed_request_seq=processed_request_seq,
            engine_state=engine_state,
            functions=functions,
            instances=instances,
            calls=calls,
            events=events,
            ledger_entries=ledger_entries,
            budget_charges=budget_charges,
            policy_pointer=policy_pointer,
            result=result,
            kill_before_commit=False,
        )

    def _write_snapshot_tx(self, *, kill_before_commit: bool, **kwargs: Any) -> None:
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('engine_state_json', ?)",
                (canonical_json(kwargs["engine_state"]),),
            )
            cold_watermark = kwargs["engine_state"].get("cold_start_watermark", {})
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) "
                "VALUES ('cold_start_watermark_json', ?)",
                (canonical_json(cold_watermark),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('logical_clock_watermark_ms', ?)",
                (str(kwargs["clock_watermark_ms"]),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('processed_request_seq', ?)",
                (str(kwargs["processed_request_seq"]),),
            )
            if kwargs["policy_pointer"] is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES ('active_policy_snapshot', ?)",
                    (kwargs["policy_pointer"].get("snapshot_id", ""),),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES ('policy_pointer_json', ?)",
                    (canonical_json(kwargs["policy_pointer"]),),
                )

            for function in kwargs["functions"]:
                name = function["name"]
                spec = function.get("spec", {})
                state = function.get("state", {})
                conn.execute(
                    "INSERT OR REPLACE INTO functions(name, spec_json, state_json) VALUES (?, ?, ?)",
                    (name, canonical_json(spec), canonical_json(state)),
                )

            for instance in kwargs["instances"]:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO instances(
                        id, function_name, phase, logical_clock_ms, state_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        instance["id"],
                        instance["function_name"],
                        instance["phase"],
                        int(instance.get("logical_clock_ms", 0)),
                        canonical_json(instance),
                    ),
                )

            for call in kwargs["calls"]:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO calls(
                        seq, function_name, instance_id, status, state_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        int(call["seq"]),
                        call["function_name"],
                        call.get("instance_id"),
                        call["status"],
                        canonical_json(call),
                    ),
                )

            for entry in kwargs["ledger_entries"]:
                conn.execute(
                    "INSERT OR REPLACE INTO ledger_entries(entry_id, entry_json) VALUES (?, ?)",
                    (int(entry["entry_id"]), canonical_json(entry)),
                )

            for charge in kwargs["budget_charges"]:
                existing = conn.execute(
                    "SELECT amount_ms FROM budget_charges WHERE charge_id=?",
                    (charge["charge_id"],),
                ).fetchone()
                if existing is not None and int(existing["amount_ms"]) != int(charge["amount_ms"]):
                    raise RecoveryConsistencyError(
                        f"idempotency key {charge['charge_id']} reused with different amount"
                    )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO budget_charges(
                        charge_id, step_id, round_id, candidate_id,
                        amount_ms, created_clock_ms
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        charge["charge_id"],
                        charge["step_id"],
                        charge["round_id"],
                        charge["candidate_id"],
                        int(charge["amount_ms"]),
                        int(charge["clock_ms"]),
                    ),
                )

            for event in kwargs["events"]:
                conn.execute(
                    """
                    INSERT INTO events(step_id, logical_clock_ms, kind, payload_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        event.get("step_id", kwargs["step_id"]),
                        int(event.get("clock_ms", kwargs["clock_watermark_ms"])),
                        event["kind"],
                        canonical_json(event.get("payload", {})),
                    ),
                )

            conn.execute(
                """
                UPDATE step_journal
                   SET status='committed', result_json=?, committed_clock_ms=?
                 WHERE step_id=?
                """,
                (
                    canonical_json(kwargs["result"]),
                    int(kwargs["clock_watermark_ms"]),
                    kwargs["step_id"],
                ),
            )
            if kill_before_commit:
                # Intentionally kill before COMMIT. SQLite rolls the snapshot
                # transaction back after process death; the prepared journal
                # row remains as the identifiable half-committed intent.
                os.kill(os.getpid(), signal.SIGKILL)  # pragma: no cover
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def recover(self, recovery_clock_ms: int = 0) -> RecoveryReport:
        check = self.conn.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise RecoveryConsistencyError(f"SQLite integrity check failed: {check}")

        watermark_row = self.conn.execute(
            "SELECT value FROM meta WHERE key='logical_clock_watermark_ms'"
        ).fetchone()
        seq_row = self.conn.execute(
            "SELECT value FROM meta WHERE key='processed_request_seq'"
        ).fetchone()
        policy_row = self.conn.execute(
            "SELECT value FROM meta WHERE key='active_policy_snapshot'"
        ).fetchone()
        cold_row = self.conn.execute(
            "SELECT value FROM meta WHERE key='cold_start_watermark_json'"
        ).fetchone()
        cold_watermark: dict[str, Any] = {}
        if cold_row is not None:
            import json

            cold_watermark = json.loads(cold_row["value"])
        report = RecoveryReport(
            database=str(self.path),
            integrity_check=str(check),
            logical_clock_watermark_ms=int(watermark_row["value"]) if watermark_row else 0,
            processed_request_seq=int(seq_row["value"]) if seq_row else 0,
            active_policy_snapshot=policy_row["value"] if policy_row else None,
            cold_start_watermark=cold_watermark,
        )

        committed = self.conn.execute(
            "SELECT step_id FROM step_journal WHERE status='committed' ORDER BY step_order ASC"
        ).fetchall()
        report.committed_steps = [row["step_id"] for row in committed]
        last = self.conn.execute(
            """
            SELECT * FROM step_journal
             WHERE status='committed'
             ORDER BY step_order DESC, rowid DESC LIMIT 1
            """
        ).fetchone()
        if last is not None:
            report.last_committed_step_id = last["step_id"]
            report.last_committed_step_order = int(last["step_order"])

        interrupted = self.conn.execute(
            """
            SELECT step_id, step_order, planned_json, created_clock_ms
              FROM step_journal
             WHERE status IN ('prepared', 'recovering')
             ORDER BY step_order ASC
            """
        ).fetchall()
        for row in interrupted:
            step_id = row["step_id"]
            charges = self.conn.execute(
                "SELECT * FROM budget_charges WHERE step_id=?", (step_id,)
            ).fetchall()
            item = {
                "step_id": step_id,
                "step_order": int(row["step_order"]),
                "created_clock_ms": int(row["created_clock_ms"]),
                "planned": __import__("json").loads(row["planned_json"]),
                "committed_budget_charges": [dict(charge) for charge in charges],
                "classification": "prepared_intent_without_commit_marker",
            }
            report.half_committed_steps.append(item)
            if charges:
                report.orphan_charges.extend(dict(charge) for charge in charges)
            self.conn.execute(
                "UPDATE step_journal SET status='interrupted' WHERE step_id=?",
                (step_id,),
            )
            report.actions.append(f"marked {step_id} interrupted; deterministic replay required")
        self.conn.commit()

        if report.orphan_charges:
            raise RecoveryConsistencyError(
                "found committed budget charges without a committed step marker"
            )
        return report

    def load_engine_state(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key='engine_state_json'"
        ).fetchone()
        if not row:
            return None
        import json

        return json.loads(row["value"])

    def load_ledger_entries(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT entry_json FROM ledger_entries ORDER BY entry_id ASC"
        ).fetchall()
        import json

        return [json.loads(row["entry_json"]) for row in rows]

    def load_calls(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT state_json FROM calls ORDER BY seq ASC").fetchall()
        import json

        return [json.loads(row["state_json"]) for row in rows]

    def load_instances(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT state_json FROM instances ORDER BY id ASC").fetchall()
        import json

        return [json.loads(row["state_json"]) for row in rows]

    def install_policy_snapshot(
        self,
        *,
        snapshot_id: str,
        version: int,
        kind: str,
        manifest: dict[str, Any],
        records: list[dict[str, Any]],
        clock_ms: int,
    ) -> None:
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO policy_snapshots(
                    snapshot_id, version, kind, manifest_json, logical_clock_ms
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (snapshot_id, int(version), kind, canonical_json(manifest), int(clock_ms)),
            )
            for record in records:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO policy_records(
                        snapshot_id, policy_key, generation, record_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        record["key"],
                        int(record.get("generation", 1)),
                        canonical_json(record),
                    ),
                )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('active_policy_snapshot', ?)",
                (snapshot_id,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('policy_pointer_json', ?)",
                (
                    canonical_json(
                        {
                            "snapshot_id": snapshot_id,
                            "version": version,
                            "kind": kind,
                            "clock_ms": clock_ms,
                        }
                    ),
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def get_active_policy_snapshot(self) -> dict[str, Any] | None:
        pointer = self.conn.execute(
            "SELECT value FROM meta WHERE key='policy_pointer_json'"
        ).fetchone()
        if pointer:
            import json

            return json.loads(pointer["value"])
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key='active_policy_snapshot'"
        ).fetchone()
        return {"snapshot_id": row["value"]} if row else None

    def get_policy_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM policy_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if not row:
            return None
        import json

        result = dict(row)
        result["manifest"] = json.loads(result.pop("manifest_json"))
        records = self.conn.execute(
            "SELECT record_json FROM policy_records WHERE snapshot_id=? ORDER BY policy_key",
            (snapshot_id,),
        ).fetchall()
        result["records"] = [json.loads(item["record_json"]) for item in records]
        return result


def atomic_write_json(path: str | Path, value: Any) -> None:
    """Helper used by CLI result export; SQLite remains the recovery authority."""

    write_json(path, value)


def read_json_file(path: str | Path) -> Any:
    return read_json(path)
