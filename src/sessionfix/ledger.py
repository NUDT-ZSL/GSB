from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .adjudication import (
    AdjudicationResult,
    RevocationRequest,
    adjudicate,
)
from .errors import SessionFixError
from .models import NormalizedSample, layout_version
from .util import (
    atomic_write_json,
    canonical_json,
    digest_bytes,
    digest_json,
    read_json,
    scan_temp_residue,
)


SCHEMA_VERSION = "1"


def _checkpoint_payload_meta(meta: dict[str, str]) -> dict[str, str]:
    excluded = {
        "checkpoint_seq",
        "batch_index",
        "last_checkpoint_id",
        "recovery_status",
        "restored_from_checkpoint",
    }
    return {
        key: value
        for key, value in sorted(meta.items())
        if key not in excluded
    }


def _decision_tables_digest(payload: dict[str, Any]) -> str:
    tables = {
        "sessions": [
            {
                "session_id": item["session_id"],
                "generation": item["generation"],
                "user_id": item["user_id"],
                "policy_key": item["policy_key"],
                "repair_state": item["repair_state"],
                "policy_generation": item["policy_generation"],
                "binding_check": item["binding_check"],
                "decision_watermark": item["decision_watermark"],
            }
            for item in payload.get("sessions", [])
        ],
        "successor_edges": list(payload.get("successor_edges", [])),
        "findings": [
            {
                "code": item["code"],
                "session_id": item["session_id"],
                "generation": item["generation"],
                "policy_generation": item["policy_generation"],
                "decision_watermark": item["decision_watermark"],
                "detail": item["detail"],
            }
            for item in payload.get("findings", [])
        ],
        "rejections": list(payload.get("rejections", [])),
        "policy_conflicts": list(payload.get("policy_conflicts", [])),
        "revocations": [
            {
                "target_kind": item["target_kind"],
                "target_value": item["target_value"],
                "resolved_session_id": item["resolved_session_id"],
                "closure": item["closure"],
                "cycle_session_ids": item["cycle_session_ids"],
                "codes": item["codes"],
            }
            for item in payload.get("revocations", [])
        ],
    }
    return digest_json(
        {
            key: sorted(value, key=lambda item: canonical_json(item))
            for key, value in tables.items()
        }
    )


def _expected_decision_digest(
    sample: NormalizedSample,
    processed_count: int,
    revocations: list[RevocationRequest],
) -> str:
    result = adjudicate(sample, processed_count, revocations)
    payload = {
        "sessions": [
            {
                "session_id": item.session_id,
                "generation": item.generation,
                "user_id": item.user_id,
                "policy_key": item.policy_key,
                "repair_state": item.repair_state,
                "policy_generation": item.policy_generation,
                "binding_check": item.binding_check,
                "decision_watermark": item.decision_watermark,
            }
            for item in result.sessions
        ],
        "successor_edges": [item.to_dict() for item in result.edges],
        "findings": [
            {
                "code": item.code,
                "session_id": item.session_id,
                "generation": item.generation,
                "policy_generation": item.policy_generation,
                "decision_watermark": result.decision_watermark,
                "detail": item.detail,
            }
            for item in result.findings
        ],
        "rejections": [item.to_dict() for item in sample.rejections],
        "policy_conflicts": [
            {
                "policy_key": item.policy_key,
                "field": item.field,
                "detail": item.to_dict(),
            }
            for item in sample.policy_conflicts
        ],
        "revocations": [
            {
                "target_kind": item.request.target_kind,
                "target_value": item.request.target_value,
                "resolved_session_id": item.resolved_session_id,
                "closure": list(item.closure),
                "cycle_session_ids": list(item.cycle_session_ids),
                "codes": list(item.codes),
            }
            for item in result.revocations
        ],
    }
    return _decision_tables_digest(payload)


