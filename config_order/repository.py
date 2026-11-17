"""SQLite persistence for versioned diagnoses and whole-view replacements."""

from __future__ import annotations

import copy
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical import canonical_dumps, sha256_canonical


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS rule_versions (
    version INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    created_session_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    environment_set_version TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    rule_fingerprint TEXT NOT NULL,
    diagnosis_parameters_json TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    report_json TEXT NOT NULL,
    replay_status TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_chains (
    session_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    config_key TEXT NOT NULL,
    digest TEXT NOT NULL,
    chain_json TEXT NOT NULL,
    PRIMARY KEY (session_id, environment_id, config_key),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS session_decisions (
    session_id TEXT NOT NULL,
    environment_set_version TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    config_key TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    code TEXT NOT NULL,
    selected_source TEXT,
    effective_value_json TEXT,
    coverage_json TEXT NOT NULL,
    trace_json TEXT NOT NULL,
    stale INTEGER NOT NULL DEFAULT 0 CHECK (stale IN (0, 1)),
    PRIMARY KEY (session_id, environment_id, config_key),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS current_chains (
    environment_set_version TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    config_key TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    digest TEXT NOT NULL,
    chain_json TEXT NOT NULL,
    PRIMARY KEY (environment_set_version, environment_id, config_key)
);

CREATE TABLE IF NOT EXISTS current_decisions (
    environment_set_version TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    config_key TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    code TEXT NOT NULL,
    selected_source TEXT,
    effective_value_json TEXT,
    coverage_json TEXT NOT NULL,
    trace_json TEXT NOT NULL,
    PRIMARY KEY (environment_set_version, environment_id, config_key)
);

CREATE TABLE IF NOT EXISTS replacements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    config_key TEXT NOT NULL,
    old_rule_version INTEGER,
    new_rule_version INTEGER NOT NULL,
    old_selected_sources_json TEXT NOT NULL,
    new_selected_sources_json TEXT NOT NULL,
    changed_fields_json TEXT NOT NULL,
    change_type TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_session_decisions_version
    ON session_decisions(environment_set_version, rule_version, stale);
CREATE INDEX IF NOT EXISTS idx_current_decisions_version
    ON current_decisions(environment_set_version, rule_version);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_session_id(
    scenario: dict[str, Any],
    diagnosis_parameters: dict[str, Any],
    rule_version: int,
) -> str:
    fingerprint = sha256_canonical(
        {
            "scenario": scenario,
            "diagnosis_parameters": diagnosis_parameters,
            "rule_version": rule_version,
        }
    )
    return f"session-{fingerprint[:24]}"


def _loads(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


def _decision_signature(
    row: sqlite3.Row | dict[str, Any] | None,
    digest: str | None = None,
) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, sqlite3.Row):
        value = {
            "rule_version": row["rule_version"],
            "status": row["status"],
            "code": row["code"],
            "selected_source": row["selected_source"],
            "effective_value": _loads(row["effective_value_json"]),
            "coverage": _loads(row["coverage_json"]),
            "digest": digest,
        }
    else:
        value = {
            "rule_version": row.get("rule_version"),
            "status": row.get("status"),
            "code": row.get("code"),
            "selected_source": row.get("selected_source"),
            "effective_value": row.get("effective_value"),
            "coverage": row.get("coverage"),
            "digest": row.get("digest"),
        }
    return value


class Repository:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Repository":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def get_or_create_rule_version(
        self, fingerprint: str, session_id: str | None
    ) -> tuple[int, bool]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            latest = self.conn.execute(
                "SELECT version, fingerprint FROM rule_versions "
                "ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if latest is not None and latest["fingerprint"] == fingerprint:
                self.conn.commit()
                return int(latest["version"]), False
            max_row = self.conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM rule_versions"
            ).fetchone()
            version = int(max_row["version"]) + 1
            self.conn.execute(
                "INSERT INTO rule_versions(version, fingerprint, created_session_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (version, fingerprint, session_id, utc_now()),
            )
            self.conn.commit()
            return version, True
        except Exception:
            self.conn.rollback()
            raise

    def latest_rule_version(self) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM rule_versions"
        ).fetchone()
        return int(row["version"])

    def _old_current(
        self, environment_set_version: str
    ) -> dict[tuple[str, str], dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT d.environment_id, d.config_key, d.rule_version, d.status,
                   d.code, d.selected_source, d.effective_value_json,
                   d.coverage_json, c.digest
            FROM current_decisions AS d
            LEFT JOIN current_chains AS c
              ON c.environment_set_version = d.environment_set_version
             AND c.environment_id = d.environment_id
             AND c.config_key = d.config_key
            WHERE d.environment_set_version = ?
            ORDER BY d.environment_id, d.config_key
            """,
            (environment_set_version,),
        ).fetchall()
        return {
            (row["environment_id"], row["config_key"]): _decision_signature(
                row, row["digest"]
            )
            for row in rows
        }

    @staticmethod
    def _new_current(
        result: dict[str, Any],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        new: dict[tuple[str, str], dict[str, Any]] = {}
        for environment in result["environments"]:
            for key_report in environment["keys"]:
                decision = key_report["decision"]
                new[(environment["environment_id"], key_report["key"])] = {
                    "status": decision["status"],
                    "code": decision["code"],
                    "selected_source": decision["selected_source"],
                    "effective_value": decision["effective_value"],
                    "coverage": decision["coverage"],
                    "digest": key_report["chain"]["digest"],
                    "decision": decision,
                    "chain": key_report["chain"],
                }
        return new

    def _calculate_changes(
        self,
        old: dict[tuple[str, str], dict[str, Any]],
        new: dict[tuple[str, str], dict[str, Any]],
        new_rule_version: int,
    ) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        for environment_id, config_key in sorted(set(old) | set(new)):
            old_value = old.get((environment_id, config_key))
            new_value = new.get((environment_id, config_key))
            if old_value is None:
                changed_fields = ["key_added"]
                change_type = "added"
            elif new_value is None:
                changed_fields = ["key_removed"]
                change_type = "removed"
            else:
                candidates = {
                    "status": (old_value["status"], new_value["status"]),
                    "code": (old_value["code"], new_value["code"]),
                    "selected_source": (
                        old_value["selected_source"],
                        new_value["selected_source"],
                    ),
                    "effective_value": (
                        old_value["effective_value"],
                        new_value["effective_value"],
                    ),
                    "coverage": (old_value["coverage"], new_value["coverage"]),
                    "chain_digest": (old_value["digest"], new_value["digest"]),
                }
                changed_fields = [
                    field
                    for field in sorted(candidates)
                    if canonical_dumps(candidates[field][0])
                    != canonical_dumps(candidates[field][1])
                ]
                if not changed_fields:
                    continue
                change_type = "modified"
            changes.append(
                {
                    "environment_id": environment_id,
                    "key": config_key,
                    "change_type": change_type,
                    "changed_fields": changed_fields,
                    "old_rule_version": (
                        old_value.get("rule_version")
                        if old_value is not None
                        else None
                    ),
                    "new_rule_version": new_rule_version,
                    "old_selected_sources": (
                        [old_value["selected_source"]]
                        if old_value and old_value["selected_source"] is not None
                        else []
                    ),
                    "new_selected_sources": (
                        [new_value["selected_source"]]
                        if new_value and new_value["selected_source"] is not None
                        else []
                    ),
                }
            )
        return changes

    def _insert_history(
        self,
        session_id: str,
        scenario: dict[str, Any],
        result: dict[str, Any],
        rule_version: int,
    ) -> None:
        environment_set_version = result["environment_set_version"]
        self.conn.execute(
            "UPDATE session_decisions SET stale = 1 "
            "WHERE environment_set_version = ? AND rule_version <> ?",
            (environment_set_version, rule_version),
        )
        for environment in result["environments"]:
            env_id = environment["environment_id"]
            for key_report in environment["keys"]:
                decision = key_report["decision"]
                chain = key_report["chain"]
                self.conn.execute(
                    "INSERT INTO session_chains"
                    "(session_id, environment_id, config_key, digest, chain_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        session_id,
                        env_id,
                        key_report["key"],
                        chain["digest"],
                        canonical_dumps(chain),
                    ),
                )
                self.conn.execute(
                    "INSERT INTO session_decisions"
                    "(session_id, environment_set_version, environment_id, config_key, "
                    " rule_version, status, code, selected_source, effective_value_json, "
                    " coverage_json, trace_json, stale) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        environment_set_version,
                        env_id,
                        key_report["key"],
                        rule_version,
                        decision["status"],
                        decision["code"],
                        decision["selected_source"],
                        canonical_dumps(decision["effective_value"]),
                        canonical_dumps(decision["coverage"]),
                        canonical_dumps(decision["trace"]),
                        0,
                    ),
                )

    def _replace_current(
        self,
        environment_set_version: str,
        result: dict[str, Any],
        rule_version: int,
    ) -> None:
        self.conn.execute(
            "DELETE FROM current_chains WHERE environment_set_version = ?",
            (environment_set_version,),
        )
        self.conn.execute(
            "DELETE FROM current_decisions WHERE environment_set_version = ?",
            (environment_set_version,),
        )
        for environment in result["environments"]:
            env_id = environment["environment_id"]
            for key_report in environment["keys"]:
                decision = key_report["decision"]
                chain = key_report["chain"]
                self.conn.execute(
                    "INSERT INTO current_chains"
                    "(environment_set_version, environment_id, config_key, "
                    " rule_version, digest, chain_json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        environment_set_version,
                        env_id,
                        key_report["key"],
                        rule_version,
                        chain["digest"],
                        canonical_dumps(chain),
                    ),
                )
                self.conn.execute(
                    "INSERT INTO current_decisions"
                    "(environment_set_version, environment_id, config_key, "
                    " rule_version, status, code, selected_source, "
                    " effective_value_json, coverage_json, trace_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        environment_set_version,
                        env_id,
                        key_report["key"],
                        rule_version,
                        decision["status"],
                        decision["code"],
                        decision["selected_source"],
                        canonical_dumps(decision["effective_value"]),
                        canonical_dumps(decision["coverage"]),
                        canonical_dumps(decision["trace"]),
                    ),
                )

    def current_statistics(self, environment_set_version: str) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT code, COUNT(*) AS count FROM current_decisions "
            "WHERE environment_set_version = ? GROUP BY code ORDER BY code",
            (environment_set_version,),
        ).fetchall()
        by_code = {row["code"]: int(row["count"]) for row in rows}
        return {
            "keys": sum(by_code.values()),
            "resolved": by_code.get("OK", 0),
            "conflict": by_code.get("E_CONFLICT", 0),
            "cycle": by_code.get("E_CYCLE", 0),
            "chain_integrity": by_code.get("E_CHAIN", 0),
            "by_code": by_code,
        }

    def persist_diagnosis(
        self,
        scenario: dict[str, Any],
        result: dict[str, Any],
        diagnosis_parameters: dict[str, Any],
        rule_version: int,
        rule_fp: str,
    ) -> dict[str, Any]:
        session_id = result["session_id"]
        environment_set_version = result["environment_set_version"]
        new_current = self._new_current(result)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            old_current = self._old_current(environment_set_version)
            changes = self._calculate_changes(
                old_current, new_current, rule_version
            )
            delete_row = self.conn.execute(
                "SELECT session_id FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if delete_row is not None:
                self.conn.execute(
                    "DELETE FROM sessions WHERE session_id = ?", (session_id,)
                )

            snapshot = {
                "schema_version": "1.0",
                "session_id": session_id,
                "environment_set_version": environment_set_version,
                "rule_version": rule_version,
                "rule_fingerprint": rule_fp,
                "diagnosis_parameters": diagnosis_parameters,
                "scenario": copy.deepcopy(scenario),
                "result": copy.deepcopy(result),
            }
            result["session_id"] = session_id
            result["rule_version"] = rule_version
            result["rule_fingerprint"] = rule_fp
            result["diagnosis_parameters"] = diagnosis_parameters
            result["input_fingerprint"] = sha256_canonical(scenario)
            result["replacement"] = {
                "strategy": "whole-view-replace",
                "environment_set_version": environment_set_version,
                "changes": changes,
                "changed_key_count": len(changes),
            }

            self.conn.execute(
                "INSERT INTO sessions"
                "(session_id, mode, environment_set_version, rule_version, "
                " rule_fingerprint, diagnosis_parameters_json, input_fingerprint, "
                " snapshot_json, report_json, replay_status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    "diagnose",
                    environment_set_version,
                    rule_version,
                    rule_fp,
                    canonical_dumps(diagnosis_parameters),
                    result["input_fingerprint"],
                    canonical_dumps(snapshot),
                    canonical_dumps(result),
                    "NOT_RUN",
                    utc_now(),
                ),
            )
            self._insert_history(session_id, scenario, result, rule_version)
            self._replace_current(
                environment_set_version, result, rule_version
            )
            for change in changes:
                self.conn.execute(
                    "INSERT INTO replacements"
                    "(session_id, environment_id, config_key, old_rule_version, "
                    " new_rule_version, old_selected_sources_json, "
                    " new_selected_sources_json, changed_fields_json, change_type) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        change["environment_id"],
                        change["key"],
                        change["old_rule_version"],
                        change["new_rule_version"],
                        canonical_dumps(change["old_selected_sources"]),
                        canonical_dumps(change["new_selected_sources"]),
                        canonical_dumps(change["changed_fields"]),
                        change["change_type"],
                    ),
                )
            result["current_statistics"] = self.current_statistics(
                environment_set_version
            )
            snapshot["result"] = copy.deepcopy(result)
            self.conn.execute(
                "UPDATE sessions SET snapshot_json = ?, report_json = ? "
                "WHERE session_id = ?",
                (
                    canonical_dumps(snapshot),
                    canonical_dumps(result),
                    session_id,
                ),
            )
            self.conn.commit()
            return result
        except Exception:
            self.conn.rollback()
            raise

    def load_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT snapshot_json, report_json, rule_version, replay_status "
            "FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "snapshot": json.loads(row["snapshot_json"]),
            "report": json.loads(row["report_json"]),
            "rule_version": int(row["rule_version"]),
            "replay_status": row["replay_status"],
        }

    def list_sessions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT session_id, environment_set_version, rule_version, "
            "report_json, replay_status, created_at "
            "FROM sessions ORDER BY created_at, session_id"
        ).fetchall()
        result = []
        for row in rows:
            report = json.loads(row["report_json"])
            result.append(
                {
                    "session_id": row["session_id"],
                    "environment_set_version": row["environment_set_version"],
                    "rule_version": row["rule_version"],
                    "overall_code": report.get("overall_code"),
                    "replay_status": row["replay_status"],
                }
            )
        return result

    def set_replay_status(self, session_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE sessions SET replay_status = ? WHERE session_id = ?",
            (status, session_id),
        )
        self.conn.commit()

    def restore_current_view(
        self,
        session_id: str,
        scenario: dict[str, Any],
        result: dict[str, Any],
        rule_version: int,
    ) -> dict[str, Any]:
        environment_set_version = result["environment_set_version"]
        new_current = self._new_current(result)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            old_current = self._old_current(environment_set_version)
            changes = self._calculate_changes(
                old_current, new_current, rule_version
            )
            self.conn.execute(
                "UPDATE session_decisions SET stale = CASE "
                "WHEN rule_version = ? AND environment_set_version = ? THEN 0 ELSE 1 END "
                "WHERE environment_set_version = ?",
                (
                    rule_version,
                    environment_set_version,
                    environment_set_version,
                ),
            )
            self._replace_current(
                environment_set_version, result, rule_version
            )
            self.conn.execute(
                "UPDATE sessions SET replay_status = ? WHERE session_id = ?",
                ("PASS", session_id),
            )
            self.conn.commit()
            return {
                "strategy": "whole-view-replace",
                "environment_set_version": environment_set_version,
                "restored_rule_version": rule_version,
                "changes": changes,
                "changed_key_count": len(changes),
                "current_statistics": self.current_statistics(
                    environment_set_version
                ),
            }
        except Exception:
            self.conn.rollback()
            raise

    def current_rows(self, environment_set_version: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM current_decisions "
            "WHERE environment_set_version = ? "
            "ORDER BY environment_id, config_key",
            (environment_set_version,),
        ).fetchall()
        return [dict(row) for row in rows]
