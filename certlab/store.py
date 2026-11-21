from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from cryptography import x509

from .clock import LogicalClock, require_int
from .engine import (
    LoadedCertificate,
    SampleRepository,
    build_and_validate,
    fingerprint,
    load_certificate,
    loaded_from_pem,
)
from .errors import CertLabError, ErrorCode
from .snapshot import REVOCATION_VALUES, TOMBSTONE_VALUES, active_anchors
from .util import (
    canonical_alias,
    fault_point,
    load_json_file,
    sha256_bytes,
    stable_json,
)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    clock_watermark_ms INTEGER NOT NULL,
    view_digest TEXT NOT NULL,
    sample_dir TEXT NOT NULL,
    revision_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trust_versions (
    version INTEGER PRIMARY KEY,
    generation INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    clock_watermark_ms INTEGER NOT NULL,
    view_digest TEXT NOT NULL,
    view_json TEXT NOT NULL,
    sample_dir TEXT NOT NULL,
    patch_hash TEXT,
    tombstone_epoch INTEGER NOT NULL,
    created_event_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS anchors (
    version INTEGER NOT NULL,
    alias TEXT NOT NULL,
    canonical_alias TEXT NOT NULL,
    certificate_pem TEXT NOT NULL,
    fingerprint_sha256 TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    tombstone TEXT NOT NULL,
    revocation TEXT NOT NULL,
    effective_at_ms INTEGER,
    trust_generation INTEGER NOT NULL,
    source_version TEXT NOT NULL,
    source_format TEXT NOT NULL,
    raw_time_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    PRIMARY KEY (version, canonical_alias),
    FOREIGN KEY (version) REFERENCES trust_versions(version)
);

CREATE TABLE IF NOT EXISTS chain_conclusions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cache_key TEXT NOT NULL UNIQUE,
    leaf_id TEXT NOT NULL,
    leaf_fingerprint_sha256 TEXT NOT NULL,
    hostname TEXT NOT NULL,
    purpose TEXT NOT NULL,
    trust_generation INTEGER NOT NULL,
    trust_version INTEGER NOT NULL,
    conclusion_json TEXT NOT NULL,
    dependencies_json TEXT NOT NULL,
    reused_from_id INTEGER,
    committed_event_seq INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS chain_conclusion_generation
ON chain_conclusions(trust_generation, leaf_fingerprint_sha256);

CREATE TABLE IF NOT EXISTS revocation_conclusions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cache_key TEXT NOT NULL UNIQUE,
    certificate_fingerprint_sha256 TEXT NOT NULL,
    issuer_fingerprint_sha256 TEXT,
    trust_generation INTEGER NOT NULL,
    trust_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    conclusion_json TEXT NOT NULL,
    reused_from_id INTEGER,
    committed_event_seq INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS revocation_conclusion_generation
ON revocation_conclusions(trust_generation, certificate_fingerprint_sha256);

CREATE TABLE IF NOT EXISTS hostname_conclusions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cache_key TEXT NOT NULL UNIQUE,
    certificate_fingerprint_sha256 TEXT NOT NULL,
    hostname TEXT NOT NULL,
    trust_generation INTEGER NOT NULL,
    trust_version INTEGER NOT NULL,
    matched INTEGER NOT NULL,
    conclusion_json TEXT NOT NULL,
    reused_from_id INTEGER,
    committed_event_seq INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS hostname_conclusion_generation
ON hostname_conclusions(trust_generation, certificate_fingerprint_sha256);

CREATE TABLE IF NOT EXISTS patch_applications (
    patch_hash TEXT PRIMARY KEY,
    patch_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    base_version INTEGER NOT NULL,
    target_version INTEGER NOT NULL,
    target_generation INTEGER NOT NULL,
    report_json TEXT,
    created_event_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    base_version INTEGER NOT NULL,
    target_version INTEGER,
    patch_hash TEXT,
    owner TEXT,
    created_event_seq INTEGER NOT NULL,
    updated_event_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS job_steps (
    job_id TEXT NOT NULL,
    step_key TEXT NOT NULL,
    status TEXT NOT NULL,
    result_key TEXT,
    updated_event_seq INTEGER NOT NULL,
    PRIMARY KEY (job_id, step_key),
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);

CREATE TABLE IF NOT EXISTS invalidation_events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    patch_hash TEXT NOT NULL,
    from_version INTEGER NOT NULL,
    to_version INTEGER NOT NULL,
    from_generation INTEGER NOT NULL,
    to_generation INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    report_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS invalidation_patch_hash
ON invalidation_events(patch_hash);

CREATE TABLE IF NOT EXISTS audit_events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    job_id TEXT
);
"""


def _chain_key(
    leaf_fingerprint: str, hostname: str, purpose: str, generation: int
) -> str:
    return sha256_bytes(
        stable_json(
            {
                "kind": "chain",
                "leaf_fingerprint_sha256": leaf_fingerprint,
                "hostname": hostname.casefold().rstrip("."),
                "purpose": purpose,
                "trust_generation": generation,
            }
        ).encode("utf-8")
    )


def _revocation_key(
    certificate_fingerprint: str,
    issuer_fingerprint: str | None,
    generation: int,
) -> str:
    return sha256_bytes(
        stable_json(
            {
                "kind": "revocation",
                "certificate_fingerprint_sha256": certificate_fingerprint,
                "issuer_fingerprint_sha256": issuer_fingerprint,
                "trust_generation": generation,
            }
        ).encode("utf-8")
    )


def _hostname_key(certificate_fingerprint: str, hostname: str, generation: int) -> str:
    return sha256_bytes(
        stable_json(
            {
                "kind": "hostname",
                "certificate_fingerprint_sha256": certificate_fingerprint,
                "hostname": hostname.casefold().rstrip("."),
                "trust_generation": generation,
            }
        ).encode("utf-8")
    )


def _signature_ok(child: x509.Certificate, parent: x509.Certificate) -> bool:
    try:
        child.verify_directly_issued_by(parent)
        return True
    except Exception:
        return False


def _read_pem_certificates(directory: Path) -> Iterator[LoadedCertificate]:
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.pem")):
        try:
            yield load_certificate(path, source=str(path))
        except ValueError:
            continue


@dataclass
class TransitionPlan:
    new_view: dict[str, Any]
    target_generation: int
    report: dict[str, Any]
    clones: dict[str, list[dict[str, Any]]]


class StateStore:
    """SQLite-backed, WAL-isolated state and conclusion store."""

    def __init__(
        self,
        database: str | Path,
        *,
        clock_tolerance_ms: int = 5000,
    ):
        self.database = Path(database)
        self.clock_tolerance_ms = clock_tolerance_ms
        self.lock_path = self.database.with_name(self.database.name + ".lock")
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextlib.contextmanager
    def _file_lock(self, *, exclusive: bool = True) -> Iterator[None]:
        self.lock_path.touch(exist_ok=True)
        with self.lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _ensure_schema(self) -> None:
        with self._file_lock():
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.executescript(SCHEMA_SQL)
            finally:
                connection.close()

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        event_type: str,
        detail: dict[str, Any],
        clock_ms: int,
        job_id: str | None = None,
    ) -> int:
        cursor = connection.execute(
            "INSERT INTO audit_events(event_type, detail_json, logical_clock_ms, job_id) "
            "VALUES (?, ?, ?, ?)",
            (event_type, stable_json(detail), clock_ms, job_id),
        )
        return int(cursor.lastrowid)

    def get_state(self) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM state WHERE id = 1").fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def load_view(self, version: int | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        connection = self._connect()
        try:
            state_row = connection.execute("SELECT * FROM state WHERE id = 1").fetchone()
            if state_row is None:
                raise CertLabError(ErrorCode.NO_STATE, "no trust state has been loaded")
            state = dict(state_row)
            selected = version if version is not None else state["version"]
            row = connection.execute(
                "SELECT * FROM trust_versions WHERE version = ?", (selected,)
            ).fetchone()
            if row is None:
                raise CertLabError(
                    ErrorCode.NO_STATE,
                    "requested trust version does not exist",
                    {"version": selected},
                )
            try:
                view = json.loads(row["view_json"])
            except json.JSONDecodeError as exc:
                raise CertLabError(
                    ErrorCode.DB_ERROR,
                    "stored trust view is corrupt",
                    {"version": selected},
                ) from exc
            return state, view
        finally:
            connection.close()

    def _stage_view(
        self, connection: sqlite3.Connection, view: dict[str, Any]
    ) -> None:
        connection.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS stage_anchors (
                alias TEXT,
                canonical_alias TEXT,
                certificate_pem TEXT,
                fingerprint_sha256 TEXT,
                enabled INTEGER,
                tombstone TEXT,
                revocation TEXT,
                effective_at_ms INTEGER,
                trust_generation INTEGER,
                source_version TEXT,
                source_format TEXT,
                raw_time_json TEXT,
                decision_json TEXT
            )
            """
        )
        connection.execute("DELETE FROM stage_anchors")
        connection.executemany(
            "INSERT INTO stage_anchors VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    item["alias"],
                    item["canonical_alias"],
                    item["certificate_pem"],
                    item["fingerprint_sha256"],
                    1 if item["enabled"] else 0,
                    item["tombstone"],
                    item["revocation"],
                    item["effective_at_ms"],
                    item["trust_generation"],
                    item["source"]["version"],
                    item["source"]["format"],
                    stable_json(item["raw_time"]),
                    stable_json(item["decision"]),
                )
                for item in view["anchors"]
            ],
        )

    def _insert_version(
        self,
        connection: sqlite3.Connection,
        *,
        view: dict[str, Any],
        version: int,
        clock_ms: int,
        watermark_ms: int,
        sample_dir: str,
        event_seq: int,
        patch_hash: str | None,
    ) -> None:
        self._stage_view(connection, view)
        connection.execute(
            "INSERT INTO trust_versions("
            "version, generation, logical_clock_ms, clock_watermark_ms, view_digest, "
            "view_json, sample_dir, patch_hash, tombstone_epoch, created_event_seq"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version,
                view["generation"],
                clock_ms,
                watermark_ms,
                view["view_digest"],
                stable_json(view),
                sample_dir,
                patch_hash,
                view["tombstone_epoch"],
                event_seq,
            ),
        )
        connection.execute(
            "INSERT INTO anchors("
            "version, alias, canonical_alias, certificate_pem, fingerprint_sha256, "
            "enabled, tombstone, revocation, effective_at_ms, trust_generation, "
            "source_version, source_format, raw_time_json, decision_json"
            ") SELECT ?, alias, canonical_alias, certificate_pem, fingerprint_sha256, "
            "enabled, tombstone, revocation, effective_at_ms, trust_generation, "
            "source_version, source_format, raw_time_json, decision_json "
            "FROM stage_anchors",
            (version,),
        )
        connection.execute("DROP TABLE stage_anchors")

    def _set_pointer(
        self,
        connection: sqlite3.Connection,
        *,
        version: int,
        generation: int,
        clock_ms: int,
        watermark_ms: int,
        view_digest: str,
        sample_dir: str,
        revision_seq: int,
    ) -> None:
        current = connection.execute("SELECT id FROM state WHERE id = 1").fetchone()
        values = (
            version,
            generation,
            clock_ms,
            watermark_ms,
            view_digest,
            sample_dir,
            revision_seq,
        )
        if current is None:
            connection.execute(
                "INSERT INTO state("
                "id, version, generation, logical_clock_ms, clock_watermark_ms, "
                "view_digest, sample_dir, revision_seq"
                ") VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        else:
            connection.execute(
                "UPDATE state SET version = ?, generation = ?, logical_clock_ms = ?, "
                "clock_watermark_ms = ?, view_digest = ?, sample_dir = ?, revision_seq = ? "
                "WHERE id = 1",
                values,
            )

    @staticmethod
    def _clock_watermark(current_watermark: int, clock_ms: int) -> int:
        return max(current_watermark, clock_ms)

    def _check_clock(self, state: dict[str, Any], clock: LogicalClock) -> None:
        if clock.now_ms < state["clock_watermark_ms"] - self.clock_tolerance_ms:
            raise CertLabError(
                ErrorCode.CLOCK_ROLLBACK,
                "logical clock rolled back beyond tolerance",
                {
                    "clock_ms": clock.now_ms,
                    "watermark_ms": state["clock_watermark_ms"],
                    "tolerance_ms": self.clock_tolerance_ms,
                },
            )

    def initialize(
        self,
        view: dict[str, Any],
        *,
        sample_dir: str | Path,
        clock: LogicalClock,
    ) -> dict[str, Any]:
        sample = str(Path(sample_dir).resolve())
        with self._file_lock():
            connection = self._connect()
            try:
                state_row = connection.execute("SELECT * FROM state WHERE id = 1").fetchone()
                state = dict(state_row) if state_row else None
                if state is not None:
                    self._check_clock(state, clock)
                    if state["view_digest"] == view["view_digest"]:
                        return {
                            "ok": True,
                            "already_loaded": True,
                            "version": state["version"],
                            "generation": state["generation"],
                            "view_digest": state["view_digest"],
                        }
                    if int(view["generation"]) <= int(state["generation"]):
                        raise CertLabError(
                            ErrorCode.GENERATION_ROLLBACK,
                            "replacement snapshot generation is not newer",
                            {
                                "current_generation": state["generation"],
                                "incoming_generation": view["generation"],
                            },
                        )
                version = 1 if state is None else int(state["version"]) + 1
                watermark = (
                    clock.now_ms
                    if state is None
                    else self._clock_watermark(state["clock_watermark_ms"], clock.now_ms)
                )
                connection.execute("BEGIN IMMEDIATE")
                event_seq = self._event(
                    connection,
                    "trust_view_committed",
                    {
                        "version": version,
                        "generation": view["generation"],
                        "view_digest": view["view_digest"],
                        "mode": "initial" if state is None else "replacement",
                    },
                    clock.now_ms,
                )
                self._insert_version(
                    connection,
                    view=view,
                    version=version,
                    clock_ms=clock.now_ms,
                    watermark_ms=watermark,
                    sample_dir=sample,
                    event_seq=event_seq,
                    patch_hash=None,
                )
                report: dict[str, Any] | None = None
                if state is not None:
                    old_state, old_view = self._load_view_on(connection, state["version"])
                    triggers = self._view_diff_triggers(old_view, view)
                    plan = self._plan_transition(
                        connection,
                        old_view=old_view,
                        new_view=view,
                        sample_dir=sample,
                        trigger_type="replacement",
                        trigger_id=f"load:{view['view_digest'][:16]}",
                        from_version=old_state["version"],
                        to_version=version,
                        triggers=triggers,
                    )
                    report = plan.report
                    self._apply_clones(connection, plan, version=version, event_seq=event_seq)
                    connection.execute(
                        "INSERT INTO invalidation_events("
                        "patch_hash, from_version, to_version, from_generation, "
                        "to_generation, logical_clock_ms, report_json"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            report["trigger"]["id"],
                            old_state["version"],
                            version,
                            old_state["generation"],
                            view["generation"],
                            clock.now_ms,
                            stable_json(report),
                        ),
                    )
                self._set_pointer(
                    connection,
                    version=version,
                    generation=view["generation"],
                    clock_ms=clock.now_ms,
                    watermark_ms=watermark,
                    view_digest=view["view_digest"],
                    sample_dir=sample,
                    revision_seq=1 if state is None else int(state["revision_seq"]) + 1,
                )
                connection.execute("COMMIT")
                result: dict[str, Any] = {
                    "ok": True,
                    "already_loaded": False,
                    "version": version,
                    "generation": view["generation"],
                    "logical_clock_ms": clock.now_ms,
                    "clock_watermark_ms": watermark,
                    "view_digest": view["view_digest"],
                    "anchor_count": len(view["anchors"]),
                    "active_anchor_count": len(active_anchors(view)),
                }
                if report is not None:
                    result["invalidation"] = report
                return result
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    @staticmethod
    def _load_view_on(
        connection: sqlite3.Connection, version: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        state_row = connection.execute("SELECT * FROM state WHERE id = 1").fetchone()
        if state_row is None:
            raise CertLabError(ErrorCode.NO_STATE, "no trust state has been loaded")
        row = connection.execute(
            "SELECT * FROM trust_versions WHERE version = ?", (version,)
        ).fetchone()
        if row is None:
            raise CertLabError(
                ErrorCode.NO_STATE,
                "trust version not found",
                {"version": version},
            )
        return dict(state_row), json.loads(row["view_json"])

    @staticmethod
    def _view_diff_triggers(
        old_view: dict[str, Any], new_view: dict[str, Any]
    ) -> list[dict[str, Any]]:
        old = {item["canonical_alias"]: item for item in old_view["anchors"]}
        new = {item["canonical_alias"]: item for item in new_view["anchors"]}
        triggers: list[dict[str, Any]] = []
        for alias in sorted(set(old) | set(new)):
            before = old.get(alias)
            after = new.get(alias)
            if before is None:
                operation = "add"
            elif after is None:
                operation = "delete"
            elif stable_json(before) != stable_json(after):
                operation = "modify"
            else:
                continue
            triggers.append(
                {
                    "alias": alias,
                    "operation": operation,
                    "old_fingerprint_sha256": (
                        before["fingerprint_sha256"] if before else None
                    ),
                    "new_fingerprint_sha256": (
                        after["fingerprint_sha256"] if after else None
                    ),
                }
            )
        return triggers

    @staticmethod
    def _trigger_fingerprints(triggers: list[dict[str, Any]]) -> set[str]:
        result: set[str] = set()
        for trigger in triggers:
            for key in ("old_fingerprint_sha256", "new_fingerprint_sha256"):
                value = trigger.get(key)
                if value:
                    result.add(value)
        return result

    @staticmethod
    def _descendant_closure(
        sample_dir: Path,
        old_view: dict[str, Any],
        new_view: dict[str, Any],
        touched: set[str],
    ) -> set[str]:
        certificates: dict[str, x509.Certificate] = {}
        for directory in (sample_dir / "cas", sample_dir / "leaves"):
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.pem")):
                try:
                    cert = x509.load_pem_x509_certificate(path.read_bytes())
                except ValueError:
                    continue
                fingerprints = fingerprint(cert)
                certificates[fingerprints] = cert
        for view in (old_view, new_view):
            for item in view["anchors"]:
                try:
                    cert = x509.load_pem_x509_certificate(
                        item["certificate_pem"].encode("utf-8")
                    )
                except ValueError:
                    continue
                certificates[fingerprint(cert)] = cert

        adjacency: dict[str, set[str]] = {key: set() for key in certificates}
        for parent_fp, parent in certificates.items():
            for child_fp, child in certificates.items():
                if parent_fp == child_fp:
                    continue
                if child.issuer != parent.subject:
                    continue
                if _signature_ok(child, parent):
                    adjacency[parent_fp].add(child_fp)
        reachable = set(touched)
        queue = sorted(touched)
        while queue:
            current = queue.pop(0)
            for child in sorted(adjacency.get(current, set())):
                if child not in reachable:
                    reachable.add(child)
                    queue.append(child)
        return reachable

    def _plan_transition(
        self,
        connection: sqlite3.Connection,
        *,
        old_view: dict[str, Any],
        new_view: dict[str, Any],
        sample_dir: str,
        trigger_type: str,
        trigger_id: str,
        from_version: int,
        to_version: int,
        triggers: list[dict[str, Any]],
    ) -> TransitionPlan:
        old_generation = int(old_view["generation"])
        new_generation = int(new_view["generation"])
        touched = self._trigger_fingerprints(triggers)
        reachable = self._descendant_closure(
            Path(sample_dir), old_view, new_view, touched
        )
        chain_rows = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM chain_conclusions WHERE trust_generation = ? "
                "ORDER BY cache_key",
                (old_generation,),
            ).fetchall()
        ]
        affected_chains: list[dict[str, Any]] = []
        reused_chains: list[dict[str, Any]] = []
        affected_chain_keys: set[str] = set()
        affected_leaves: set[str] = set()
        affected_certificates = set(reachable)
        for row in chain_rows:
            dependencies = json.loads(row["dependencies_json"])
            dependency_fps = set(dependencies.get("certificate_fingerprints", []))
            affected = bool(dependency_fps & reachable)
            reason = None
            source = None
            if affected:
                for trigger in triggers:
                    if {
                        trigger.get("old_fingerprint_sha256"),
                        trigger.get("new_fingerprint_sha256"),
                    } & dependency_fps:
                        source = trigger
                        break
                if source is None and triggers:
                    source = triggers[0]
                reason = "anchor_dependency_closure"
                affected_chain_keys.add(row["cache_key"])
                affected_leaves.add(row["leaf_fingerprint_sha256"])
                affected_certificates |= dependency_fps
                item = {
                    "cache_key": row["cache_key"],
                    "leaf_id": row["leaf_id"],
                    "leaf_fingerprint_sha256": row["leaf_fingerprint_sha256"],
                    "hostname": row["hostname"],
                    "purpose": row["purpose"],
                    "reason": reason,
                    "trigger_source": source,
                    "action": "invalidate",
                }
                affected_chains.append(item)
            else:
                reused_chains.append(
                    {
                        "cache_key": row["cache_key"],
                        "new_cache_key": _chain_key(
                            row["leaf_fingerprint_sha256"],
                            row["hostname"],
                            row["purpose"],
                            new_generation,
                        ),
                        "leaf_id": row["leaf_id"],
                        "leaf_fingerprint_sha256": row["leaf_fingerprint_sha256"],
                        "hostname": row["hostname"],
                        "purpose": row["purpose"],
                        "reason": "outside_anchor_dependency_closure",
                        "action": "reuse",
                    }
                )

        revocation_rows = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM revocation_conclusions WHERE trust_generation = ? "
                "ORDER BY cache_key",
                (old_generation,),
            ).fetchall()
        ]
        affected_revocations: list[dict[str, Any]] = []
        reused_revocations: list[dict[str, Any]] = []
        for row in revocation_rows:
            affected = row["certificate_fingerprint_sha256"] in affected_certificates
            if affected:
                affected_revocations.append(
                    {
                        "cache_key": row["cache_key"],
                        "certificate_fingerprint_sha256": row[
                            "certificate_fingerprint_sha256"
                        ],
                        "issuer_fingerprint_sha256": row["issuer_fingerprint_sha256"],
                        "status": row["status"],
                        "reason": "trust_generation_or_certificate_in_closure",
                        "trigger_source": triggers[0] if triggers else None,
                        "action": "invalidate",
                    }
                )
            else:
                reused_revocations.append(
                    {
                        "cache_key": row["cache_key"],
                        "new_cache_key": _revocation_key(
                            row["certificate_fingerprint_sha256"],
                            row["issuer_fingerprint_sha256"],
                            new_generation,
                        ),
                        "certificate_fingerprint_sha256": row[
                            "certificate_fingerprint_sha256"
                        ],
                        "issuer_fingerprint_sha256": row["issuer_fingerprint_sha256"],
                        "status": row["status"],
                        "reason": "outside_anchor_dependency_closure",
                        "action": "reuse",
                    }
                )

        hostname_rows = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM hostname_conclusions WHERE trust_generation = ? "
                "ORDER BY cache_key",
                (old_generation,),
            ).fetchall()
        ]
        reused_hostnames: list[dict[str, Any]] = []
        for row in hostname_rows:
            reused_hostnames.append(
                {
                    "cache_key": row["cache_key"],
                    "new_cache_key": _hostname_key(
                        row["certificate_fingerprint_sha256"],
                        row["hostname"],
                        new_generation,
                    ),
                    "certificate_fingerprint_sha256": row[
                        "certificate_fingerprint_sha256"
                    ],
                    "hostname": row["hostname"],
                    "matched": bool(row["matched"]),
                    "reason": "leaf_certificate_and_hostname_unchanged",
                    "action": "reuse",
                }
            )

        affected_chains.sort(key=lambda item: (item["leaf_id"], item["hostname"], item["purpose"]))
        reused_chains.sort(key=lambda item: (item["leaf_id"], item["hostname"], item["purpose"]))
        affected_revocations.sort(key=lambda item: item["cache_key"])
        reused_revocations.sort(key=lambda item: item["cache_key"])
        reused_hostnames.sort(key=lambda item: item["cache_key"])
        report = {
            "schema": "certlab-invalidation-report-v1",
            "trigger": {
                "type": trigger_type,
                "id": trigger_id,
                "from_version": from_version,
                "to_version": to_version,
                "from_generation": old_generation,
                "to_generation": new_generation,
            },
            "trigger_sources": sorted(
                triggers,
                key=lambda item: (
                    item["alias"],
                    item["operation"],
                    str(item.get("old_fingerprint_sha256")),
                ),
            ),
            "closure": {
                "anchor_fingerprints": sorted(touched),
                "certificate_fingerprints": sorted(reachable),
                "affected_leaf_fingerprints": sorted(affected_leaves),
            },
            "invalidated": {
                "chains": affected_chains,
                "revocations": affected_revocations,
                "hostnames": [],
            },
            "reused": {
                "chains": reused_chains,
                "revocations": reused_revocations,
                "hostnames": reused_hostnames,
            },
            "summary": {
                "invalidated_chain_count": len(affected_chains),
                "invalidated_revocation_count": len(affected_revocations),
                "reused_chain_count": len(reused_chains),
                "reused_revocation_count": len(reused_revocations),
                "reused_hostname_count": len(reused_hostnames),
                "hostname_safe_reuse_count": len(reused_hostnames),
            },
        }
        clones = {
            "chains": [
                row
                for row in chain_rows
                if row["cache_key"] not in affected_chain_keys
            ],
            "revocations": [
                row
                for row in revocation_rows
                if row["certificate_fingerprint_sha256"] not in affected_certificates
            ],
            "hostnames": hostname_rows,
        }
        return TransitionPlan(
            new_view=new_view,
            target_generation=new_generation,
            report=report,
            clones=clones,
        )

    def _apply_clones(
        self,
        connection: sqlite3.Connection,
        plan: TransitionPlan,
        *,
        version: int,
        event_seq: int,
    ) -> None:
        generation = plan.target_generation
        for row in plan.clones["chains"]:
            new_key = _chain_key(
                row["leaf_fingerprint_sha256"],
                row["hostname"],
                row["purpose"],
                generation,
            )
            connection.execute(
                "INSERT OR IGNORE INTO chain_conclusions("
                "cache_key, leaf_id, leaf_fingerprint_sha256, hostname, purpose, "
                "trust_generation, trust_version, conclusion_json, dependencies_json, "
                "reused_from_id, committed_event_seq"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_key,
                    row["leaf_id"],
                    row["leaf_fingerprint_sha256"],
                    row["hostname"],
                    row["purpose"],
                    generation,
                    version,
                    row["conclusion_json"],
                    row["dependencies_json"],
                    row["id"],
                    event_seq,
                ),
            )

    @staticmethod
    def _bad_patch(message: str, **details: Any) -> CertLabError:
        return CertLabError(ErrorCode.BAD_PATCH, message, details)

    def _patch_anchor(
        self,
        *,
        alias: str,
        canonical: str,
        pem_text: str,
        expected_fingerprint: str,
        effective_at_ms: int,
        enabled: bool,
        tombstone: str,
        revocation: str,
        generation: int,
        clock_ms: int,
        operation: str,
    ) -> dict[str, Any]:
        try:
            cert = x509.load_pem_x509_certificate(pem_text.encode("utf-8"))
        except Exception as exc:
            raise self._bad_patch(
                "patch certificate PEM cannot be parsed",
                alias=alias,
                cause=type(exc).__name__,
            ) from exc
        actual = fingerprint(cert)
        if expected_fingerprint.casefold() != actual:
            raise CertLabError(
                ErrorCode.HASH_MISMATCH,
                "patch certificate fingerprint does not match PEM",
                {
                    "alias": alias,
                    "expected": expected_fingerprint,
                    "actual": actual,
                    "context": "patch",
                },
            )
        if tombstone not in TOMBSTONE_VALUES:
            raise self._bad_patch("invalid patch tombstone enum", alias=alias, value=tombstone)
        if revocation not in REVOCATION_VALUES:
            raise self._bad_patch("invalid patch revocation enum", alias=alias, value=revocation)
        if not isinstance(enabled, bool):
            raise self._bad_patch("patch enabled must be boolean", alias=alias)
        require_int(effective_at_ms, "effective_at_ms", code=ErrorCode.BAD_PATCH)
        return {
            "alias": alias,
            "canonical_alias": canonical,
            "certificate_pem": pem_text,
            "fingerprint_sha256": actual,
            "subject": cert.subject.rfc4514_string(),
            "enabled": enabled,
            "tombstone": tombstone,
            "revocation": revocation,
            "effective_at_ms": effective_at_ms,
            "trust_generation": generation,
            "source": {
                "version": "patch",
                "format": "incremental-patch",
                "priority": 3,
                "logical_clock_ms": clock_ms,
            },
            "raw_time": {"kind": "absolute_ms", "value": effective_at_ms},
            "time_conversion": {
                "reference_clock_ms": clock_ms,
                "scale_ms": 1,
                "operator": "identity",
                "rounding": "none",
            },
            "decision": {
                "winner_source": "patch",
                "winner_fingerprint_sha256": actual,
                "reason": operation,
                "rule": "incremental_patch_generation",
                "candidates": [],
            },
        }

    def _build_patch_view(
        self,
        current_view: dict[str, Any],
        patch: dict[str, Any],
        *,
        target_generation: int,
        clock_ms: int,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if not isinstance(patch, dict) or patch.get("schema") != "trust-patch-v1":
            raise self._bad_patch("patch schema must be trust-patch-v1")
        if not isinstance(patch.get("patch_id"), str) or not patch["patch_id"].strip():
            raise self._bad_patch("patch_id must be a non-empty string")
        if "logical_clock_ms" in patch:
            declared = require_int(
                patch["logical_clock_ms"], "logical_clock_ms", code=ErrorCode.BAD_PATCH
            )
            if declared != clock_ms:
                raise self._bad_patch(
                    "patch logical_clock_ms differs from injected clock",
                    patch_clock_ms=declared,
                    injected_clock_ms=clock_ms,
                )
        operations = patch.get("operations")
        if not isinstance(operations, list) or not operations:
            raise self._bad_patch("patch operations must be a non-empty array")
        anchors = {
            item["canonical_alias"]: json.loads(stable_json(item))
            for item in current_view["anchors"]
        }
        triggers: list[dict[str, Any]] = []
        seen_operations: set[str] = set()
        updates: list[dict[str, Any]] = []
        for index, operation in enumerate(operations):
            if not isinstance(operation, dict):
                raise self._bad_patch("patch operation must be an object", index=index)
            op = operation.get("op")
            if op not in {"add", "modify", "delete", "tombstone"}:
                raise self._bad_patch("unsupported patch operation", op=op, index=index)
            alias = operation.get("alias")
            if not isinstance(alias, str) or not alias.strip():
                raise self._bad_patch("operation alias must be a non-empty string", index=index)
            canonical = canonical_alias(
                operation.get("canonical_alias", alias)
                if isinstance(operation.get("canonical_alias", alias), str)
                else alias
            )
            if canonical in seen_operations:
                raise self._bad_patch("multiple operations target one canonical alias", alias=alias)
            seen_operations.add(canonical)
            before = anchors.get(canonical)
            if op == "add":
                if before is not None:
                    raise self._bad_patch("patch add target already exists", alias=alias)
                required = {
                    "pem",
                    "fingerprint_sha256",
                    "effective_at_ms",
                    "enabled",
                    "tombstone",
                    "revocation",
                }
                missing = sorted(required - set(operation))
                if missing:
                    raise self._bad_patch("patch add is missing fields", alias=alias, missing=missing)
                after = self._patch_anchor(
                    alias=alias,
                    canonical=canonical,
                    pem_text=operation["pem"],
                    expected_fingerprint=operation["fingerprint_sha256"],
                    effective_at_ms=operation["effective_at_ms"],
                    enabled=operation.get("enabled", True),
                    tombstone=operation.get("tombstone", "none"),
                    revocation=operation.get("revocation", "unknown"),
                    generation=target_generation,
                    clock_ms=clock_ms,
                    operation="add",
                )
            elif op == "delete":
                if before is None:
                    raise self._bad_patch("patch delete target does not exist", alias=alias)
                after = None
            else:
                if before is None:
                    raise self._bad_patch(f"patch {op} target does not exist", alias=alias)
                if op == "tombstone":
                    tombstone = operation.get("tombstone")
                    if tombstone not in {"warning", "tombstoned"}:
                        raise self._bad_patch(
                            "tombstone operation requires warning or tombstoned",
                            alias=alias,
                        )
                    after = json.loads(stable_json(before))
                    after["tombstone"] = tombstone
                    after["enabled"] = False if tombstone == "tombstoned" else after["enabled"]
                    after["trust_generation"] = target_generation
                    after["source"] = {
                        "version": "patch",
                        "format": "incremental-patch",
                        "priority": 3,
                        "logical_clock_ms": clock_ms,
                    }
                    after["decision"] = {
                        "winner_source": "patch",
                        "winner_fingerprint_sha256": after["fingerprint_sha256"],
                        "reason": f"tombstone:{tombstone}",
                        "rule": "incremental_patch_generation",
                        "candidates": [],
                    }
                else:
                    merged = json.loads(stable_json(before))
                    if "pem" in operation or "fingerprint_sha256" in operation:
                        if "pem" not in operation or "fingerprint_sha256" not in operation:
                            raise self._bad_patch(
                                "modify must supply pem and fingerprint together",
                                alias=alias,
                            )
                        after = self._patch_anchor(
                            alias=operation.get("alias", merged["alias"]),
                            canonical=canonical,
                            pem_text=operation["pem"],
                            expected_fingerprint=operation["fingerprint_sha256"],
                            effective_at_ms=operation.get(
                                "effective_at_ms", merged["effective_at_ms"]
                            ),
                            enabled=operation.get("enabled", merged["enabled"]),
                            tombstone=operation.get("tombstone", merged["tombstone"]),
                            revocation=operation.get("revocation", merged["revocation"]),
                            generation=target_generation,
                            clock_ms=clock_ms,
                            operation="modify",
                        )
                    else:
                        after = merged
                        if "effective_at_ms" in operation:
                            after["effective_at_ms"] = require_int(
                                operation["effective_at_ms"],
                                "effective_at_ms",
                                code=ErrorCode.BAD_PATCH,
                            )
                            after["raw_time"] = {
                                "kind": "absolute_ms",
                                "value": after["effective_at_ms"],
                            }
                        for field_name in ("enabled", "tombstone", "revocation"):
                            if field_name in operation:
                                after[field_name] = operation[field_name]
                        if after["tombstone"] not in TOMBSTONE_VALUES:
                            raise self._bad_patch(
                                "invalid tombstone enum", alias=alias, value=after["tombstone"]
                            )
                        if after["revocation"] not in REVOCATION_VALUES:
                            raise self._bad_patch(
                                "invalid revocation enum",
                                alias=alias,
                                value=after["revocation"],
                            )
                        if not isinstance(after["enabled"], bool):
                            raise self._bad_patch("enabled must be boolean", alias=alias)
                        after["trust_generation"] = target_generation
                        after["source"] = {
                            "version": "patch",
                            "format": "incremental-patch",
                            "priority": 3,
                            "logical_clock_ms": clock_ms,
                        }
                        after["decision"] = {
                            "winner_source": "patch",
                            "winner_fingerprint_sha256": after["fingerprint_sha256"],
                            "reason": "modify",
                            "rule": "incremental_patch_generation",
                            "candidates": [],
                        }
            if after is None:
                anchors.pop(canonical, None)
            else:
                anchors[canonical] = after
            triggers.append(
                {
                    "alias": alias,
                    "operation": op,
                    "old_fingerprint_sha256": before["fingerprint_sha256"] if before else None,
                    "new_fingerprint_sha256": after["fingerprint_sha256"] if after else None,
                }
            )
            updates.append(
                {
                    "alias": alias,
                    "operation": op,
                    "old_fingerprint_sha256": before["fingerprint_sha256"] if before else None,
                    "new_fingerprint_sha256": after["fingerprint_sha256"] if after else None,
                }
            )

        new_view = json.loads(stable_json(current_view))
        new_view.pop("view_digest", None)
        new_view["anchors"] = [anchors[key] for key in sorted(anchors)]
        new_view["generation"] = target_generation
        new_view["logical_clock_ms"] = clock_ms
        new_view["tombstone_epoch"] = sum(
            1
            for item in new_view["anchors"]
            if item["tombstone"] in {"warning", "tombstoned"}
        )
        new_view["updates"] = [
            *new_view.get("updates", []),
            {
                "patch_id": patch["patch_id"],
                "target_generation": target_generation,
                "logical_clock_ms": clock_ms,
                "operations": updates,
            },
        ]
        new_view["view_digest"] = sha256_bytes(stable_json(new_view).encode("utf-8"))
        return new_view, triggers

    def apply_patch(
        self,
        patch: dict[str, Any],
        *,
        sample_dir: str | Path | None,
        clock: LogicalClock,
    ) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise self._bad_patch("patch must be an object")
        patch_hash = sha256_bytes(stable_json(patch).encode("utf-8"))
        patch_id = patch.get("patch_id")
        with self._file_lock():
            connection = self._connect()
            try:
                state_row = connection.execute("SELECT * FROM state WHERE id = 1").fetchone()
                if state_row is None:
                    raise CertLabError(ErrorCode.NO_STATE, "no trust state has been loaded")
                state = dict(state_row)
                self._check_clock(state, clock)
                prior_hash = connection.execute(
                    "SELECT * FROM patch_applications WHERE patch_id = ?", (patch_id,)
                ).fetchone()
                if prior_hash is not None and prior_hash["patch_hash"] != patch_hash:
                    raise CertLabError(
                        ErrorCode.PATCH_CONFLICT,
                        "patch_id was already used for different content",
                        {"patch_id": patch_id, "existing_hash": prior_hash["patch_hash"]},
                    )
                existing = connection.execute(
                    "SELECT * FROM patch_applications WHERE patch_hash = ?",
                    (patch_hash,),
                ).fetchone()
                if existing is not None and existing["status"] == "completed":
                    report_row = connection.execute(
                        "SELECT report_json FROM invalidation_events WHERE patch_hash = ? "
                        "ORDER BY event_seq DESC LIMIT 1",
                        (patch_hash,),
                    ).fetchone()
                    report = json.loads(report_row["report_json"]) if report_row else None
                    return self._patch_result(
                        patch,
                        patch_hash,
                        state,
                        existing["target_generation"],
                        report,
                        already_applied=True,
                    )

                _, current_view = self._load_view_on(connection, state["version"])
                patch_triggers: list[dict[str, Any]] | None = None
                if existing is None:
                    target_generation = int(state["generation"]) + 1
                    if "trust_generation" in patch:
                        declared_generation = require_int(
                            patch["trust_generation"],
                            "trust_generation",
                            code=ErrorCode.BAD_PATCH,
                        )
                        if declared_generation < target_generation:
                            raise CertLabError(
                                ErrorCode.GENERATION_ROLLBACK,
                                "patch trust generation would roll back",
                                {
                                    "declared_generation": declared_generation,
                                    "expected_generation": target_generation,
                                },
                            )
                        if declared_generation != target_generation:
                            raise self._bad_patch(
                                "patch trust_generation must be current + 1",
                                declared=declared_generation,
                                expected=target_generation,
                            )
                    if "base_generation" in patch:
                        base_generation = require_int(
                            patch["base_generation"],
                            "base_generation",
                            code=ErrorCode.BAD_PATCH,
                        )
                        if base_generation != state["generation"]:
                            raise CertLabError(
                                ErrorCode.PATCH_CONFLICT,
                                "patch base generation is stale",
                                {
                                    "base_generation": base_generation,
                                    "current_generation": state["generation"],
                                },
                            )
                    new_view, patch_triggers = self._build_patch_view(
                        current_view,
                        patch,
                        target_generation=target_generation,
                        clock_ms=clock.now_ms,
                    )
                    base_version = int(state["version"])
                    target_version = base_version + 1
                    job_id = f"patch:{patch_hash[:32]}"
                    event_seq = self._event(
                        connection,
                        "patch_prepared",
                        {
                            "patch_id": patch_id,
                            "patch_hash": patch_hash,
                            "base_version": base_version,
                            "target_version": target_version,
                            "target_generation": target_generation,
                        },
                        clock.now_ms,
                        job_id,
                    )
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "INSERT INTO patch_applications("
                        "patch_hash, patch_id, status, payload_json, base_version, "
                        "target_version, target_generation, report_json, created_event_seq"
                        ") VALUES (?, ?, 'inflight', ?, ?, ?, ?, NULL, ?)",
                        (
                            patch_hash,
                            patch_id,
                            stable_json(patch),
                            base_version,
                            target_version,
                            target_generation,
                            event_seq,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO jobs("
                        "job_id, kind, status, payload_json, base_version, target_version, "
                        "patch_hash, owner, created_event_seq, updated_event_seq"
                        ") VALUES (?, 'patch', 'running', ?, ?, ?, ?, ?, ?, ?)",
                        (
                            job_id,
                            stable_json({"patch": patch}),
                            base_version,
                            target_version,
                            patch_hash,
                            f"pid:{os.getpid()}",
                            event_seq,
                            event_seq,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO job_steps(job_id, step_key, status, result_key, "
                        "updated_event_seq) VALUES (?, 'view', 'pending', NULL, ?)",
                        (job_id, event_seq),
                    )
                    connection.execute("COMMIT")
                else:
                    target_generation = int(existing["target_generation"])
                    target_version = int(existing["target_version"])
                    base_version = int(existing["base_version"])
                    job_id = f"patch:{patch_hash[:32]}"
                    current_view = json.loads(current_view and stable_json(current_view))
                    if state["version"] != base_version and state["version"] != target_version:
                        raise CertLabError(
                            ErrorCode.PATCH_CONFLICT,
                            "unfinished patch base version has been superseded",
                            {
                                "base_version": base_version,
                                "target_version": target_version,
                                "current_version": state["version"],
                            },
                        )
                    if state["version"] == base_version:
                        new_view, patch_triggers = self._build_patch_view(
                            current_view,
                            patch,
                            target_generation=target_generation,
                            clock_ms=clock.now_ms,
                        )
                    else:
                        row = connection.execute(
                            "SELECT * FROM trust_versions WHERE version = ?",
                            (target_version,),
                        ).fetchone()
                        if row is None or row["patch_hash"] != patch_hash:
                            raise CertLabError(
                                ErrorCode.PATCH_CONFLICT,
                                "target version belongs to another patch",
                            )
                        new_view = json.loads(row["view_json"])

                if state["version"] == target_version:
                    self._complete_patch(
                        connection, patch_hash, job_id, target_version, clock.now_ms
                    )
                    report_row = connection.execute(
                        "SELECT report_json FROM invalidation_events WHERE patch_hash = ? "
                        "ORDER BY event_seq DESC LIMIT 1",
                        (patch_hash,),
                    ).fetchone()
                    report = json.loads(report_row["report_json"]) if report_row else None
                    return self._patch_result(
                        patch,
                        patch_hash,
                        self.get_state() or state,
                        target_generation,
                        report,
                        already_applied=False,
                    )
                if state["version"] != base_version:
                    raise CertLabError(
                        ErrorCode.PATCH_CONFLICT,
                        "patch base version no longer current",
                        {
                            "base_version": base_version,
                            "current_version": state["version"],
                        },
                    )
                sample = (
                    str(Path(sample_dir).resolve())
                    if sample_dir is not None
                    else state["sample_dir"]
                )
                triggers = (
                    patch_triggers
                    if patch_triggers is not None
                    else self._view_diff_triggers(current_view, new_view)
                )
                plan = self._plan_transition(
                    connection,
                    old_view=current_view,
                    new_view=new_view,
                    sample_dir=sample,
                    trigger_type="patch",
                    trigger_id=patch_hash,
                    from_version=base_version,
                    to_version=target_version,
                    triggers=triggers,
                )
                fault_point("apply_after_prepare")
                watermark = self._clock_watermark(state["clock_watermark_ms"], clock.now_ms)
                connection.execute("BEGIN IMMEDIATE")
                event_seq = self._event(
                    connection,
                    "trust_view_committed",
                    {
                        "version": target_version,
                        "generation": target_generation,
                        "view_digest": new_view["view_digest"],
                        "patch_hash": patch_hash,
                        "mode": "patch",
                    },
                    clock.now_ms,
                    job_id,
                )
                self._insert_version(
                    connection,
                    view=new_view,
                    version=target_version,
                    clock_ms=clock.now_ms,
                    watermark_ms=watermark,
                    sample_dir=sample,
                    event_seq=event_seq,
                    patch_hash=patch_hash,
                )
                self._apply_clones(
                    connection, plan, version=target_version, event_seq=event_seq
                )
                connection.execute(
                    "INSERT INTO invalidation_events("
                    "patch_hash, from_version, to_version, from_generation, "
                    "to_generation, logical_clock_ms, report_json"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        patch_hash,
                        base_version,
                        target_version,
                        current_view["generation"],
                        target_generation,
                        clock.now_ms,
                        stable_json(plan.report),
                    ),
                )
                self._set_pointer(
                    connection,
                    version=target_version,
                    generation=target_generation,
                    clock_ms=clock.now_ms,
                    watermark_ms=watermark,
                    view_digest=new_view["view_digest"],
                    sample_dir=sample,
                    revision_seq=int(state["revision_seq"]) + 1,
                )
                connection.execute(
                    "UPDATE patch_applications SET status = 'view_committed', report_json = ? "
                    "WHERE patch_hash = ?",
                    (stable_json(plan.report), patch_hash),
                )
                connection.execute(
                    "UPDATE job_steps SET status = 'done', result_key = ?, "
                    "updated_event_seq = ? WHERE job_id = ? AND step_key = 'view'",
                    (new_view["view_digest"], event_seq, job_id),
                )
                connection.execute(
                    "UPDATE jobs SET status = 'running', updated_event_seq = ? WHERE job_id = ?",
                    (event_seq, job_id),
                )
                connection.execute("COMMIT")
                fault_point("apply_after_view_commit")
                self._complete_patch(
                    connection, patch_hash, job_id, target_version, clock.now_ms
                )
                return self._patch_result(
                    patch,
                    patch_hash,
                    self.get_state() or state,
                    target_generation,
                    plan.report,
                    already_applied=False,
                )
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    def _complete_patch(
        self,
        connection: sqlite3.Connection,
        patch_hash: str,
        job_id: str,
        target_version: int,
        clock_ms: int,
    ) -> None:
        event_seq = self._event(
            connection,
            "patch_completed",
            {"patch_hash": patch_hash, "version": target_version},
            clock_ms,
            job_id,
        )
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE patch_applications SET status = 'completed' WHERE patch_hash = ?",
            (patch_hash,),
        )
        connection.execute(
            "UPDATE job_steps SET status = 'done', updated_event_seq = ? "
            "WHERE job_id = ? AND step_key = 'view'",
            (event_seq, job_id),
        )
        connection.execute(
            "UPDATE jobs SET status = 'completed', updated_event_seq = ? WHERE job_id = ?",
            (event_seq, job_id),
        )
        connection.execute("COMMIT")

    @staticmethod
    def _patch_result(
        patch: dict[str, Any],
        patch_hash: str,
        state: dict[str, Any],
        target_generation: int,
        report: dict[str, Any] | None,
        *,
        already_applied: bool,
    ) -> dict[str, Any]:
        result = {
            "ok": True,
            "patch_id": patch.get("patch_id"),
            "patch_hash": patch_hash,
            "already_applied": already_applied,
            "version": state["version"],
            "generation": target_generation,
            "invalidation": report,
        }
        if report is not None:
            result["summary"] = report["summary"]
            result["reused"] = report["reused"]
        return result

    @contextlib.contextmanager
    def _key_lock(self, key: str) -> Iterator[None]:
        directory = self.database.parent / (self.database.name + ".locks")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (key + ".lock")
        path.touch(exist_ok=True)
        with path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _normalize_requests(
        requests: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for index, request in enumerate(requests):
            if not isinstance(request, dict):
                raise CertLabError(
                    ErrorCode.INVALID_ARGUMENT,
                    "verification request must be an object",
                    {"index": index},
                )
            leaf_id = request.get("leaf_id")
            hostname = request.get("hostname")
            purpose = request.get("purpose")
            if not isinstance(leaf_id, str) or not leaf_id.strip():
                raise CertLabError(
                    ErrorCode.INVALID_ARGUMENT,
                    "verification leaf_id is required",
                    {"index": index},
                )
            if not isinstance(hostname, str) or not hostname.strip():
                raise CertLabError(
                    ErrorCode.INVALID_ARGUMENT,
                    "verification hostname is required",
                    {"index": index, "leaf_id": leaf_id},
                )
            if not isinstance(purpose, str) or not purpose.strip():
                raise CertLabError(
                    ErrorCode.INVALID_ARGUMENT,
                    "verification purpose is required",
                    {"index": index, "leaf_id": leaf_id},
                )
            item = {
                "leaf_id": leaf_id,
                "hostname": hostname,
                "purpose": purpose,
            }
            key = (leaf_id, hostname.casefold().rstrip("."), purpose)
            if key not in seen:
                seen.add(key)
                normalized.append(item)
        if not normalized:
            raise CertLabError(
                ErrorCode.INVALID_ARGUMENT, "at least one verification request is required"
            )
        return normalized

    @staticmethod
    def _job_id(
        requests: list[dict[str, str]], generation: int, view_version: int
    ) -> str:
        digest = sha256_bytes(
            stable_json(
                {
                    "kind": "verify",
                    "requests": requests,
                    "generation": generation,
                    "view_version": view_version,
                }
            ).encode("utf-8")
        )
        return f"verify:{digest[:32]}"

    def verify_requests(
        self,
        requests: list[dict[str, Any]],
        *,
        clock: LogicalClock,
        sample_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        normalized = self._normalize_requests(requests)
        state = self.get_state()
        if state is None:
            raise CertLabError(ErrorCode.NO_STATE, "no trust state has been loaded")
        self._check_clock(state, clock)
        view_version = int(state["version"])
        view_generation = int(state["generation"])
        job_id = self._job_id(normalized, view_generation, view_version)
        sample = (
            str(Path(sample_dir).resolve())
            if sample_dir is not None
            else state["sample_dir"]
        )
        already_completed = False
        with self._file_lock():
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                if row is not None and row["status"] == "completed":
                    already_completed = True
                elif row is None:
                    event_seq = self._event(
                        connection,
                        "verification_job_created",
                        {
                            "job_id": job_id,
                            "request_count": len(normalized),
                            "view_version": view_version,
                        },
                        clock.now_ms,
                        job_id,
                    )
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "INSERT INTO jobs("
                        "job_id, kind, status, payload_json, base_version, target_version, "
                        "patch_hash, owner, created_event_seq, updated_event_seq"
                        ") VALUES (?, 'verify', 'running', ?, ?, NULL, NULL, ?, ?, ?)",
                        (
                            job_id,
                            stable_json(
                                {
                                    "requests": normalized,
                                    "sample_dir": sample,
                                    "view_version": view_version,
                                    "generation": view_generation,
                                }
                            ),
                            view_version,
                            f"pid:{os.getpid()}",
                            event_seq,
                            event_seq,
                        ),
                    )
                    for request in normalized:
                        probe = self._request_cache_key(
                            normalized_request=request,
                            generation=view_generation,
                            repository=None,
                        )
                        connection.execute(
                            "INSERT INTO job_steps("
                            "job_id, step_key, status, result_key, updated_event_seq"
                            ") VALUES (?, ?, 'pending', NULL, ?)",
                            (job_id, probe, event_seq),
                        )
                    connection.execute("COMMIT")
                else:
                    payload = json.loads(row["payload_json"])
                    sample = payload.get("sample_dir", sample)
                    view_version = int(payload.get("view_version", view_version))
                    view_generation = int(payload.get("generation", view_generation))
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

        return self._execute_verify_job(
            job_id=job_id,
            normalized=normalized,
            sample=sample,
            view_version=view_version,
            view_generation=view_generation,
            clock=clock,
            already_completed=already_completed,
        )

    def _execute_verify_job(
        self,
        *,
        job_id: str,
        normalized: list[dict[str, str]],
        sample: str,
        view_version: int,
        view_generation: int,
        clock: LogicalClock,
        already_completed: bool,
    ) -> dict[str, Any]:
        _, view = self.load_view(view_version)
        repository = SampleRepository(sample, view)
        computed = 0
        reused = 0
        results: list[dict[str, Any]] = []
        for request in normalized:
            leaf_entry, leaf = repository.load_leaf(request["leaf_id"])
            hostname = request["hostname"] or str(leaf_entry.get("hostname"))
            purpose = request["purpose"] or str(leaf_entry.get("purpose"))
            step_key = self._request_cache_key(
                normalized_request=request,
                generation=view_generation,
                repository=None,
            )
            cache_key = _chain_key(
                leaf.fingerprint_sha256, hostname, purpose, view_generation
            )
            from_cache = self._chain_cache(cache_key)
            if from_cache is not None:
                self._mark_step_done(job_id, step_key, clock.now_ms, cache_key)
                reused += 1
                results.append(
                    {
                        "leaf_id": request["leaf_id"],
                        "hostname": hostname,
                        "purpose": purpose,
                        "cache_key": cache_key,
                        "reused": True,
                        "conclusion": json.loads(from_cache["conclusion_json"]),
                    }
                )
                continue
            with self._key_lock(cache_key):
                from_cache = self._chain_cache(cache_key)
                if from_cache is not None:
                    self._mark_step_done(job_id, step_key, clock.now_ms, cache_key)
                    reused += 1
                    results.append(
                        {
                            "leaf_id": request["leaf_id"],
                            "hostname": hostname,
                            "purpose": purpose,
                            "cache_key": cache_key,
                            "reused": True,
                            "conclusion": json.loads(from_cache["conclusion_json"]),
                        }
                    )
                    continue
                try:
                    conclusion = build_and_validate(
                        repository,
                        leaf,
                        hostname=hostname,
                        purpose=purpose,
                        now_ms=clock.now_ms,
                    )
                except Exception as exc:
                    conclusion = {
                        "schema": "certlab-chain-result-v1",
                        "valid": False,
                        "error_code": ErrorCode.VALIDATION_INTERNAL,
                        "primary_error": {
                            "code": ErrorCode.VALIDATION_INTERNAL,
                            "check": "unexpected_exception",
                            "details": {
                                "exception_type": type(exc).__name__,
                                "message": str(exc),
                                "leaf_fingerprint_sha256": leaf.fingerprint_sha256,
                            },
                        },
                        "leaf": leaf.summary("leaf"),
                        "anchor": None,
                        "hostname": hostname,
                        "purpose": purpose,
                        "observed_at_ms": clock.now_ms,
                        "path": [],
                        "steps": [],
                        "hostname_match": {"matched": False},
                        "revocation": [],
                        "cache_dependencies": {
                            "certificate_fingerprints": [leaf.fingerprint_sha256],
                            "anchor_fingerprint_sha256": None,
                            "leaf_fingerprint_sha256": leaf.fingerprint_sha256,
                        },
                    }
                fault_point("verify_after_compute")
                conclusion_json = stable_json(conclusion)
                dependencies = conclusion.get("cache_dependencies", {})
                self._commit_cached_conclusion(
                    job_id=job_id,
                    step_key=step_key,
                    cache_key=cache_key,
                    leaf_id=request["leaf_id"],
                    leaf=leaf,
                    hostname=hostname,
                    purpose=purpose,
                    generation=view_generation,
                    version=view_version,
                    conclusion_json=conclusion_json,
                    dependencies=dependencies,
                    clock_ms=clock.now_ms,
                )
                computed += 1
                results.append(
                    {
                        "leaf_id": request["leaf_id"],
                        "hostname": hostname,
                        "purpose": purpose,
                        "cache_key": cache_key,
                        "reused": False,
                        "conclusion": conclusion,
                    }
                )
                fault_point("verify_after_step_commit")
        self._complete_verify_job(job_id, clock.now_ms)
        return {
            "ok": True,
            "job_id": job_id,
            "trust_version": view_version,
            "trust_generation": view_generation,
            "already_completed": already_completed,
            "computed_count": computed,
            "reused_count": reused,
            "results": results,
        }

    @staticmethod
    def _request_cache_key(
        *,
        normalized_request: dict[str, str],
        generation: int,
        repository: SampleRepository | None,
    ) -> str:
        if repository is None:
            return sha256_bytes(
                stable_json(
                    {
                        "kind": "pending-request",
                        "request": normalized_request,
                        "generation": generation,
                    }
                ).encode("utf-8")
            )
        entry, leaf = repository.load_leaf(normalized_request["leaf_id"])
        hostname = normalized_request["hostname"] or str(entry.get("hostname"))
        purpose = normalized_request["purpose"] or str(entry.get("purpose"))
        return _chain_key(leaf.fingerprint_sha256, hostname, purpose, generation)

    def _chain_cache(self, cache_key: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM chain_conclusions WHERE cache_key = ?", (cache_key,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def _mark_step_done(
        self, job_id: str, step_key: str, clock_ms: int, result_key: str
    ) -> None:
        connection = self._connect()
        try:
            event_seq = self._event(
                connection,
                "verification_step_reused",
                {"job_id": job_id, "step_key": step_key},
                clock_ms,
                job_id,
            )
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM job_steps WHERE job_id = ? AND step_key = ?",
                (job_id, step_key),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO job_steps(job_id, step_key, status, result_key, "
                    "updated_event_seq) VALUES (?, ?, 'done', ?, ?)",
                    (job_id, step_key, result_key, event_seq),
                )
            elif row["status"] != "done":
                connection.execute(
                    "UPDATE job_steps SET status = 'done', result_key = ?, "
                    "updated_event_seq = ? WHERE job_id = ? AND step_key = ?",
                    (result_key, event_seq, job_id, step_key),
                )
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _commit_cached_conclusion(
        self,
        *,
        job_id: str,
        step_key: str,
        cache_key: str,
        leaf_id: str,
        leaf: LoadedCertificate,
        hostname: str,
        purpose: str,
        generation: int,
        version: int,
        conclusion_json: str,
        dependencies: dict[str, Any],
        clock_ms: int,
    ) -> None:
        conclusion = json.loads(conclusion_json)
        connection = self._connect()
        try:
            event_seq = self._event(
                connection,
                "chain_conclusion_committed",
                {
                    "job_id": job_id,
                    "cache_key": cache_key,
                    "trust_generation": generation,
                    "leaf_id": leaf_id,
                },
                clock_ms,
                job_id,
            )
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO chain_conclusions("
                "cache_key, leaf_id, leaf_fingerprint_sha256, hostname, purpose, "
                "trust_generation, trust_version, conclusion_json, dependencies_json, "
                "reused_from_id, committed_event_seq"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    cache_key,
                    leaf_id,
                    leaf.fingerprint_sha256,
                    hostname,
                    purpose,
                    generation,
                    version,
                    conclusion_json,
                    stable_json(dependencies),
                    event_seq,
                ),
            )
            for revocation in conclusion.get("revocation", []):
                cert_fp = revocation["certificate_fingerprint_sha256"]
                issuer_fp = revocation.get("issuer_fingerprint_sha256")
                revocation_key = _revocation_key(cert_fp, issuer_fp, generation)
                connection.execute(
                    "INSERT OR IGNORE INTO revocation_conclusions("
                    "cache_key, certificate_fingerprint_sha256, "
                    "issuer_fingerprint_sha256, trust_generation, trust_version, "
                    "status, conclusion_json, reused_from_id, committed_event_seq"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                    (
                        revocation_key,
                        cert_fp,
                        issuer_fp,
                        generation,
                        version,
                        revocation["status"],
                        stable_json(revocation),
                        event_seq,
                    ),
                )
            hostname_result = conclusion.get("hostname_match", {})
            hostname_key = _hostname_key(leaf.fingerprint_sha256, hostname, generation)
            connection.execute(
                "INSERT OR IGNORE INTO hostname_conclusions("
                "cache_key, certificate_fingerprint_sha256, hostname, "
                "trust_generation, trust_version, matched, conclusion_json, "
                "reused_from_id, committed_event_seq"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    hostname_key,
                    leaf.fingerprint_sha256,
                    hostname,
                    generation,
                    version,
                    1 if hostname_result.get("matched") else 0,
                    stable_json(hostname_result),
                    event_seq,
                ),
            )
            step = connection.execute(
                "SELECT status FROM job_steps WHERE job_id = ? AND step_key = ?",
                (job_id, step_key),
            ).fetchone()
            if step is None:
                connection.execute(
                    "INSERT INTO job_steps(job_id, step_key, status, result_key, "
                    "updated_event_seq) VALUES (?, ?, 'done', ?, ?)",
                    (job_id, step_key, cache_key, event_seq),
                )
            else:
                connection.execute(
                    "UPDATE job_steps SET status = 'done', result_key = ?, "
                    "updated_event_seq = ? WHERE job_id = ? AND step_key = ?",
                    (cache_key, event_seq, job_id, step_key),
                )
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _complete_verify_job(self, job_id: str, clock_ms: int) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is not None and row["status"] != "completed":
                event_seq = self._event(
                    connection,
                    "verification_job_completed",
                    {"job_id": job_id},
                    clock_ms,
                    job_id,
                )
                connection.execute(
                    "UPDATE job_steps SET status = 'done', updated_event_seq = ? "
                    "WHERE job_id = ? AND status != 'done'",
                    (event_seq, job_id),
                )
                connection.execute(
                    "UPDATE jobs SET status = 'completed', updated_event_seq = ? "
                    "WHERE job_id = ?",
                    (event_seq, job_id),
                )
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def resume(self, *, clock: LogicalClock) -> dict[str, Any]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status != 'completed' ORDER BY created_event_seq"
            ).fetchall()
            jobs = [dict(row) for row in rows]
        finally:
            connection.close()
        resumed: list[dict[str, Any]] = []
        for job in jobs:
            payload = json.loads(job["payload_json"])
            if job["kind"] == "patch":
                result = self.apply_patch(
                    payload["patch"], sample_dir=None, clock=clock
                )
                resumed.append(
                    {
                        "job_id": job["job_id"],
                        "kind": "patch",
                        "status": "completed",
                        "version": result["version"],
                        "generation": result["generation"],
                        "already_applied": result["already_applied"],
                    }
                )
            elif job["kind"] == "verify":
                normalized = self._normalize_requests(payload["requests"])
                result = self._execute_verify_job(
                    job_id=job["job_id"],
                    normalized=normalized,
                    sample=payload["sample_dir"],
                    view_version=int(payload["view_version"]),
                    view_generation=int(payload["generation"]),
                    clock=clock,
                    already_completed=False,
                )
                resumed.append(
                    {
                        "job_id": job["job_id"],
                        "kind": "verify",
                        "status": "completed",
                        "computed_count": result["computed_count"],
                        "reused_count": result["reused_count"],
                    }
                )
            else:
                resumed.append(
                    {
                        "job_id": job["job_id"],
                        "kind": job["kind"],
                        "status": "skipped",
                        "reason": "unknown_job_kind",
                    }
                )
        connection = self._connect()
        try:
            remaining = [
                dict(row)
                for row in connection.execute(
                    "SELECT job_id, kind, status FROM jobs WHERE status != 'completed' "
                    "ORDER BY created_event_seq"
                ).fetchall()
            ]
        finally:
            connection.close()
        return {
            "ok": not remaining,
            "resumed": resumed,
            "remaining_jobs": remaining,
        }

    def status(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            state_row = connection.execute("SELECT * FROM state WHERE id = 1").fetchone()
            if state_row is None:
                return {
                    "ok": True,
                    "loaded": False,
                    "database": str(self.database),
                    "jobs": [],
                    "counts": {},
                }
            state = dict(state_row)
            jobs = [
                dict(row)
                for row in connection.execute(
                    "SELECT job_id, kind, status, base_version, target_version "
                    "FROM jobs ORDER BY created_event_seq"
                ).fetchall()
            ]
            current_generation = state["generation"]
            counts = {
                "anchors": connection.execute(
                    "SELECT COUNT(*) FROM anchors WHERE version = ?",
                    (state["version"],),
                ).fetchone()[0],
                "chain_conclusions": connection.execute(
                    "SELECT COUNT(*) FROM chain_conclusions WHERE trust_generation = ?",
                    (current_generation,),
                ).fetchone()[0],
                "revocation_conclusions": connection.execute(
                    "SELECT COUNT(*) FROM revocation_conclusions WHERE trust_generation = ?",
                    (current_generation,),
                ).fetchone()[0],
                "hostname_conclusions": connection.execute(
                    "SELECT COUNT(*) FROM hostname_conclusions WHERE trust_generation = ?",
                    (current_generation,),
                ).fetchone()[0],
                "invalidation_events": connection.execute(
                    "SELECT COUNT(*) FROM invalidation_events"
                ).fetchone()[0],
            }
            latest = connection.execute(
                "SELECT patch_hash, from_version, to_version, from_generation, "
                "to_generation, logical_clock_ms, report_json FROM invalidation_events "
                "ORDER BY event_seq DESC LIMIT 1"
            ).fetchone()
            result = {
                "ok": True,
                "loaded": True,
                "database": str(self.database),
                "version": state["version"],
                "generation": state["generation"],
                "logical_clock_ms": state["logical_clock_ms"],
                "clock_watermark_ms": state["clock_watermark_ms"],
                "view_digest": state["view_digest"],
                "sample_dir": state["sample_dir"],
                "revision_seq": state["revision_seq"],
                "counts": counts,
                "jobs": jobs,
                "pending_job_count": sum(
                    1 for job in jobs if job["status"] != "completed"
                ),
            }
            if latest is not None:
                result["latest_invalidation"] = {
                    "patch_hash": latest["patch_hash"],
                    "from_version": latest["from_version"],
                    "to_version": latest["to_version"],
                    "from_generation": latest["from_generation"],
                    "to_generation": latest["to_generation"],
                    "logical_clock_ms": latest["logical_clock_ms"],
                    "summary": json.loads(latest["report_json"])["summary"],
                }
            return result
        finally:
            connection.close()

    def verify_audit(self) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        checks: list[dict[str, Any]] = []

        def add(check: str, ok: bool, **details: Any) -> None:
            checks.append({"check": check, "ok": ok, "details": details})
            if not ok:
                issues.append({"check": check, **details})

        connection = self._connect()
        try:
            state_row = connection.execute("SELECT * FROM state WHERE id = 1").fetchone()
            add("state_present", state_row is not None)
            if state_row is None:
                return {
                    "ok": False,
                    "checks": checks,
                    "issues": issues,
                }
            state = dict(state_row)
            version_row = connection.execute(
                "SELECT * FROM trust_versions WHERE version = ?", (state["version"],)
            ).fetchone()
            add("current_version_present", version_row is not None)
            if version_row is None:
                return {"ok": False, "checks": checks, "issues": issues}
            view = json.loads(version_row["view_json"])
            digest_payload = dict(view)
            stored_digest = digest_payload.pop("view_digest", None)
            recomputed = sha256_bytes(stable_json(digest_payload).encode("utf-8"))
            add(
                "view_digest_matches",
                stored_digest == recomputed == state["view_digest"],
                stored=stored_digest,
                recomputed=recomputed,
                pointer=state["view_digest"],
            )
            anchor_count = connection.execute(
                "SELECT COUNT(*) FROM anchors WHERE version = ?", (state["version"],)
            ).fetchone()[0]
            add(
                "anchor_count_matches",
                anchor_count == len(view["anchors"]),
                stored=anchor_count,
                view=len(view["anchors"]),
            )
            add(
                "generation_matches",
                int(view["generation"]) == int(state["generation"]),
                view=view["generation"],
                pointer=state["generation"],
            )
            add(
                "clock_watermark_monotonic",
                int(state["clock_watermark_ms"]) >= int(version_row["logical_clock_ms"]),
                watermark=state["clock_watermark_ms"],
                version_clock=version_row["logical_clock_ms"],
            )
            current_chains = connection.execute(
                "SELECT cache_key, trust_generation, dependencies_json FROM chain_conclusions "
                "WHERE trust_generation = ?",
                (state["generation"],),
            ).fetchall()
            bad_dependencies = 0
            for row in current_chains:
                try:
                    dependencies = json.loads(row["dependencies_json"])
                    if not isinstance(
                        dependencies.get("certificate_fingerprints"), list
                    ):
                        bad_dependencies += 1
                except Exception:
                    bad_dependencies += 1
            add(
                "chain_dependencies_valid",
                bad_dependencies == 0,
                checked=len(current_chains),
                invalid=bad_dependencies,
            )
            pending_jobs = connection.execute(
                "SELECT job_id, kind, status FROM jobs WHERE status != 'completed' "
                "ORDER BY created_event_seq"
            ).fetchall()
            add(
                "no_pending_jobs",
                not pending_jobs,
                pending=[dict(row) for row in pending_jobs],
            )
            bad_steps = connection.execute(
                "SELECT COUNT(*) FROM job_steps js JOIN jobs j ON j.job_id = js.job_id "
                "WHERE j.status = 'completed' AND js.status != 'done'"
            ).fetchone()[0]
            add("completed_jobs_have_done_steps", bad_steps == 0, invalid=bad_steps)
            reports = connection.execute(
                "SELECT report_json FROM invalidation_events ORDER BY event_seq"
            ).fetchall()
            bad_reports = 0
            for report in reports:
                try:
                    parsed = json.loads(report["report_json"])
                    if not isinstance(parsed.get("trigger_sources"), list):
                        bad_reports += 1
                except Exception:
                    bad_reports += 1
            add(
                "invalidation_reports_parse",
                bad_reports == 0,
                checked=len(reports),
                invalid=bad_reports,
            )
            return {
                "ok": not issues,
                "checks": checks,
                "issues": issues,
                "version": state["version"],
                "generation": state["generation"],
                "view_digest": state["view_digest"],
            }
        finally:
            connection.close()
        for row in plan.clones["revocations"]:
            new_key = _revocation_key(
                row["certificate_fingerprint_sha256"],
                row["issuer_fingerprint_sha256"],
                generation,
            )
            connection.execute(
                "INSERT OR IGNORE INTO revocation_conclusions("
                "cache_key, certificate_fingerprint_sha256, issuer_fingerprint_sha256, "
                "trust_generation, trust_version, status, conclusion_json, "
                "reused_from_id, committed_event_seq"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_key,
                    row["certificate_fingerprint_sha256"],
                    row["issuer_fingerprint_sha256"],
                    generation,
                    version,
                    row["status"],
                    row["conclusion_json"],
                    row["id"],
                    event_seq,
                ),
            )
        for row in plan.clones["hostnames"]:
            new_key = _hostname_key(
                row["certificate_fingerprint_sha256"],
                row["hostname"],
                generation,
            )
            connection.execute(
                "INSERT OR IGNORE INTO hostname_conclusions("
                "cache_key, certificate_fingerprint_sha256, hostname, trust_generation, "
                "trust_version, matched, conclusion_json, reused_from_id, "
                "committed_event_seq"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_key,
                    row["certificate_fingerprint_sha256"],
                    row["hostname"],
                    generation,
                    version,
                    row["matched"],
                    row["conclusion_json"],
                    row["id"],
                    event_seq,
                ),
            )
        else:
            connection.execute(
                "UPDATE state SET version = ?, generation = ?, logical_clock_ms = ?, "
                "clock_watermark_ms = ?, view_digest = ?, sample_dir = ?, revision_seq = ? "
                "WHERE id = 1",
                values,
            )