class Ledger:
    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir.resolve()
        self.state_dir = self.workdir / "state"
        self.checkpoint_dir = self.state_dir / "checkpoints"
        self.db_path = self.state_dir / "ledger.sqlite3"
        self.normalized_path = self.state_dir / "normalized.json"
        self.report_path = self.state_dir / "report.json"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = DELETE")
        self.connection.execute("PRAGMA synchronous = FULL")
        self._ensure_schema()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _ensure_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_ledger (
                session_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                policy_key TEXT NOT NULL,
                repair_state TEXT NOT NULL,
                policy_generation INTEGER NOT NULL,
                binding_check TEXT NOT NULL,
                decision_watermark INTEGER NOT NULL,
                PRIMARY KEY (session_id, generation)
            );
            CREATE TABLE IF NOT EXISTS successor_edges (
                parent_session_id TEXT NOT NULL,
                parent_generation INTEGER NOT NULL,
                child_session_id TEXT NOT NULL,
                child_generation INTEGER NOT NULL,
                child_issued_at_ms INTEGER NOT NULL,
                PRIMARY KEY (
                    parent_session_id, parent_generation,
                    child_session_id, child_generation
                )
            );
            CREATE TABLE IF NOT EXISTS findings (
                finding_id TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                session_id TEXT,
                generation INTEGER,
                policy_generation INTEGER NOT NULL,
                decision_watermark INTEGER NOT NULL,
                detail_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rejections (
                rejection_id TEXT PRIMARY KEY,
                source_category TEXT NOT NULL,
                record_index INTEGER NOT NULL,
                code TEXT NOT NULL,
                message TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                record_digest TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS policy_conflicts (
                conflict_id TEXT PRIMARY KEY,
                policy_key TEXT NOT NULL,
                field TEXT NOT NULL,
                detail_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS revocations (
                revocation_id TEXT PRIMARY KEY,
                target_kind TEXT NOT NULL,
                target_value TEXT NOT NULL,
                resolved_session_id TEXT,
                closure_json TEXT NOT NULL,
                cycle_json TEXT NOT NULL,
                codes_json TEXT NOT NULL,
                outcome_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                checkpoint_id INTEGER PRIMARY KEY,
                sample_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                batch_index INTEGER NOT NULL,
                processed_count INTEGER NOT NULL,
                decision_watermark INTEGER NOT NULL,
                ledger_digest TEXT NOT NULL,
                snapshot_path TEXT NOT NULL,
                snapshot_digest TEXT NOT NULL,
                complete INTEGER NOT NULL DEFAULT 1
            );
            """
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def _meta(self) -> dict[str, str]:
        return {
            str(row["key"]): str(row["value"])
            for row in self.connection.execute("SELECT key, value FROM meta")
        }

    def _set_meta(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def _clear_ledger(self, include_checkpoints: bool = True) -> None:
        for table in (
            "session_ledger",
            "successor_edges",
            "findings",
            "rejections",
            "policy_conflicts",
            "revocations",
        ):
            self.connection.execute(f"DELETE FROM {table}")
        if include_checkpoints:
            self.connection.execute("DELETE FROM checkpoints")
            for path in sorted(self.checkpoint_dir.glob("checkpoint-*.json")):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        self.connection.execute("DELETE FROM meta")

    def initialize(
        self,
        sample: NormalizedSample,
        result: AdjudicationResult,
        *,
        force: bool,
    ) -> dict[str, Any]:
        new_layout = layout_version(sample.policy_views.values())
        meta = self._meta()
        reset = (
            force
            or meta.get("sample_id") != sample.sample_id
            or meta.get("sample_digest") != sample.sample_digest
            or meta.get("layout_version") != new_layout
        )
        if reset:
            with self.transaction():
                self._clear_ledger(include_checkpoints=True)
                self._set_meta("schema_version", SCHEMA_VERSION)
                self._set_meta("sample_id", sample.sample_id)
                self._set_meta("sample_digest", sample.sample_digest)
                self._set_meta("layout_version", new_layout)
                self._set_meta("checkpoint_seq", "0")
                self._set_meta("last_checkpoint_id", "")
                self._set_meta("recovery_status", "fresh")
            atomic_write_json(self.normalized_path, sample.to_dict())
        elif not self.normalized_path.exists():
            atomic_write_json(self.normalized_path, sample.to_dict())
        self.apply_result(sample, result, batch_index=0)
        checkpoint = self.write_checkpoint(
            sample_id=sample.sample_id,
            kind="import",
            batch_index=0,
            processed_count=result.processed_count,
            decision_watermark=result.decision_watermark,
        )
        return {
            "rebuilt": reset,
            "layout_version": new_layout,
            "checkpoint": checkpoint,
        }

    def prepare_for_sample(self, sample: NormalizedSample) -> dict[str, Any]:
        new_layout = layout_version(sample.policy_views.values())
        meta = self._meta()
        reset = (
            not meta
            or meta.get("sample_id") != sample.sample_id
            or meta.get("sample_digest") != sample.sample_digest
            or meta.get("layout_version") != new_layout
        )
        if reset:
            with self.transaction():
                self._clear_ledger(include_checkpoints=True)
                self._set_meta("schema_version", SCHEMA_VERSION)
                self._set_meta("sample_id", sample.sample_id)
                self._set_meta("sample_digest", sample.sample_digest)
                self._set_meta("layout_version", new_layout)
                self._set_meta("processed_count", "0")
                self._set_meta("batch_index", "0")
                self._set_meta("decision_watermark", "0")
                self._set_meta("checkpoint_seq", "0")
                self._set_meta("last_checkpoint_id", "")
                self._set_meta("recovery_status", "fresh")
            atomic_write_json(self.normalized_path, sample.to_dict())
        elif not self.normalized_path.exists():
            atomic_write_json(self.normalized_path, sample.to_dict())
        return {
            "rebuilt": reset,
            "layout_version": new_layout,
            "processed_count": int(self._meta().get("processed_count", "0")),
            "batch_index": int(self._meta().get("batch_index", "0")),
        }

    def load_normalized(self) -> NormalizedSample:
        if not self.normalized_path.exists():
            raise SessionFixError(
                "E_STATE_MISSING",
                "normalized sample is not present; run import first",
                str(self.normalized_path),
            )
        try:
            value = read_json(self.normalized_path)
        except (OSError, ValueError, TypeError) as exc:
            raise SessionFixError(
                "E_STATE_INVALID",
                f"cannot read normalized sample: {exc}",
                str(self.normalized_path),
            ) from exc
        return NormalizedSample.from_dict(value)

    def apply_result(
        self,
        sample: NormalizedSample,
        result: AdjudicationResult,
        *,
        batch_index: int,
    ) -> str:
        with self.transaction() as connection:
            for table in (
                "session_ledger",
                "successor_edges",
                "findings",
                "rejections",
                "policy_conflicts",
                "revocations",
            ):
                connection.execute(f"DELETE FROM {table}")
            for decision in result.sessions:
                connection.execute(
                    """
                    INSERT INTO session_ledger(
                        session_id, generation, user_id, policy_key,
                        repair_state, policy_generation, binding_check,
                        decision_watermark
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision.session_id,
                        decision.generation,
                        decision.user_id,
                        decision.policy_key,
                        decision.repair_state,
                        decision.policy_generation,
                        decision.binding_check,
                        decision.decision_watermark,
                    ),
                )
            for edge in result.edges:
                connection.execute(
                    """
                    INSERT INTO successor_edges(
                        parent_session_id, parent_generation,
                        child_session_id, child_generation, child_issued_at_ms
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        edge.parent_session_id,
                        edge.parent_generation,
                        edge.child_session_id,
                        edge.child_generation,
                        edge.child_issued_at_ms,
                    ),
                )
            for finding in result.findings:
                detail_json = canonical_json(finding.detail)
                finding_id = digest_json(
                    {
                        "code": finding.code,
                        "session_id": finding.session_id,
                        "generation": finding.generation,
                        "policy_generation": finding.policy_generation,
                        "detail": finding.detail,
                    }
                )
                connection.execute(
                    """
                    INSERT INTO findings(
                        finding_id, code, session_id, generation,
                        policy_generation, decision_watermark, detail_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        finding_id,
                        finding.code,
                        finding.session_id,
                        finding.generation,
                        finding.policy_generation,
                        result.decision_watermark,
                        detail_json,
                    ),
                )
            for rejection in sample.rejections:
                rejection_id = digest_json(rejection.to_dict())
                connection.execute(
                    """
                    INSERT INTO rejections(
                        rejection_id, source_category, record_index, code,
                        message, source_ref, record_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rejection_id,
                        rejection.source_category,
                        rejection.record_index,
                        rejection.code,
                        rejection.message,
                        rejection.source_ref,
                        rejection.record_digest,
                    ),
                )
            for conflict in sample.policy_conflicts:
                conflict_id = digest_json(conflict.to_dict())
                connection.execute(
                    """
                    INSERT INTO policy_conflicts(
                        conflict_id, policy_key, field, detail_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        conflict_id,
                        conflict.policy_key,
                        conflict.field,
                        canonical_json(conflict.to_dict()),
                    ),
                )
            for outcome in result.revocations:
                revocation_id = digest_json(outcome.request.to_dict())
                connection.execute(
                    """
                    INSERT INTO revocations(
                        revocation_id, target_kind, target_value,
                        resolved_session_id, closure_json, cycle_json,
                        codes_json, outcome_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revocation_id,
                        outcome.request.target_kind,
                        outcome.request.target_value,
                        outcome.resolved_session_id,
                        canonical_json(outcome.closure),
                        canonical_json(outcome.cycle_session_ids),
                        canonical_json(outcome.codes),
                        canonical_json(outcome.to_dict()),
                    ),
                )
            self._set_meta("processed_count", str(result.processed_count))
            self._set_meta("batch_index", str(batch_index))
            self._set_meta("decision_watermark", str(result.decision_watermark))
            self._set_meta("result_digest", digest_json(result.to_dict()))
        return self.ledger_digest()

    def ledger_payload(self) -> dict[str, Any]:
        meta = _checkpoint_payload_meta(self._meta())
        sessions = [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT session_id, generation, user_id, policy_key,
                       repair_state, policy_generation, binding_check,
                       decision_watermark
                FROM session_ledger
                ORDER BY session_id, generation
                """
            )
        ]
        edges = [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT parent_session_id, parent_generation,
                       child_session_id, child_generation, child_issued_at_ms
                FROM successor_edges
                ORDER BY parent_session_id, parent_generation,
                         child_session_id, child_generation
                """
            )
        ]
        findings = []
        for row in self.connection.execute(
            """
            SELECT code, session_id, generation, policy_generation,
                   decision_watermark, detail_json
            FROM findings
            ORDER BY code, session_id, generation, finding_id
            """
        ):
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            findings.append(item)
        rejections = [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT source_category, record_index, code, message,
                       source_ref, record_digest
                FROM rejections
                ORDER BY source_category, record_index, code, rejection_id
                """
            )
        ]
        conflicts = []
        for row in self.connection.execute(
            """
            SELECT policy_key, field, detail_json
            FROM policy_conflicts
            ORDER BY policy_key, field, conflict_id
            """
        ):
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            conflicts.append(item)
        revocations = []
        for row in self.connection.execute(
            """
            SELECT target_kind, target_value, resolved_session_id,
                   closure_json, cycle_json, codes_json
            FROM revocations
            ORDER BY target_kind, target_value
            """
        ):
            item = dict(row)
            item["closure"] = json.loads(item.pop("closure_json"))
            item["cycle_session_ids"] = json.loads(item.pop("cycle_json"))
            item["codes"] = json.loads(item.pop("codes_json"))
            revocations.append(item)
        return {
            "meta": meta,
            "sessions": sessions,
            "successor_edges": edges,
            "findings": findings,
            "rejections": rejections,
            "policy_conflicts": conflicts,
            "revocations": revocations,
        }

    def ledger_digest(self) -> str:
        return digest_json(self.ledger_payload())

    def write_checkpoint(
        self,
        *,
        sample_id: str,
        kind: str,
        batch_index: int,
        processed_count: int,
        decision_watermark: int,
    ) -> dict[str, Any]:
        payload = self.ledger_payload()
        ledger_digest = digest_json(payload)
        meta = self._meta()
        checkpoint_id = int(meta.get("checkpoint_seq", "0")) + 1
        snapshot = {
            "checkpoint_version": 1,
            "checkpoint_id": checkpoint_id,
            "kind": kind,
            "sample_id": sample_id,
            "sample_digest": meta.get("sample_digest", ""),
            "layout_version": meta.get("layout_version", ""),
            "batch_index": batch_index,
            "processed_count": processed_count,
            "decision_watermark": decision_watermark,
            "ledger_digest": ledger_digest,
            "ledger": payload,
        }
        relative_path = Path("state") / "checkpoints" / (
            f"checkpoint-{checkpoint_id:06d}-{kind}.json"
        )
        absolute_path = self.workdir / relative_path
        snapshot_digest = atomic_write_json(absolute_path, snapshot)
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO checkpoints(
                    checkpoint_id, sample_id, kind, batch_index,
                    processed_count, decision_watermark, ledger_digest,
                    snapshot_path, snapshot_digest, complete
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    checkpoint_id,
                    sample_id,
                    kind,
                    batch_index,
                    processed_count,
                    decision_watermark,
                    ledger_digest,
                    str(relative_path).replace(os.sep, "/"),
                    snapshot_digest,
                ),
            )
            self._set_meta("checkpoint_seq", str(checkpoint_id))
            self._set_meta("last_checkpoint_id", str(checkpoint_id))
            self._set_meta("batch_index", str(batch_index))
        return {
            "checkpoint_id": checkpoint_id,
            "kind": kind,
            "batch_index": batch_index,
            "processed_count": processed_count,
            "decision_watermark": decision_watermark,
            "ledger_digest": ledger_digest,
            "snapshot_path": str(relative_path).replace(os.sep, "/"),
            "snapshot_digest": snapshot_digest,
        }

    def checkpoint_rows(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM checkpoints ORDER BY checkpoint_id DESC"
            )
        ]

    def _orphan_checkpoint_files(self) -> list[str]:
        referenced = {
            str(row["snapshot_path"]).replace(os.sep, "/")
            for row in self.checkpoint_rows()
        }
        result = []
        for path in sorted(self.checkpoint_dir.glob("checkpoint-*.json")):
            relative = str(path.relative_to(self.workdir)).replace(os.sep, "/")
            if relative not in referenced:
                result.append(relative)
        return result

    def _validate_checkpoint_row(
        self,
        row: dict[str, Any],
        sample: NormalizedSample | None = None,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        issues: list[str] = []
        path = self.workdir / str(row["snapshot_path"])
        try:
            data = path.read_bytes()
        except OSError:
            return None, [f"E_SNAPSHOT_MISSING:{row['checkpoint_id']}"]
        actual_digest = digest_bytes(data)
        if actual_digest != row["snapshot_digest"]:
            issues.append(f"E_SNAPSHOT_DIGEST:{row['checkpoint_id']}")
        try:
            snapshot = json.loads(data.decode("utf-8"))
            payload = snapshot["ledger"]
            snapshot_ledger_digest = snapshot["ledger_digest"]
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            issues.append(f"E_SNAPSHOT_INVALID:{row['checkpoint_id']}")
            return None, issues
        if digest_json(payload) != snapshot_ledger_digest:
            issues.append(f"E_SNAPSHOT_LEDGER_DIGEST:{row['checkpoint_id']}")
        if snapshot_ledger_digest != row["ledger_digest"]:
            issues.append(f"E_CHECKPOINT_LEDGER_MISMATCH:{row['checkpoint_id']}")
        if int(snapshot.get("processed_count", -1)) != int(row["processed_count"]):
            issues.append(f"E_CHECKPOINT_WATERMARK:{row['checkpoint_id']}")
        if sample is not None:
            requests = [
                RevocationRequest(
                    target_kind=str(item["target_kind"]),
                    target_value=str(item["target_value"]),
                )
                for item in payload.get("revocations", [])
            ]
            try:
                expected = _expected_decision_digest(
                    sample,
                    int(snapshot["processed_count"]),
                    requests,
                )
                actual = _decision_tables_digest(payload)
            except (KeyError, TypeError, ValueError):
                expected = ""
                actual = ""
            if expected != actual:
                issues.append(f"E_REPLAY_MISMATCH:{row['checkpoint_id']}")
        if issues:
            return None, issues
        return snapshot, []

    def _restore_payload(
        self,
        snapshot: dict[str, Any],
    ) -> None:
        payload = snapshot["ledger"]
        with self.transaction() as connection:
            for table in (
                "session_ledger",
                "successor_edges",
                "findings",
                "rejections",
                "policy_conflicts",
                "revocations",
            ):
                connection.execute(f"DELETE FROM {table}")
            for item in payload["sessions"]:
                connection.execute(
                    """
                    INSERT INTO session_ledger(
                        session_id, generation, user_id, policy_key,
                        repair_state, policy_generation, binding_check,
                        decision_watermark
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["session_id"],
                        item["generation"],
                        item["user_id"],
                        item["policy_key"],
                        item["repair_state"],
                        item["policy_generation"],
                        item["binding_check"],
                        item["decision_watermark"],
                    ),
                )
            for item in payload["successor_edges"]:
                connection.execute(
                    """
                    INSERT INTO successor_edges(
                        parent_session_id, parent_generation,
                        child_session_id, child_generation, child_issued_at_ms
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        item["parent_session_id"],
                        item["parent_generation"],
                        item["child_session_id"],
                        item["child_generation"],
                        item["child_issued_at_ms"],
                    ),
                )
            for item in payload["findings"]:
                finding_id = digest_json(
                    {
                        "code": item["code"],
                        "session_id": item["session_id"],
                        "generation": item["generation"],
                        "policy_generation": item["policy_generation"],
                        "detail": item["detail"],
                    }
                )
                connection.execute(
                    """
                    INSERT INTO findings(
                        finding_id, code, session_id, generation,
                        policy_generation, decision_watermark, detail_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        finding_id,
                        item["code"],
                        item["session_id"],
                        item["generation"],
                        item["policy_generation"],
                        item["decision_watermark"],
                        canonical_json(item["detail"]),
                    ),
                )
            for item in payload["rejections"]:
                rejection_id = digest_json(item)
                connection.execute(
                    """
                    INSERT INTO rejections(
                        rejection_id, source_category, record_index, code,
                        message, source_ref, record_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rejection_id,
                        item["source_category"],
                        item["record_index"],
                        item["code"],
                        item["message"],
                        item["source_ref"],
                        item["record_digest"],
                    ),
                )
            for item in payload["policy_conflicts"]:
                conflict_id = digest_json(item["detail"])
                connection.execute(
                    """
                    INSERT INTO policy_conflicts(
                        conflict_id, policy_key, field, detail_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        conflict_id,
                        item["policy_key"],
                        item["field"],
                        canonical_json(item["detail"]),
                    ),
                )
            for item in payload["revocations"]:
                revocation_id = digest_json(
                    {
                        "target_kind": item["target_kind"],
                        "target_value": item["target_value"],
                    }
                )
                connection.execute(
                    """
                    INSERT INTO revocations(
                        revocation_id, target_kind, target_value,
                        resolved_session_id, closure_json, cycle_json,
                        codes_json, outcome_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revocation_id,
                        item["target_kind"],
                        item["target_value"],
                        item["resolved_session_id"],
                        canonical_json(item["closure"]),
                        canonical_json(item["cycle_session_ids"]),
                        canonical_json(item["codes"]),
                        canonical_json(item),
                    ),
                )
            for key, value in payload["meta"].items():
                self._set_meta(str(key), str(value))
            self._set_meta("recovery_status", "restored")
            self._set_meta(
                "restored_from_checkpoint",
                str(snapshot["checkpoint_id"]),
            )

    def recover(self, sample: NormalizedSample) -> dict[str, Any]:
        residue = scan_temp_residue(self.workdir)
        issues: list[str] = [
            f"E_CHECKPOINT_ORPHAN:{path}"
            for path in self._orphan_checkpoint_files()
        ]
        selected: dict[str, Any] | None = None
        selected_row: dict[str, Any] | None = None
        for row in self.checkpoint_rows():
            snapshot, row_issues = self._validate_checkpoint_row(row, sample)
            if row_issues:
                issues.extend(row_issues)
                continue
            assert snapshot is not None
            if (
                snapshot.get("sample_digest") != sample.sample_digest
                or snapshot.get("layout_version")
                != self._meta().get("layout_version")
            ):
                continue
            selected = snapshot
            selected_row = row
            break
        restored = False
        half_write = bool(issues or residue)
        if selected is None or selected_row is None:
            return {
                "used_checkpoint_id": None,
                "restored": False,
                "half_write_detected": half_write,
                "issues": sorted(set(issues)),
                "temp_residue": residue,
                "start_processed_count": 0,
                "start_batch_index": 0,
            }
        current_meta = self._meta()
        current_processed = int(current_meta.get("processed_count", "0"))
        current_digest = self.ledger_digest()
        same_processed = current_processed == int(selected["processed_count"])
        if not same_processed or current_digest != selected["ledger_digest"]:
            self._restore_payload(selected)
            restored = True
        return {
            "used_checkpoint_id": selected_row["checkpoint_id"],
            "restored": restored,
            "half_write_detected": half_write,
            "issues": sorted(set(issues)),
            "temp_residue": residue,
            "start_processed_count": int(selected["processed_count"]),
            "start_batch_index": int(selected["batch_index"]),
        }

    def verify_checkpoints(self, sample: NormalizedSample | None = None) -> dict[str, Any]:
        rows = self.checkpoint_rows()
        issues: list[str] = [
            f"E_CHECKPOINT_ORPHAN:{path}"
            for path in self._orphan_checkpoint_files()
        ]
        latest_valid: dict[str, Any] | None = None
        latest_row: dict[str, Any] | None = None
        for row in rows:
            snapshot, row_issues = self._validate_checkpoint_row(row, sample)
            if row_issues:
                issues.extend(row_issues)
                continue
            assert snapshot is not None
            if sample is not None and (
                snapshot.get("sample_digest") != sample.sample_digest
                or snapshot.get("layout_version")
                != self._meta().get("layout_version")
            ):
                issues.append(f"E_CHECKPOINT_SAMPLE_MISMATCH:{row['checkpoint_id']}")
                continue
            latest_valid = snapshot
            latest_row = row
            break
        residue = scan_temp_residue(self.workdir)
        current_digest = self.ledger_digest()
        current_processed = int(self._meta().get("processed_count", "0"))
        recovery_required = False
        matches_current = False
        if latest_valid is not None and latest_row is not None:
            matches_current = (
                latest_valid["ledger_digest"] == current_digest
                and int(latest_valid["processed_count"]) == current_processed
            )
            if not matches_current:
                recovery_required = True
        else:
            recovery_required = True
        half_write = bool(issues or residue)
        return {
            "ok": bool(
                latest_valid is not None
                and matches_current
                and not half_write
                and not recovery_required
            ),
            "half_write_detected": half_write,
            "issues": sorted(set(issues)),
            "temp_residue": residue,
            "latest_complete_checkpoint": (
                {
                    "checkpoint_id": latest_row["checkpoint_id"],
                    "processed_count": latest_row["processed_count"],
                    "batch_index": latest_row["batch_index"],
                    "snapshot_path": latest_row["snapshot_path"],
                }
                if latest_row is not None
                else None
            ),
            "current_ledger_digest": current_digest,
            "checkpoint_ledger_digest": (
                latest_valid["ledger_digest"] if latest_valid is not None else None
            ),
            "ledger_digest_match": matches_current,
            "recovery_required": recovery_required,
            "business_codes": sorted(
                {"E_HALF_WRITE"} if half_write else set()
            ),
        }

    def load_revocation_requests(self) -> list[RevocationRequest]:
        return [
            RevocationRequest(
                target_kind=str(row["target_kind"]),
                target_value=str(row["target_value"]),
            )
            for row in self.connection.execute(
                """
                SELECT target_kind, target_value
                FROM revocations
                ORDER BY target_kind, target_value
                """
            )
        ]

    def add_revocation_requests(
        self,
        requests: list[RevocationRequest],
    ) -> list[RevocationRequest]:
        existing = self.load_revocation_requests()
        merged = sorted(
            set(existing) | set(requests),
            key=lambda item: item.stable_key,
        )
        return merged
