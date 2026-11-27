"""sqlite3 persistence for normalized projects, results and evaluations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .model import canonical_json


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_run_id(model: dict[str, Any], source_sha256: str) -> str:
    material = {
        "source_schema": model["source_schema"],
        "source_sha256": source_sha256,
        "normalized": {
            key: model[key]
            for key in (
                "canonical_schema",
                "project_id",
                "domain_id",
                "horizon",
                "connections",
                "rules",
                "pools",
                "events",
            )
        },
    }
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


class ArtifactStore:
    """Small, explicit schema so operators can inspect every persisted result."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ArtifactStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS projects (
                run_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                source_schema TEXT NOT NULL,
                source_path TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                normalized_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS results (
                run_id TEXT PRIMARY KEY,
                result_json TEXT NOT NULL,
                reference_json TEXT NOT NULL,
                reference_ok INTEGER NOT NULL,
                reference_check_json TEXT NOT NULL,
                result_digest TEXT NOT NULL,
                reference_digest TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES projects(run_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS sessions (
                run_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                greenhouse_id TEXT,
                final_state TEXT NOT NULL,
                require_rebuild INTEGER NOT NULL,
                mapping_json TEXT,
                trace_json TEXT NOT NULL,
                PRIMARY KEY (run_id, session_id),
                FOREIGN KEY (run_id) REFERENCES projects(run_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS impacts (
                run_id TEXT NOT NULL,
                change_event_id TEXT NOT NULL,
                time INTEGER NOT NULL,
                affected_count INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (run_id, change_event_id),
                FOREIGN KEY (run_id) REFERENCES projects(run_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS residual_cleanups (
                run_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                expired_at INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (run_id, session_id, expired_at),
                FOREIGN KEY (run_id) REFERENCES projects(run_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS groups (
                run_id TEXT NOT NULL,
                greenhouse_id TEXT NOT NULL,
                rule_id TEXT NOT NULL,
                session_count INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (run_id, greenhouse_id, rule_id),
                FOREIGN KEY (run_id) REFERENCES projects(run_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS evaluations (
                evaluation_id TEXT PRIMARY KEY,
                ok INTEGER NOT NULL,
                run_ids_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_connection
                ON sessions(run_id, connection_id);
            CREATE INDEX IF NOT EXISTS idx_impacts_time
                ON impacts(run_id, time);
            """
        )
        self.connection.commit()

    def store_run(
        self,
        model: dict[str, Any],
        result: dict[str, Any],
        reference_result: dict[str, Any],
        reference_check: dict[str, Any],
        *,
        source_path: str,
        source_sha256: str,
    ) -> str:
        run_id = make_run_id(model, source_sha256)
        recorded_at = _now()
        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO projects
                    (run_id, project_id, source_schema, source_path,
                     source_sha256, normalized_json, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    model["project_id"],
                    model["source_schema"],
                    source_path,
                    source_sha256,
                    _dumps(model),
                    recorded_at,
                ),
            )
            self.connection.execute(
                """
                INSERT OR REPLACE INTO results
                    (run_id, result_json, reference_json, reference_ok,
                     reference_check_json, result_digest, reference_digest)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    _dumps(result),
                    _dumps(reference_result),
                    1 if reference_check.get("ok") else 0,
                    _dumps(reference_check),
                    hashlib.sha256(_dumps(result).encode("utf-8")).hexdigest(),
                    hashlib.sha256(_dumps(reference_result).encode("utf-8")).hexdigest(),
                ),
            )
            self.connection.execute(
                "DELETE FROM sessions WHERE run_id = ?", (run_id,)
            )
            self.connection.execute(
                "DELETE FROM impacts WHERE run_id = ?", (run_id,)
            )
            self.connection.execute(
                "DELETE FROM residual_cleanups WHERE run_id = ?", (run_id,)
            )
            self.connection.execute(
                "DELETE FROM groups WHERE run_id = ?", (run_id,)
            )

            for session in result["sessions"]:
                self.connection.execute(
                    """
                    INSERT INTO sessions
                        (run_id, session_id, connection_id, greenhouse_id,
                         final_state, require_rebuild, mapping_json, trace_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        session["session_id"],
                        session["connection_id"],
                        session["greenhouse_id"],
                        session["state"],
                        1 if session["require_rebuild"] else 0,
                        _dumps(session["mapping"]),
                        _dumps(session["trace"]),
                    ),
                )
            for impact in result["impacts"]:
                self.connection.execute(
                    """
                    INSERT INTO impacts
                        (run_id, change_event_id, time, affected_count, payload_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        impact["change_event_id"],
                        impact["time"],
                        len(impact["affected"]),
                        _dumps(impact),
                    ),
                )
            for cleanup in result["residual_cleanups"]:
                self.connection.execute(
                    """
                    INSERT INTO residual_cleanups
                        (run_id, session_id, expired_at, payload_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        cleanup["session_id"],
                        cleanup["expired_at"],
                        _dumps(cleanup),
                    ),
                )
            for group in result["reports"]["groups"]:
                self.connection.execute(
                    """
                    INSERT INTO groups
                        (run_id, greenhouse_id, rule_id, session_count, payload_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        group["greenhouse_id"],
                        group["rule_id"],
                        group["session_count"],
                        _dumps(group),
                    ),
                )
        return run_id

    def store_evaluation(
        self,
        payload: dict[str, Any],
        run_ids: list[str],
    ) -> str:
        evaluation_id = hashlib.sha256(
            canonical_json(
                {"run_ids": sorted(run_ids), "payload": payload}
            ).encode("utf-8")
        ).hexdigest()
        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO evaluations
                    (evaluation_id, ok, run_ids_json, payload_json, recorded_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    evaluation_id,
                    1 if payload.get("ok") else 0,
                    _dumps(sorted(run_ids)),
                    _dumps(payload),
                    _now(),
                ),
            )
        return evaluation_id

    def get_result(self, run_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT result_json FROM results WHERE run_id = ?", (run_id,)
        ).fetchone()
        return json.loads(row["result_json"]) if row else None

    def counts(self) -> dict[str, int]:
        names = (
            "projects",
            "results",
            "sessions",
            "impacts",
            "residual_cleanups",
            "groups",
            "evaluations",
        )
        return {
            name: self.connection.execute(
                f"SELECT COUNT(*) AS count FROM {name}"
            ).fetchone()["count"]
            for name in names
        }

