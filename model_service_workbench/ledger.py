"""SQLite ledger, immutable snapshots, recovery and deterministic replay."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .declaration import diff_declarations, load_declaration_text
from .errors import ReplayError, StateError
from .models import (
    ClusterDeclaration,
    MinuteDecision,
    VersionRegistration,
    canonical_json,
    canonical_sha256,
)
from .scheduler import schedule_minute


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS declarations (
    version INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    source_path TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    authored_version INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_declarations_fingerprint
    ON declarations(fingerprint);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    declaration_version INTEGER NOT NULL REFERENCES declarations(version),
    declaration_fingerprint TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_id TEXT,
    total_minutes INTEGER NOT NULL,
    completed_minutes INTEGER NOT NULL DEFAULT 0,
    last_completed_minute INTEGER,
    in_flight_minute INTEGER,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'interrupted')),
    is_current INTEGER NOT NULL DEFAULT 0,
    is_stale INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_current ON runs(is_current, is_stale);

CREATE TABLE IF NOT EXISTS minute_decisions (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    minute INTEGER NOT NULL,
    declaration_version INTEGER NOT NULL,
    budget_limit INTEGER NOT NULL,
    budget_requested_total INTEGER NOT NULL,
    budget_used INTEGER NOT NULL,
    requests_total INTEGER NOT NULL,
    accepted_total INTEGER NOT NULL,
    rejected_total INTEGER NOT NULL,
    estimated_work_ms_total INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    is_stale INTEGER NOT NULL DEFAULT 0,
    committed_at TEXT NOT NULL,
    PRIMARY KEY (run_id, minute)
);

CREATE TABLE IF NOT EXISTS service_decisions (
    run_id TEXT NOT NULL,
    minute INTEGER NOT NULL,
    service TEXT NOT NULL,
    request_count INTEGER NOT NULL,
    capacity_per_replica INTEGER NOT NULL,
    unclamped_demand INTEGER NOT NULL,
    demand_replicas INTEGER NOT NULL,
    concurrency_quota INTEGER NOT NULL,
    final_replicas INTEGER NOT NULL,
    capacity_requests INTEGER NOT NULL,
    accepted_requests INTEGER NOT NULL,
    rejected_requests INTEGER NOT NULL,
    clip_reasons_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    is_stale INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, minute, service),
    FOREIGN KEY (run_id, minute)
        REFERENCES minute_decisions(run_id, minute) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    source_run_id TEXT NOT NULL,
    declaration_version INTEGER NOT NULL,
    declaration_fingerprint TEXT NOT NULL,
    total_minutes INTEGER NOT NULL,
    completed_minutes INTEGER NOT NULL,
    payload_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    file_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS version_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    previous_version INTEGER,
    new_version INTEGER NOT NULL,
    affected_services_json TEXT NOT NULL,
    affected_minute_range_json TEXT NOT NULL,
    differences_json TEXT NOT NULL,
    stale_run_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_load(value: str) -> Any:
    return json.loads(value)


class Ledger:
    """Durable local decision ledger."""

    def __init__(
        self,
        database_path: str | Path,
        snapshot_dir: str | Path | None = None,
    ) -> None:
        self.path = Path(database_path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir = (
            Path(snapshot_dir).resolve()
            if snapshot_dir is not None
            else self.path.parent / "snapshots"
        )
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), timeout=30.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        self.connection.executescript(SCHEMA_SQL)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def register_declaration(
        self,
        declaration: ClusterDeclaration,
        source_path: str | None = None,
    ) -> VersionRegistration:
        """Register content-addressed declaration and invalidate old views."""

        source = source_path or declaration.source_path
        latest = self.connection.execute(
            "SELECT * FROM declarations ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if latest is not None and latest["fingerprint"] == declaration.fingerprint:
            cached = load_declaration_text(
                latest["raw_text"],
                latest["source_path"],
            ).with_version(int(latest["version"]))
            return VersionRegistration(
                declaration=cached,
                version_changed=False,
                previous_version=None,
            )

        previous_version = int(latest["version"]) if latest else None
        if latest is not None:
            resolved_version = max(
                declaration.authored_version,
                previous_version + 1,
            )
            previous = load_declaration_text(
                latest["raw_text"],
                latest["source_path"],
            ).with_version(previous_version)
        else:
            resolved_version = declaration.authored_version
            previous = None

        resolved = declaration.with_version(resolved_version)
        diff = diff_declarations(previous, resolved)
        stale_rows = self.connection.execute(
            "SELECT run_id FROM runs WHERE is_stale = 0 OR is_current = 1 ORDER BY run_id"
        ).fetchall()
        stale_run_ids = tuple(row["run_id"] for row in stale_rows)

        with self.connection:
            self.connection.execute(
                """
                INSERT INTO declarations(
                    version, fingerprint, source_path, raw_text,
                    canonical_json, authored_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolved_version,
                    resolved.fingerprint,
                    source,
                    resolved.raw_text,
                    canonical_json(resolved.normalized_document()),
                    resolved.authored_version,
                    _now(),
                ),
            )
            self.connection.execute(
                "UPDATE runs SET is_stale = 1, is_current = 0"
            )
            self.connection.execute(
                "UPDATE minute_decisions SET is_stale = 1"
            )
            self.connection.execute(
                "UPDATE service_decisions SET is_stale = 1"
            )
            self.connection.execute(
                """
                INSERT INTO version_events(
                    previous_version, new_version, affected_services_json,
                    affected_minute_range_json, differences_json,
                    stale_run_ids_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    previous_version,
                    resolved_version,
                    canonical_json(list(diff["affected_services"])),
                    canonical_json(
                        list(diff["affected_minute_range"])
                        if diff["affected_minute_range"] is not None
                        else None
                    ),
                    canonical_json(list(diff["differences"])),
                    canonical_json(list(stale_run_ids)),
                    _now(),
                ),
            )

        return VersionRegistration(
            declaration=resolved,
            version_changed=True,
            previous_version=previous_version,
            differences=diff["differences"],
            affected_services=diff["affected_services"],
            affected_minute_range=diff["affected_minute_range"],
            stale_run_ids=stale_run_ids,
        )

    def load_declaration(self, version: int) -> ClusterDeclaration:
        row = self.connection.execute(
            "SELECT * FROM declarations WHERE version = ?",
            (int(version),),
        ).fetchone()
        if row is None:
            raise StateError(
                "declaration version is not present in the ledger",
                "$.declaration_version",
                {"declaration_version": version},
            )
        declaration = load_declaration_text(
            row["raw_text"],
            row["source_path"],
        ).with_version(int(row["version"]))
        if declaration.fingerprint != row["fingerprint"]:
            raise StateError(
                "stored declaration fingerprint does not match its raw text",
                "$.declarations.fingerprint",
                {"declaration_version": version},
            )
        return declaration

    def latest_declaration(self) -> ClusterDeclaration | None:
        row = self.connection.execute(
            "SELECT * FROM declarations ORDER BY version DESC LIMIT 1"
        ).fetchone()
        return self.load_declaration(row["version"]) if row else None

    def list_version_events(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM version_events ORDER BY event_id"
        ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "previous_version": row["previous_version"],
                "new_version": row["new_version"],
                "affected_services": _json_load(row["affected_services_json"]),
                "affected_minute_range": _json_load(row["affected_minute_range_json"]),
                "differences": _json_load(row["differences_json"]),
                "stale_run_ids": _json_load(row["stale_run_ids_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _ensure_declaration_row(self, declaration: ClusterDeclaration) -> None:
        row = self.connection.execute(
            "SELECT * FROM declarations WHERE version = ?",
            (declaration.declaration_version,),
        ).fetchone()
        if row is None:
            with self.connection:
                self.connection.execute(
                    """
                    INSERT INTO declarations(
                        version, fingerprint, source_path, raw_text,
                        canonical_json, authored_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        declaration.declaration_version,
                        declaration.fingerprint,
                        declaration.source_path,
                        declaration.raw_text,
                        canonical_json(declaration.normalized_document()),
                        declaration.authored_version,
                        _now(),
                    ),
                )
            return
        if row["fingerprint"] != declaration.fingerprint:
            raise StateError(
                "declaration version is already bound to different content",
                "$.declaration_version",
                {"declaration_version": declaration.declaration_version},
            )

    def start_run(
        self,
        declaration: ClusterDeclaration,
        *,
        source_kind: str = "live",
        source_id: str | None = None,
    ) -> str:
        """Create a new current run and retire every previous conclusion."""

        self._ensure_declaration_row(declaration)
        with self.connection:
            count = int(
                self.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            )
            run_id = f"run-v{declaration.declaration_version}-{count + 1:04d}"
            self.connection.execute(
                "UPDATE runs SET is_current = 0, is_stale = 1"
            )
            self.connection.execute(
                "UPDATE minute_decisions SET is_stale = 1"
            )
            self.connection.execute(
                "UPDATE service_decisions SET is_stale = 1"
            )
            self.connection.execute(
                """
                INSERT INTO runs(
                    run_id, declaration_version, declaration_fingerprint,
                    source_path, source_kind, source_id, total_minutes,
                    completed_minutes, last_completed_minute, in_flight_minute,
                    status, is_current, is_stale, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, 0, 'running', 1, 0, ?)
                """,
                (
                    run_id,
                    declaration.declaration_version,
                    declaration.fingerprint,
                    declaration.source_path,
                    source_kind,
                    source_id,
                    declaration.total_minutes,
                    _now(),
                ),
            )
        return run_id

    def begin_minute(self, run_id: str, minute: int) -> None:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE runs
                SET in_flight_minute = ?, status = 'running'
                WHERE run_id = ?
                """,
                (int(minute), run_id),
            )
            if cursor.rowcount != 1:
                raise StateError("run does not exist", "$.run_id", {"run_id": run_id})

    def commit_minute(self, run_id: str, decision: MinuteDecision) -> None:
        """Write one complete minute as a single SQLite transaction."""

        run = self.get_run(run_id)
        if run is None:
            raise StateError("run does not exist", "$.run_id", {"run_id": run_id})
        expected_minute = int(run["completed_minutes"])
        if decision.minute != expected_minute:
            raise StateError(
                "minutes must be committed in contiguous ascending order",
                f"$.runs[{run_id}].minutes[{decision.minute}]",
                {"expected_minute": expected_minute, "actual_minute": decision.minute},
            )
        if decision.declaration_version != run["declaration_version"]:
            raise StateError(
                "minute declaration version does not match its run",
                f"$.runs[{run_id}].minutes[{decision.minute}].declaration_version",
                {
                    "expected": run["declaration_version"],
                    "actual": decision.declaration_version,
                },
            )

        next_minute = decision.minute + 1
        completed = next_minute >= run["total_minutes"]
        status = "completed" if completed else "running"
        in_flight = None if completed else next_minute
        minute_payload = canonical_json(decision.to_dict())
        committed_at = _now()
        try:
            with self.connection:
                self.connection.execute(
                    """
                    INSERT INTO minute_decisions(
                        run_id, minute, declaration_version, budget_limit,
                        budget_requested_total, budget_used, requests_total,
                        accepted_total, rejected_total, estimated_work_ms_total,
                        payload_json, is_stale, committed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        run_id,
                        decision.minute,
                        decision.declaration_version,
                        decision.budget_limit,
                        decision.budget_requested_total,
                        decision.budget_used,
                        decision.requests_total,
                        decision.accepted_total,
                        decision.rejected_total,
                        decision.estimated_work_ms_total,
                        minute_payload,
                        committed_at,
                    ),
                )
                for service in decision.services:
                    payload = canonical_json(service.to_dict())
                    self.connection.execute(
                        """
                        INSERT INTO service_decisions(
                            run_id, minute, service, request_count,
                            capacity_per_replica, unclamped_demand,
                            demand_replicas, concurrency_quota, final_replicas,
                            capacity_requests, accepted_requests,
                            rejected_requests, clip_reasons_json,
                            payload_json, is_stale
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                        """,
                        (
                            run_id,
                            decision.minute,
                            service.service,
                            service.request_count,
                            service.capacity_per_replica,
                            service.unclamped_demand,
                            service.demand_replicas,
                            service.concurrency_quota,
                            service.final_replicas,
                            service.capacity_requests,
                            service.accepted_requests,
                            service.rejected_requests,
                            canonical_json([item.to_dict() for item in service.clip_reasons]),
                            payload,
                        ),
                    )
                self.connection.execute(
                    """
                    UPDATE runs
                    SET completed_minutes = ?, last_completed_minute = ?,
                        in_flight_minute = ?, status = ?
                    WHERE run_id = ?
                    """,
                    (
                        next_minute,
                        decision.minute,
                        in_flight,
                        status,
                        run_id,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise StateError(
                "minute commit violated ledger integrity",
                f"$.runs[{run_id}].minutes[{decision.minute}]",
                {"reason": str(exc)},
            ) from exc

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_current_run(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT * FROM runs
            WHERE is_current = 1 AND is_stale = 0
            ORDER BY created_at DESC, run_id DESC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row is not None else None

    def list_runs(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM runs ORDER BY created_at, run_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_decisions(self, run_id: str) -> list[dict[str, Any]]:
        minute_rows = self.connection.execute(
            "SELECT * FROM minute_decisions WHERE run_id = ? ORDER BY minute",
            (run_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for minute_row in minute_rows:
            service_rows = self.connection.execute(
                """
                SELECT * FROM service_decisions
                WHERE run_id = ? AND minute = ?
                ORDER BY service
                """,
                (run_id, minute_row["minute"]),
            ).fetchall()
            result.append(
                {
                    "minute": minute_row["minute"],
                    "declaration_version": minute_row["declaration_version"],
                    "budget_limit": minute_row["budget_limit"],
                    "budget_requested_total": minute_row["budget_requested_total"],
                    "budget_used": minute_row["budget_used"],
                    "requests_total": minute_row["requests_total"],
                    "accepted_total": minute_row["accepted_total"],
                    "rejected_total": minute_row["rejected_total"],
                    "estimated_work_ms_total": minute_row["estimated_work_ms_total"],
                    "is_stale": bool(minute_row["is_stale"]),
                    "committed_at": minute_row["committed_at"],
                    "services": [
                        _json_load(service_row["payload_json"])
                        for service_row in service_rows
                    ],
                }
            )
        return result

    def get_current_view(self) -> dict[str, Any]:
        run = self.get_current_run()
        if run is None:
            return {"run": None, "minutes": [], "snapshots": self.list_snapshots()}
        return {
            "run": run,
            "minutes": self.get_decisions(run["run_id"]),
            "snapshots": self.list_snapshots(),
        }

    def _contiguous_complete_prefix(self, run_id: str) -> tuple[int, list[int]]:
        run = self.get_run(run_id)
        if run is None:
            raise StateError("run does not exist", "$.run_id", {"run_id": run_id})
        declaration = self.load_declaration(run["declaration_version"])
        expected_names = sorted(declaration.service_names)
        minute_rows = self.connection.execute(
            "SELECT minute FROM minute_decisions WHERE run_id = ? ORDER BY minute",
            (run_id,),
        ).fetchall()
        rows_by_minute = {int(row["minute"]): row for row in minute_rows}
        completed = 0
        noncontiguous: list[int] = []
        for minute in range(int(run["total_minutes"])):
            if minute not in rows_by_minute:
                break
            service_rows = self.connection.execute(
                """
                SELECT service FROM service_decisions
                WHERE run_id = ? AND minute = ?
                ORDER BY service
                """,
                (run_id, minute),
            ).fetchall()
            names = [row["service"] for row in service_rows]
            if names != expected_names:
                break
            completed += 1
        for minute in sorted(rows_by_minute):
            if minute >= completed:
                noncontiguous.append(minute)
        return completed, noncontiguous

    def recover_run(self, run_id: str) -> dict[str, Any]:
        """Recover only a contiguous prefix of complete minute transactions."""

        run = self.get_run(run_id)
        if run is None:
            raise StateError("run does not exist", "$.run_id", {"run_id": run_id})
        completed, noncontiguous = self._contiguous_complete_prefix(run_id)
        total = int(run["total_minutes"])
        pending = list(range(completed, total))
        unknown = list(pending)
        with self.connection:
            if noncontiguous:
                placeholders = ",".join("?" for _ in noncontiguous)
                self.connection.execute(
                    f"DELETE FROM service_decisions WHERE run_id = ? AND minute IN ({placeholders})",
                    (run_id, *noncontiguous),
                )
                self.connection.execute(
                    f"DELETE FROM minute_decisions WHERE run_id = ? AND minute IN ({placeholders})",
                    (run_id, *noncontiguous),
                )
            self.connection.execute(
                """
                UPDATE runs
                SET completed_minutes = ?, last_completed_minute = ?,
                    in_flight_minute = ?, status = ?
                WHERE run_id = ?
                """,
                (
                    completed,
                    completed - 1 if completed else None,
                    pending[0] if pending else None,
                    "interrupted" if pending else "completed",
                    run_id,
                ),
            )
        recovered_run = self.get_run(run_id)
        return {
            "ok": True,
            "code": None,
            "report_kind": "recovery",
            "exit_report_available": not pending,
            "run_id": run_id,
            "declaration_version": run["declaration_version"],
            "total_minutes": total,
            "recovered_minutes": completed,
            "recovered_minute_range": [0, completed - 1] if completed else None,
            "pending_minutes": pending,
            "unknown_minutes": unknown,
            "first_unknown_minute": pending[0] if pending else None,
            "requires_rerun": bool(pending),
            "discarded_noncontiguous_minutes": noncontiguous,
            "status": recovered_run["status"] if recovered_run else None,
            "is_stale": bool(recovered_run["is_stale"]) if recovered_run else None,
        }

    def _compare_replayed_service(
        self,
        *,
        run_id: str,
        minute: int,
        computed: Any,
        expected: Mapping[str, Any],
        basis: Mapping[str, Any],
    ) -> None:
        fields = (
            "request_count",
            "capacity_per_replica",
            "unclamped_demand",
            "demand_replicas",
            "final_replicas",
            "accepted_requests",
            "rejected_requests",
            "clip_reasons",
        )
        computed_dict = computed.to_dict() if hasattr(computed, "to_dict") else dict(computed)
        for field in fields:
            actual = computed_dict.get(field)
            recorded = expected.get(field)
            if actual != recorded:
                raise ReplayError(
                    "replayed decision differs from the ledger",
                    minute=minute,
                    service=str(computed_dict.get("service")),
                    field=field,
                    details={
                        "run_id": run_id,
                        "expected": recorded,
                        "actual": actual,
                        **dict(basis),
                    },
                )

    def _validate_declaration_basis(
        self,
        declaration: ClusterDeclaration,
        run: Mapping[str, Any],
        supplied: ClusterDeclaration | None,
    ) -> dict[str, Any]:
        if declaration.fingerprint != run["declaration_fingerprint"]:
            raise ReplayError(
                "the recorded declaration fingerprint is unavailable",
                field="declaration_fingerprint",
                details={
                    "reason": "declaration_fingerprint_mismatch",
                    "recorded": run["declaration_fingerprint"],
                    "available": declaration.fingerprint,
                },
            )
        basis = {
            "declaration_version": run["declaration_version"],
            "declaration_fingerprint": run["declaration_fingerprint"],
            "basis": "stored immutable declaration",
        }
        if supplied is None:
            return basis
        if supplied.declaration_version != run["declaration_version"]:
            raise ReplayError(
                "supplied declaration version differs from the recorded version",
                field="declaration_version",
                details={
                    "reason": "declaration_version_mismatch",
                    "recorded_declaration_version": run["declaration_version"],
                    "supplied_declaration_version": supplied.declaration_version,
                },
            )
        if supplied.fingerprint != run["declaration_fingerprint"]:
            recorded_map = declaration.service_map
            supplied_map = supplied.service_map
            for service in sorted(set(recorded_map) | set(supplied_map)):
                if service not in recorded_map or service not in supplied_map:
                    raise ReplayError(
                        "supplied declaration service set differs from the recorded load",
                        service=service,
                        field="requests",
                        details={
                            "reason": "load_sequence_mismatch",
                            "recorded_service_present": service in recorded_map,
                            "supplied_service_present": service in supplied_map,
                        },
                    )
                old = recorded_map[service].requests
                new = supplied_map[service].requests
                horizon = max(len(old), len(new))
                for minute in range(horizon):
                    old_value = old[minute] if minute < len(old) else None
                    new_value = new[minute] if minute < len(new) else None
                    if old_value != new_value:
                        raise ReplayError(
                            "supplied load sequence differs from the recorded load",
                            minute=minute,
                            service=service,
                            field="requests",
                            details={
                                "reason": "load_sequence_mismatch",
                                "recorded": old_value,
                                "supplied": new_value,
                            },
                        )
            raise ReplayError(
                "declaration content differs but the load sequences are equal",
                field="declaration_fingerprint",
                details={
                    "reason": "declaration_content_mismatch",
                    "recorded_fingerprint": run["declaration_fingerprint"],
                    "supplied_fingerprint": supplied.fingerprint,
                },
            )
        return basis

    def replay_run(
        self,
        run_id: str,
        supplied_declaration: ClusterDeclaration | None = None,
    ) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run is None:
            raise StateError("run does not exist", "$.run_id", {"run_id": run_id})
        declaration = self.load_declaration(run["declaration_version"])
        basis = self._validate_declaration_basis(
            declaration,
            run,
            supplied_declaration,
        )
        completed, noncontiguous = self._contiguous_complete_prefix(run_id)
        if completed != int(run["completed_minutes"]):
            raise ReplayError(
                "ledger progress disagrees with the contiguous complete minutes",
                minute=completed,
                details={
                    "reason": "incomplete_minute_prefix",
                    "recorded_completed_minutes": run["completed_minutes"],
                    "contiguous_completed_minutes": completed,
                    "noncontiguous_minutes": noncontiguous,
                },
            )
        service_rows = self.connection.execute(
            """
            SELECT * FROM service_decisions
            WHERE run_id = ?
            ORDER BY minute, service
            """,
            (run_id,),
        ).fetchall()
        rows_by_minute: dict[int, list[Mapping[str, Any]]] = {}
        for row in service_rows:
            rows_by_minute.setdefault(int(row["minute"]), []).append(row)

        for minute in range(completed):
            expected_rows = rows_by_minute.get(minute, [])
            expected_map = {row["service"]: row for row in expected_rows}
            recomputed = schedule_minute(declaration, minute)
            if set(expected_map) != set(declaration.service_names):
                missing = sorted(set(declaration.service_names) - set(expected_map))
                extra = sorted(set(expected_map) - set(declaration.service_names))
                raise ReplayError(
                    "service decision set differs from the declaration",
                    minute=minute,
                    service=missing[0] if missing else extra[0],
                    field="service",
                    details={
                        "reason": "service_set_mismatch",
                        "missing_services": missing,
                        "extra_services": extra,
                    },
                )
            for item in recomputed.services:
                row = expected_map[item.service]
                expected = {
                    "request_count": row["request_count"],
                    "capacity_per_replica": row["capacity_per_replica"],
                    "unclamped_demand": row["unclamped_demand"],
                    "demand_replicas": row["demand_replicas"],
                    "final_replicas": row["final_replicas"],
                    "accepted_requests": row["accepted_requests"],
                    "rejected_requests": row["rejected_requests"],
                    "clip_reasons": _json_load(row["clip_reasons_json"]),
                }
                self._compare_replayed_service(
                    run_id=run_id,
                    minute=minute,
                    computed=item,
                    expected=expected,
                    basis=basis,
                )

            minute_row = self.connection.execute(
                "SELECT * FROM minute_decisions WHERE run_id = ? AND minute = ?",
                (run_id, minute),
            ).fetchone()
            minute_fields = {
                "budget_limit": recomputed.budget_limit,
                "budget_requested_total": recomputed.budget_requested_total,
                "budget_used": recomputed.budget_used,
                "requests_total": recomputed.requests_total,
                "accepted_total": recomputed.accepted_total,
                "rejected_total": recomputed.rejected_total,
                "estimated_work_ms_total": recomputed.estimated_work_ms_total,
            }
            for field, actual in minute_fields.items():
                if minute_row[field] != actual:
                    raise ReplayError(
                        "replayed minute total differs from the ledger",
                        minute=minute,
                        service="*",
                        field=field,
                        details={
                            "run_id": run_id,
                            "expected": minute_row[field],
                            "actual": actual,
                            **basis,
                        },
                    )
        return {
            "ok": True,
            "code": None,
            "run_id": run_id,
            "declaration_version": run["declaration_version"],
            "minutes_replayed": completed,
            "services_compared": len(declaration.services),
            "fields_compared": [
                "request_count",
                "capacity_per_replica",
                "unclamped_demand",
                "demand_replicas",
                "final_replicas",
                "accepted_requests",
                "rejected_requests",
                "clip_reasons",
            ],
            **basis,
        }

    def _snapshot_payload(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run is None:
            raise StateError("run does not exist", "$.run_id", {"run_id": run_id})
        completed, noncontiguous = self._contiguous_complete_prefix(run_id)
        if noncontiguous or completed != int(run["total_minutes"]):
            raise StateError(
                "a whole-run snapshot requires every minute to be complete",
                f"$.runs[{run_id}]",
                {
                    "completed_minutes": completed,
                    "total_minutes": run["total_minutes"],
                    "noncontiguous_minutes": noncontiguous,
                },
            )
        declaration = self.load_declaration(run["declaration_version"])
        return {
            "format": "model-service-workbench-snapshot-v1",
            "declaration": {
                "version": declaration.declaration_version,
                "authored_version": declaration.authored_version,
                "fingerprint": declaration.fingerprint,
                "source_path": declaration.source_path,
                "raw_text": declaration.raw_text,
            },
            "run": {
                "run_id": run_id,
                "total_minutes": run["total_minutes"],
                "completed_minutes": completed,
            },
            "minutes": self.get_decisions(run_id),
        }

    def save_snapshot(self, run_id: str) -> dict[str, Any]:
        payload = self._snapshot_payload(run_id)
        digest = canonical_sha256(payload)
        with self.connection:
            count = int(
                self.connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            )
            snapshot_id = (
                f"snap-v{payload['declaration']['version']}-{count + 1:04d}"
            )
            file_path = self.snapshot_dir / f"{snapshot_id}.json"
            temp_path = file_path.with_suffix(".json.tmp")
            try:
                temp_path.write_text(
                    canonical_json(payload) + "\n",
                    encoding="utf-8",
                )
                os.replace(temp_path, file_path)
            except OSError as exc:
                raise StateError(
                    "cannot write snapshot file",
                    "$.snapshots",
                    {"file_path": str(file_path), "reason": str(exc)},
                ) from exc
            self.connection.execute(
                """
                INSERT INTO snapshots(
                    snapshot_id, source_run_id, declaration_version,
                    declaration_fingerprint, total_minutes, completed_minutes,
                    payload_sha256, payload_json, file_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    run_id,
                    payload["declaration"]["version"],
                    payload["declaration"]["fingerprint"],
                    payload["run"]["total_minutes"],
                    payload["run"]["completed_minutes"],
                    digest,
                    canonical_json(payload),
                    str(file_path),
                    _now(),
                ),
            )
        return {
            "ok": True,
            "snapshot_id": snapshot_id,
            "run_id": run_id,
            "declaration_version": payload["declaration"]["version"],
            "completed_minutes": payload["run"]["completed_minutes"],
            "payload_sha256": digest,
            "file_path": str(file_path),
        }

    def list_snapshots(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM snapshots ORDER BY snapshot_id"
        ).fetchall()
        return [
            {
                "snapshot_id": row["snapshot_id"],
                "source_run_id": row["source_run_id"],
                "declaration_version": row["declaration_version"],
                "declaration_fingerprint": row["declaration_fingerprint"],
                "total_minutes": row["total_minutes"],
                "completed_minutes": row["completed_minutes"],
                "payload_sha256": row["payload_sha256"],
                "file_path": row["file_path"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _load_snapshot_record(self, snapshot_id: str) -> tuple[dict[str, Any], Mapping[str, Any]]:
        row = self.connection.execute(
            "SELECT * FROM snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise StateError(
                "snapshot does not exist",
                "$.snapshots",
                {"snapshot_id": snapshot_id},
            )
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError as exc:
            raise ReplayError(
                "snapshot payload is not valid JSON",
                details={"reason": "snapshot_json_invalid", "snapshot_id": snapshot_id},
            ) from exc
        digest = canonical_sha256(payload)
        if digest != row["payload_sha256"]:
            raise ReplayError(
                "snapshot payload checksum does not match",
                details={
                    "reason": "snapshot_checksum_mismatch",
                    "snapshot_id": snapshot_id,
                    "expected": row["payload_sha256"],
                    "actual": digest,
                },
            )
        return dict(row), payload

    def _compare_snapshot_minutes(
        self,
        declaration: ClusterDeclaration,
        expected_minutes: Sequence[Mapping[str, Any]],
        snapshot_id: str,
    ) -> int:
        expected_by_minute = {
            int(item["minute"]): item
            for item in expected_minutes
        }
        basis = {
            "snapshot_id": snapshot_id,
            "declaration_version": declaration.declaration_version,
            "declaration_fingerprint": declaration.fingerprint,
        }
        if set(expected_by_minute) != set(range(declaration.total_minutes)):
            missing = sorted(set(range(declaration.total_minutes)) - set(expected_by_minute))
            extra = sorted(set(expected_by_minute) - set(range(declaration.total_minutes)))
            raise ReplayError(
                "snapshot minute set is incomplete",
                minute=missing[0] if missing else extra[0],
                service="*",
                field="minutes",
                details={
                    "reason": "snapshot_minute_set_mismatch",
                    "missing_minutes": missing,
                    "extra_minutes": extra,
                },
            )
        for minute in range(declaration.total_minutes):
            recomputed = schedule_minute(declaration, minute)
            expected = expected_by_minute[minute]
            services = expected.get("services", [])
            expected_map = {str(item["service"]): item for item in services}
            if set(expected_map) != set(declaration.service_names):
                missing = sorted(set(declaration.service_names) - set(expected_map))
                extra = sorted(set(expected_map) - set(declaration.service_names))
                raise ReplayError(
                    "snapshot service set differs from the declaration",
                    minute=minute,
                    service=missing[0] if missing else extra[0],
                    field="service",
                    details={
                        "reason": "snapshot_service_set_mismatch",
                        "missing_services": missing,
                        "extra_services": extra,
                        **basis,
                    },
                )
            for item in recomputed.services:
                self._compare_replayed_service(
                    run_id=f"snapshot:{snapshot_id}",
                    minute=minute,
                    computed=item,
                    expected=expected_map[item.service],
                    basis=basis,
                )
            minute_fields = {
                "budget_limit": recomputed.budget_limit,
                "budget_requested_total": recomputed.budget_requested_total,
                "budget_used": recomputed.budget_used,
                "requests_total": recomputed.requests_total,
                "accepted_total": recomputed.accepted_total,
                "rejected_total": recomputed.rejected_total,
                "estimated_work_ms_total": recomputed.estimated_work_ms_total,
            }
            for field, actual in minute_fields.items():
                if expected.get(field) != actual:
                    raise ReplayError(
                        "replayed snapshot minute total differs",
                        minute=minute,
                        service="*",
                        field=field,
                        details={
                            "expected": expected.get(field),
                            "actual": actual,
                            **basis,
                        },
                    )
        return declaration.total_minutes

    def restore_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        record, payload = self._load_snapshot_record(snapshot_id)
        latest_row = self.connection.execute(
            "SELECT MAX(version) AS version FROM declarations"
        ).fetchone()
        latest_version = latest_row["version"] if latest_row else None
        if latest_version is not None and int(record["declaration_version"]) != int(latest_version):
            raise StateError(
                "a snapshot from a stale declaration version cannot become current",
                "$.snapshots",
                {
                    "snapshot_id": snapshot_id,
                    "snapshot_version": record["declaration_version"],
                    "latest_declaration_version": latest_version,
                },
            )
        declaration_block = payload.get("declaration", {})
        try:
            declaration = load_declaration_text(
                declaration_block["raw_text"],
                declaration_block.get("source_path", f"snapshot:{snapshot_id}"),
            ).with_version(int(declaration_block["version"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ReplayError(
                "snapshot declaration block is incomplete",
                field="declaration",
                details={"reason": "snapshot_declaration_invalid", "snapshot_id": snapshot_id},
            ) from exc
        if (
            declaration.fingerprint != declaration_block.get("fingerprint")
            or declaration.declaration_version != record["declaration_version"]
            or declaration.fingerprint != record["declaration_fingerprint"]
        ):
            raise ReplayError(
                "snapshot declaration does not match its recorded immutable identity",
                field="declaration_fingerprint",
                details={
                    "reason": "snapshot_declaration_identity_mismatch",
                    "snapshot_id": snapshot_id,
                    "recorded_fingerprint": record["declaration_fingerprint"],
                    "payload_fingerprint": declaration.fingerprint,
                },
            )

        existing = self.connection.execute(
            "SELECT * FROM declarations WHERE version = ?",
            (declaration.declaration_version,),
        ).fetchone()
        if existing is not None and existing["fingerprint"] != declaration.fingerprint:
            raise ReplayError(
                "snapshot version is bound to a different declaration",
                field="declaration_version",
                details={
                    "reason": "declaration_version_content_conflict",
                    "declaration_version": declaration.declaration_version,
                },
            )

        minutes = payload.get("minutes", [])
        self._compare_snapshot_minutes(declaration, minutes, snapshot_id)
        run_id = self.start_run(
            declaration,
            source_kind="snapshot",
            source_id=snapshot_id,
        )
        for minute in range(declaration.total_minutes):
            decision = schedule_minute(declaration, minute)
            self.begin_minute(run_id, minute)
            self.commit_minute(run_id, decision)
        replay = self.replay_run(run_id)
        return {
            "ok": True,
            "code": None,
            "snapshot_id": snapshot_id,
            "restored_run_id": run_id,
            "declaration_version": declaration.declaration_version,
            "restored_minutes": declaration.total_minutes,
            "replay": replay,
        }
