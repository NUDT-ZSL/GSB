"""Versioned, checksummed checkpoints with atomic snapshot replacement."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .codes import E_CHECKPOINT_CORRUPT
from .util import (
    BusinessError,
    WorkdirLock,
    atomic_write_json,
    canonical_json,
    read_json,
    sha256_bytes,
)

CHECKPOINT_SCHEMA_VERSION = 1
RULE_BASIS_VERSION = 1


@dataclass
class CheckpointState:
    schema_version: int
    rules_basis: str
    completed_connections: list[str] = field(default_factory=list)
    connection_results: dict[str, Any] = field(default_factory=dict)
    mutation_results: dict[str, Any] = field(default_factory=dict)
    aggregate: dict[str, Any] = field(default_factory=dict)
    generation: int = 0
    complete: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "rules_basis": self.rules_basis,
            "completed_connections": sorted(self.completed_connections),
            "connection_results": self.connection_results,
            "mutation_results": self.mutation_results,
            "aggregate": self.aggregate,
            "generation": self.generation,
            "complete": self.complete,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CheckpointState":
        return cls(
            schema_version=int(payload["schema_version"]),
            rules_basis=str(payload["rules_basis"]),
            completed_connections=list(payload.get("completed_connections", [])),
            connection_results=dict(payload.get("connection_results", {})),
            mutation_results=dict(payload.get("mutation_results", {})),
            aggregate=dict(payload.get("aggregate", {})),
            generation=int(payload.get("generation", 0)),
            complete=bool(payload.get("complete", False)),
        )


@dataclass
class CheckpointLoad:
    state: CheckpointState | None
    rebuilt: bool
    reason: str
    residue: list[str] = field(default_factory=list)
    generation: int = 0


class CheckpointStore:
    def __init__(self, workdir: str | os.PathLike[str]):
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.database_path = self.workdir / "checkpoints.sqlite"
        self.pointer_path = self.workdir / "checkpoint.json"
        self.snapshot_dir = self.workdir / "snapshots"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=0.25)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoint_rows (
                generation INTEGER PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                rules_basis TEXT NOT NULL,
                complete INTEGER NOT NULL,
                checksum TEXT NOT NULL,
                snapshot_name TEXT NOT NULL,
                state_json TEXT NOT NULL,
                created_ns INTEGER NOT NULL
            )
            """
        )
        return connection

    def residue(self) -> list[str]:
        found: list[str] = []
        for path in sorted(self.workdir.rglob(".*.tmp-*")):
            found.append(str(path))
        referenced: set[str] = set()
        rows: list[tuple[int, str]] = []
        if self.database_path.is_file():
            try:
                connection = sqlite3.connect(
                    self.database_path.as_uri() + "?mode=ro",
                    uri=True,
                )
                rows = [
                    (int(row[0]), str(row[1]))
                    for row in connection.execute(
                        "SELECT generation, snapshot_name FROM checkpoint_rows"
                    ).fetchall()
                ]
                connection.close()
            except sqlite3.Error:
                rows = []
        for generation, snapshot_name in rows:
            referenced.add(str(snapshot_name))
            if not (self.workdir / snapshot_name).is_file():
                found.append(f"db-without-snapshot:{generation}")
        for path in sorted(self.snapshot_dir.glob("checkpoint-*.json")):
            relative = str(path.relative_to(self.workdir))
            if relative not in referenced:
                found.append(f"orphan-snapshot:{relative}")
        return found

    def save(
        self,
        state: CheckpointState,
        *,
        lock_held: bool = False,
    ) -> int:
        if state.schema_version != CHECKPOINT_SCHEMA_VERSION:
            raise BusinessError(
                E_CHECKPOINT_CORRUPT,
                "refusing to save an unsupported checkpoint schema",
                {"schema_version": state.schema_version},
            )
        context = (
            _NoopLock()
            if lock_held
            else WorkdirLock(self.workdir)
        )
        with context:
            connection = self._connect()
            try:
                current = connection.execute(
                    "SELECT COALESCE(MAX(generation), 0) FROM checkpoint_rows"
                ).fetchone()
                generation = int(current[0]) + 1
                state.generation = generation
                state.schema_version = CHECKPOINT_SCHEMA_VERSION
                payload = state.to_dict()
                state_json = canonical_json(payload)
                checksum = sha256_bytes(state_json.encode("utf-8"))
                snapshot_name = f"snapshots/checkpoint-{generation:08d}.json"
                snapshot_path = self.workdir / snapshot_name
                snapshot_payload = {
                    "generation": generation,
                    "checksum": checksum,
                    "state": payload,
                }
                atomic_write_json(snapshot_path, snapshot_payload, pretty=False)
                connection.execute(
                    """
                    INSERT INTO checkpoint_rows (
                        generation, schema_version, rules_basis, complete,
                        checksum, snapshot_name, state_json, created_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        generation,
                        state.schema_version,
                        state.rules_basis,
                        1 if state.complete else 0,
                        checksum,
                        snapshot_name,
                        state_json,
                        time.time_ns(),
                    ),
                )
                connection.commit()
                atomic_write_json(
                    self.pointer_path,
                    {
                        "generation": generation,
                        "checksum": checksum,
                        "schema_version": state.schema_version,
                        "rules_basis": state.rules_basis,
                        "snapshot": snapshot_name,
                    },
                    pretty=False,
                )
                return generation
            finally:
                connection.close()

    def load(
        self,
        *,
        expected_rules_basis: str,
        lock_held: bool = False,
    ) -> CheckpointLoad:
        context = (
            _NoopLock()
            if lock_held
            else WorkdirLock(self.workdir)
        )
        with context:
            residue = self.residue()
            if not self.database_path.is_file():
                return CheckpointLoad(
                    state=None,
                    rebuilt=True,
                    reason="NO_CHECKPOINT",
                    residue=residue,
                )
            try:
                connection = sqlite3.connect(
                    self.database_path.as_uri() + "?mode=ro",
                    uri=True,
                )
                rows = connection.execute(
                    """
                    SELECT generation, schema_version, rules_basis, complete,
                           checksum, snapshot_name, state_json
                    FROM checkpoint_rows
                    ORDER BY generation DESC
                    """
                ).fetchall()
                connection.close()
            except sqlite3.Error as exc:
                return CheckpointLoad(
                    state=None,
                    rebuilt=True,
                    reason=f"CHECKPOINT_CORRUPT: {exc}",
                    residue=residue,
                )

            if not rows:
                return CheckpointLoad(
                    state=None,
                    rebuilt=True,
                    reason="EMPTY_CHECKPOINT_DATABASE",
                    residue=residue,
                )

            incompatible_reason = ""
            for row in rows:
                (
                    generation,
                    schema_version,
                    rules_basis,
                    complete,
                    checksum,
                    snapshot_name,
                    state_json,
                ) = row
                if int(schema_version) != CHECKPOINT_SCHEMA_VERSION:
                    incompatible_reason = (
                        f"SCHEMA_VERSION_CHANGED:{schema_version}"
                    )
                    continue
                if str(rules_basis) != expected_rules_basis:
                    incompatible_reason = "RULE_GENERATION_BASIS_CHANGED"
                    continue
                if not bool(complete):
                    incompatible_reason = "LAST_CHECKPOINT_INCOMPLETE"
                    continue
                if sha256_bytes(str(state_json).encode("utf-8")) != checksum:
                    residue.append(f"db-checksum-mismatch:{generation}")
                    continue
                snapshot_path = self.workdir / str(snapshot_name)
                if not snapshot_path.is_file():
                    residue.append(f"missing-snapshot:{generation}")
                    continue
                try:
                    snapshot = read_json(snapshot_path)
                    if int(snapshot.get("generation", -1)) != int(generation):
                        residue.append(f"snapshot-generation-mismatch:{generation}")
                        continue
                    if snapshot.get("checksum") != checksum:
                        residue.append(f"snapshot-checksum-mismatch:{generation}")
                        continue
                    snapshot_state = snapshot.get("state")
                    if canonical_json(snapshot_state) != str(state_json):
                        residue.append(f"snapshot-state-mismatch:{generation}")
                        continue
                    state = CheckpointState.from_dict(snapshot_state)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    residue.append(f"unreadable-snapshot:{generation}:{exc}")
                    continue
                if state.schema_version != CHECKPOINT_SCHEMA_VERSION:
                    incompatible_reason = "SCHEMA_VERSION_CHANGED"
                    continue
                if state.rules_basis != expected_rules_basis:
                    incompatible_reason = "RULE_GENERATION_BASIS_CHANGED"
                    continue
                if not state.complete:
                    incompatible_reason = "LAST_CHECKPOINT_INCOMPLETE"
                    continue
                return CheckpointLoad(
                    state=state,
                    rebuilt=False,
                    reason="RESTORED",
                    residue=residue,
                    generation=int(generation),
                )
            return CheckpointLoad(
                state=None,
                rebuilt=True,
                reason=incompatible_reason or "NO_COMPLETE_CHECKPOINT",
                residue=residue,
            )


class _NoopLock:
    def __enter__(self) -> "_NoopLock":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def rules_basis_fingerprint(
    rules: list[Any],
    *,
    observed_clock_ms: int,
    algorithm_version: int = RULE_BASIS_VERSION,
) -> str:
    records = []
    for rule in sorted(
        rules,
        key=lambda item: (
            item.canonical_name,
            item.generation,
            item.effective_from_ms,
            item.source_kind,
            item.source_file,
            item.record_index,
        ),
    ):
        records.append(
            {
                "name": rule.canonical_name,
                "generation": rule.generation,
                "effective_from_ms": rule.effective_from_ms,
                "effective_to_ms": rule.effective_to_ms,
                "enabled": rule.enabled,
                "tombstone": rule.tombstone,
                "transitions": [item.to_dict() for item in rule.transitions],
                "depends_on": sorted(rule.depends_on),
            }
        )
    return sha256_bytes(
        canonical_json(
            {
                "algorithm_version": algorithm_version,
                "observed_clock_ms": observed_clock_ms,
                "rules": records,
            }
        ).encode("utf-8")
    )
