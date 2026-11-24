"""SQLite plus JSON checkpoint pairs with atomic replacement and replay checks."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .constants import CHECKPOINT_SCHEMA_VERSION, RULE_SEMANTICS_VERSION
from .io_utils import (
    atomic_write_json,
    digest_value,
    read_json,
    sha256_hex,
    stable_json_bytes,
)


@dataclass
class CheckpointLoad:
    valid: bool
    reason: str
    completed: dict[str, dict] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    residues: list[str] = field(default_factory=list)
    identity_matches: bool = False
    snapshot_matches: bool = False


def _result_hashes(completed: dict[str, dict]) -> dict[str, str]:
    return {
        connection_id: sha256_hex(stable_json_bytes(result))
        for connection_id, result in completed.items()
    }


def _build_meta(
    *,
    identity: dict[str, Any],
    completed: dict[str, dict],
    sequence: int,
) -> dict[str, Any]:
    ids = sorted(completed)
    hashes = _result_hashes(completed)
    return {
        "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
        "rule_semantics_version": RULE_SEMANTICS_VERSION,
        **identity,
        "sequence": sequence,
        "completed_count": len(ids),
        "completed_ids_sha256": digest_value(ids),
        "results_sha256": digest_value(
            [[connection_id, hashes[connection_id]] for connection_id in ids]
        ),
    }


def _identity_from_meta(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "checkpoint_schema_version": meta.get("checkpoint_schema_version"),
        "rule_semantics_version": meta.get("rule_semantics_version"),
        "input_digest": meta.get("input_digest"),
        "plan_digest": meta.get("plan_digest"),
        "catalog_fingerprint": meta.get("catalog_fingerprint"),
    }


class CheckpointManager:
    def __init__(
        self,
        workspace: Path,
        *,
        input_digest: str,
        plan_digest: str,
        catalog_fingerprint: str,
    ) -> None:
        self.workspace = Path(workspace)
        self.db_path = self.workspace / "checkpoint.sqlite3"
        self.snapshot_path = self.workspace / "checkpoint_snapshot.json"
        self.identity = {
            "input_digest": input_digest,
            "plan_digest": plan_digest,
            "catalog_fingerprint": catalog_fingerprint,
        }
        self.completed: dict[str, dict] = {}
        self.sequence = 0

    def _residues(self) -> list[str]:
        found: set[str] = set()
        for path in self.workspace.glob(".*.tmp-*"):
            if path.is_file():
                found.add(path.name)
        return sorted(found)

    def _read_db(self) -> tuple[dict[str, Any] | None, dict[str, dict]]:
        if not self.db_path.is_file():
            return None, {}
        connection = sqlite3.connect(
            f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True
        )
        try:
            meta_row = connection.execute(
                "SELECT value FROM checkpoint_meta WHERE key = 'meta'"
            ).fetchone()
            if meta_row is None:
                return None, {}
            meta = json.loads(meta_row[0])
            rows = list(
                connection.execute(
                    "SELECT connection_id, result_json FROM completed "
                    "ORDER BY connection_id"
                )
            )
            completed = {
                connection_id: json.loads(result_json)
                for connection_id, result_json in rows
            }
            return meta, completed
        finally:
            connection.close()

    def _read_snapshot(
        self,
    ) -> tuple[dict[str, Any] | None, dict[str, dict], dict[str, Any] | None]:
        if not self.snapshot_path.is_file():
            return None, {}, None
        doc = read_json(self.snapshot_path)
        if not isinstance(doc, dict):
            return None, {}, None
        results = doc.get("results")
        if not isinstance(results, dict):
            return None, {}, None
        aggregate = doc.get("aggregate")
        if not isinstance(aggregate, dict):
            aggregate = None
        return doc.get("meta"), results, aggregate

    def load(self, *, require_identity: bool = True) -> CheckpointLoad:
        residues = self._residues()
        if not self.db_path.exists() and not self.snapshot_path.exists():
            return CheckpointLoad(
                valid=False,
                reason="no_checkpoint",
                residues=residues,
                identity_matches=True,
            )
        if not self.db_path.exists() or not self.snapshot_path.exists():
            return CheckpointLoad(
                valid=False,
                reason="half_checkpoint_missing_sidecar",
                residues=residues,
            )

        try:
            db_meta, db_completed = self._read_db()
        except (OSError, sqlite3.DatabaseError, ValueError, json.JSONDecodeError):
            return CheckpointLoad(
                valid=False,
                reason="corrupt_checkpoint_database",
                residues=residues,
            )
        try:
            snapshot_meta, snapshot_results, snapshot_aggregate = self._read_snapshot()
        except (OSError, ValueError, json.JSONDecodeError):
            return CheckpointLoad(
                valid=False,
                reason="corrupt_checkpoint_snapshot",
                residues=residues,
            )
        if db_meta is None or snapshot_meta is None:
            return CheckpointLoad(
                valid=False,
                reason="missing_checkpoint_metadata",
                residues=residues,
            )

        loaded_identity = _identity_from_meta(db_meta)
        version_matches = (
            loaded_identity["checkpoint_schema_version"]
            == CHECKPOINT_SCHEMA_VERSION
            and loaded_identity["rule_semantics_version"]
            == RULE_SEMANTICS_VERSION
        )
        if not version_matches:
            return CheckpointLoad(
                valid=False,
                reason="checkpoint_version_or_rule_identity_changed",
                residues=residues,
                identity_matches=False,
            )
        expected_identity = {
            "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
            "rule_semantics_version": RULE_SEMANTICS_VERSION,
            **self.identity,
        }
        identity_matches = loaded_identity == expected_identity
        if require_identity and not identity_matches:
            return CheckpointLoad(
                valid=False,
                reason="checkpoint_input_plan_or_rules_changed",
                residues=residues,
                identity_matches=False,
            )

        if snapshot_meta != db_meta:
            return CheckpointLoad(
                valid=False,
                reason="database_and_snapshot_metadata_disagree",
                residues=residues,
                identity_matches=True,
            )
        if snapshot_results != db_completed:
            return CheckpointLoad(
                valid=False,
                reason="database_and_snapshot_results_disagree",
                residues=residues,
                identity_matches=True,
            )
        from .scheduler import summarize_results

        if snapshot_aggregate != summarize_results(db_completed.values()):
            return CheckpointLoad(
                valid=False,
                reason="database_and_snapshot_aggregate_disagree",
                residues=residues,
                identity_matches=True,
            )

        context_identity = {
            key: loaded_identity[key]
            for key in (
                "input_digest",
                "plan_digest",
                "catalog_fingerprint",
            )
        }
        recomputed = _build_meta(
            identity=context_identity,
            completed=db_completed,
            sequence=db_meta.get("sequence", 0),
        )
        if recomputed != db_meta:
            return CheckpointLoad(
                valid=False,
                reason="checkpoint_replay_digest_mismatch",
                residues=residues,
                identity_matches=identity_matches,
                snapshot_matches=True,
            )

        self.completed = dict(db_completed)
        self.sequence = int(db_meta.get("sequence", 0))
        if identity_matches:
            self.identity = context_identity
        return CheckpointLoad(
            valid=True,
            reason="ok",
            completed=dict(db_completed),
            meta=db_meta,
            residues=residues,
            identity_matches=identity_matches,
            snapshot_matches=True,
        )

    def reset(self) -> CheckpointLoad:
        self.completed = {}
        self.sequence = 0
        self._write_pair()
        return CheckpointLoad(
            valid=True,
            reason="rebuilt",
            completed={},
            meta=_build_meta(
                identity=self.identity,
                completed=self.completed,
                sequence=self.sequence,
            ),
            residues=self._residues(),
            identity_matches=True,
            snapshot_matches=True,
        )

    def _write_db(self, completed: dict[str, dict], meta: dict[str, Any]) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".checkpoint.sqlite3.tmp-",
            dir=str(self.workspace),
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            connection = sqlite3.connect(str(tmp_path))
            try:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute(
                    "CREATE TABLE checkpoint_meta ("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE completed ("
                    "connection_id TEXT PRIMARY KEY, "
                    "result_json TEXT NOT NULL, "
                    "result_sha256 TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO checkpoint_meta(key, value) VALUES (?, ?)",
                    ("meta", json.dumps(meta, sort_keys=True, separators=(",", ":"))),
                )
                for connection_id in sorted(completed):
                    result = completed[connection_id]
                    result_json = json.dumps(
                        result,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    connection.execute(
                        "INSERT INTO completed("
                        "connection_id, result_json, result_sha256) "
                        "VALUES (?, ?, ?)",
                        (
                            connection_id,
                            result_json,
                            sha256_hex(result_json.encode("utf-8")),
                        ),
                    )
                connection.commit()
            finally:
                connection.close()
            with tmp_path.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.db_path)
        except BaseException:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise

    def _write_pair(self) -> dict[str, Any]:
        from .scheduler import summarize_results

        meta = _build_meta(
            identity=self.identity,
            completed=self.completed,
            sequence=self.sequence,
        )
        self._write_db(self.completed, meta)
        atomic_write_json(
            self.snapshot_path,
            {
                "meta": meta,
                "completed_ids": sorted(self.completed),
                "results": self.completed,
                "aggregate": summarize_results(self.completed.values()),
            },
        )
        return meta

    def commit(self, result: dict) -> dict[str, Any]:
        connection_id = result["connection_id"]
        self.completed[connection_id] = result
        self.sequence += 1
        return self._write_pair()

    def verify(self) -> dict[str, Any]:
        load = self.load(require_identity=False)
        return {
            "valid": load.valid,
            "reason": load.reason,
            "residues": load.residues,
            "identity_matches": load.identity_matches,
            "snapshot_matches": load.snapshot_matches,
            "completed_count": len(load.completed),
            "database": str(self.db_path),
            "snapshot": str(self.snapshot_path),
            "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
            "rule_semantics_version": RULE_SEMANTICS_VERSION,
        }

