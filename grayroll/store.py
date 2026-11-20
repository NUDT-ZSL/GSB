from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .errors import GrayrollError
from .merge import merge_candidates
from .model import CandidateBatch, LoadedPackage, MergeResult
from .util import canonical_json, natural_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS packages (
    package_id TEXT PRIMARY KEY,
    manifest_version INTEGER NOT NULL,
    generation_floor INTEGER NOT NULL,
    service TEXT NOT NULL,
    tier_digest TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    defaults_json TEXT NOT NULL,
    classification TEXT NOT NULL,
    state TEXT NOT NULL,
    staged_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS source_candidates (
    package_id TEXT NOT NULL,
    service TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    source_version TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    earliest_rollout_ms INTEGER NOT NULL,
    tiers_json TEXT NOT NULL,
    markers_json TEXT NOT NULL,
    criteria_json TEXT NOT NULL,
    rollback_start_ms INTEGER NOT NULL,
    rollback_end_ms INTEGER NOT NULL,
    rollback_step_ms INTEGER NOT NULL,
    depends_json TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    migration_json TEXT NOT NULL,
    PRIMARY KEY (package_id, service, batch_id, source_version)
);
CREATE TABLE IF NOT EXISTS package_batches (
    package_id TEXT NOT NULL,
    service TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    source_version TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    earliest_rollout_ms INTEGER NOT NULL,
    tiers_json TEXT NOT NULL,
    markers_json TEXT NOT NULL,
    criteria_json TEXT NOT NULL,
    rollback_start_ms INTEGER NOT NULL,
    rollback_end_ms INTEGER NOT NULL,
    rollback_step_ms INTEGER NOT NULL,
    depends_json TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    migration_json TEXT NOT NULL,
    PRIMARY KEY (package_id, service, batch_id)
);
CREATE TABLE IF NOT EXISTS package_pointer (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    current_package_id TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    current_tier_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS merge_reviews (
    review_id TEXT PRIMARY KEY,
    package_id TEXT NOT NULL,
    service TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_runtime (
    service TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    package_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    current_ratio REAL NOT NULL,
    current_tier_index INTEGER NOT NULL,
    completed_tiers_json TEXT NOT NULL,
    observation_results_json TEXT NOT NULL,
    pending_observation INTEGER NOT NULL,
    blockers_json TEXT NOT NULL,
    rollback_id TEXT,
    updated_clock_ms INTEGER NOT NULL,
    PRIMARY KEY (service, batch_id)
);
CREATE TABLE IF NOT EXISTS tier_ledger (
    operation_key TEXT PRIMARY KEY,
    package_id TEXT NOT NULL,
    service TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    tier_index INTEGER NOT NULL,
    target_ratio REAL NOT NULL,
    scheduled_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    effect_ms INTEGER,
    stage_after TEXT NOT NULL,
    rollback_id TEXT,
    basis_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rollback_windows (
    rollback_id TEXT PRIMARY KEY,
    package_id TEXT NOT NULL,
    service TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    abort_ms INTEGER NOT NULL,
    window_start_ms INTEGER NOT NULL,
    window_end_ms INTEGER NOT NULL,
    step_ms INTEGER NOT NULL,
    completed_json TEXT NOT NULL,
    pending_json TEXT NOT NULL,
    first_failure TEXT,
    opened_ms INTEGER NOT NULL,
    closed_ms INTEGER
);
CREATE TABLE IF NOT EXISTS observation_ledger (
    event_id TEXT PRIMARY KEY,
    logical_ms INTEGER NOT NULL,
    service TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    tier_ratio REAL NOT NULL,
    sample_count INTEGER NOT NULL,
    metrics_json TEXT NOT NULL,
    verdict TEXT NOT NULL,
    basis_json TEXT NOT NULL,
    effect_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS applied_events (
    event_id TEXT PRIMARY KEY,
    event_seq INTEGER NOT NULL,
    logical_ms INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    effect_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS clock_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    watermark_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS engine_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_json TEXT NOT NULL
);
"""

UNSTARTED_STAGES = {"PENDING", "WAITING", "BLOCKED"}


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self.conn.execute("PRAGMA journal_mode = DELETE")
        self.conn.execute("PRAGMA synchronous = FULL")
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO clock_state(singleton, watermark_ms) VALUES (1, 0)"
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema', 'grayroll-state-v1')"
        )

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def immediate(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def audit(
        self, entry: dict[str, Any], conn: sqlite3.Connection | None = None
    ) -> None:
        target = conn or self.conn
        target.execute(
            "INSERT INTO audit_log(entry_json) VALUES (?)",
            (canonical_json(entry),),
        )

    def _advance_clock_conn(self, conn: sqlite3.Connection, logical_ms: int) -> None:
        current = int(
            conn.execute(
                "SELECT watermark_ms FROM clock_state WHERE singleton = 1"
            ).fetchone()[0]
        )
        if logical_ms > current:
            conn.execute(
                "UPDATE clock_state SET watermark_ms = ? WHERE singleton = 1",
                (logical_ms,),
            )

    def get_pointer(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM package_pointer WHERE singleton = 1"
        ).fetchone()
        return dict(row) if row else None

    def current_package_id(self) -> str:
        pointer = self.get_pointer()
        if not pointer:
            raise GrayrollError("E_NO_ACTIVE_PACKAGE", "no active rhythm package")
        return str(pointer["current_package_id"])

    def get_package(self, package_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM packages WHERE package_id = ?", (package_id,)
        ).fetchone()
        if not row:
            raise GrayrollError("E_NOT_FOUND", f"package not found: {package_id}")
        return dict(row)

    def latest_staged_package(self) -> str:
        row = self.conn.execute(
            "SELECT package_id FROM packages WHERE state IN ('STAGED', 'REVIEW_REQUIRED') "
            "ORDER BY staged_seq DESC LIMIT 1"
        ).fetchone()
        if not row:
            raise GrayrollError("E_NOT_FOUND", "no staged rhythm package")
        return str(row["package_id"])

    def _content_identity(self, manifest: dict[str, Any]) -> dict[str, Any]:
        shards = sorted(
            (
                {"path": entry["path"], "sha256": entry["sha256"]}
                for entry in manifest.get("shards", [])
            ),
            key=lambda item: item["path"],
        )
        v1 = manifest.get("v1", {})
        return {
            "generation_floor": manifest.get("generation_floor"),
            "service": manifest.get("service"),
            "shards": shards,
            "tier_digest": manifest.get("tier_digest"),
            "v1_sha256": v1.get("sha256"),
        }

    def classify(self, package: LoadedPackage, *, record_rejection: bool = True) -> str:
        pointer = self.get_pointer()
        if not pointer:
            return "INITIAL"
        current = self.get_package(str(pointer["current_package_id"]))
        current_manifest = json.loads(current["manifest_json"])
        current_version = int(current["manifest_version"])
        new_identity = self._content_identity(package.manifest)
        old_identity = self._content_identity(current_manifest)
        if package.version < current_version:
            if record_rejection:
                with self.immediate() as conn:
                    self.audit(
                        {
                            "decision": "REJECTED_VERSION_ROLLBACK",
                            "new_version": package.version,
                            "current_version": current_version,
                            "package_id": package.package_id,
                        },
                        conn,
                    )
            raise GrayrollError(
                "E_VERSION_ROLLBACK",
                f"package version {package.version} is older than active version {current_version}",
                {
                    "current_version": current_version,
                    "new_version": package.version,
                    "reason": "VERSION_ROLLBACK_REJECTED",
                },
            )
        if package.version == current_version:
            if new_identity == old_identity:
                return "NOOP"
            if record_rejection:
                with self.immediate() as conn:
                    self.audit(
                        {
                            "current_version": current_version,
                            "decision": "REJECTED_SAME_VERSION_CONTENT_CONFLICT",
                            "new_version": package.version,
                            "package_id": package.package_id,
                        },
                        conn,
                    )
            raise GrayrollError(
                "E_PACKAGE_CONFLICT",
                f"package version {package.version} content differs from active version",
            )

        if new_identity == old_identity:
            return "NOOP"
        if package.generation_floor < int(current["generation_floor"]):
            if record_rejection:
                with self.immediate() as conn:
                    self.audit(
                        {
                            "current_generation_floor": current["generation_floor"],
                            "decision": "REJECTED_GENERATION_FLOOR_REGRESSION",
                            "new_generation_floor": package.generation_floor,
                            "package_id": package.package_id,
                        },
                        conn,
                    )
            raise GrayrollError(
                "E_PACKAGE_INCOMPATIBLE",
                "new package lowers the batch generation floor",
            )
        old_shards = {
            entry["path"]: entry["sha256"] for entry in old_identity["shards"]
        }
        new_shards = {
            entry["path"]: entry["sha256"] for entry in new_identity["shards"]
        }
        incremental = (
            new_identity["service"] == old_identity["service"]
            and new_identity["v1_sha256"] == old_identity["v1_sha256"]
            and all(
                new_shards.get(path) == digest for path, digest in old_shards.items()
            )
            and len(new_shards) > len(old_shards)
        )
        return "INCREMENTAL_APPEND" if incremental else "FULL_REPLACE"

    def stage_package(
        self, package: LoadedPackage, classification: str
    ) -> dict[str, Any]:
        if classification == "NOOP":
            with self.immediate() as conn:
                self.audit(
                    {
                        "decision": "NOOP",
                        "package_id": package.package_id,
                        "version": package.version,
                    },
                    conn,
                )
            return {
                "classification": "NOOP",
                "package_id": package.package_id,
                "package_version": package.version,
                "status": "NOOP",
            }
        existing = self.conn.execute(
            "SELECT * FROM packages WHERE package_id = ?", (package.package_id,)
        ).fetchone()
        if existing:
            if existing["manifest_hash"] != package.manifest_hash:
                raise GrayrollError(
                    "E_PACKAGE_CONFLICT",
                    f"package id collision with different manifest: {package.package_id}",
                )
            return {
                "classification": existing["classification"],
                "package_id": package.package_id,
                "package_version": package.version,
                "status": "ALREADY_STAGED",
            }
        with self.immediate() as conn:
            next_seq = int(
                conn.execute(
                    "SELECT COALESCE(MAX(staged_seq), 0) + 1 FROM packages"
                ).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO packages(
                    package_id, manifest_version, generation_floor, service,
                    tier_digest, manifest_hash, manifest_json, defaults_json,
                    classification, state, staged_seq
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'STAGED', ?)
                """,
                (
                    package.package_id,
                    package.version,
                    package.generation_floor,
                    package.service,
                    package.tier_digest,
                    package.manifest_hash,
                    canonical_json(package.manifest),
                    canonical_json(package.defaults),
                    classification,
                    next_seq,
                ),
            )
            for candidate in package.candidates:
                conn.execute(
                    """
                    INSERT INTO source_candidates(
                        package_id, service, batch_id, source_version,
                        source_priority, logical_clock_ms, generation,
                        earliest_rollout_ms, tiers_json, markers_json,
                        criteria_json, rollback_start_ms, rollback_end_ms,
                        rollback_step_ms, depends_json, raw_json, migration_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        package.package_id,
                        candidate.service,
                        candidate.batch_id,
                        candidate.source_version,
                        candidate.source_priority,
                        candidate.logical_clock_ms,
                        candidate.generation,
                        candidate.earliest_rollout_ms,
                        canonical_json(candidate.tiers),
                        canonical_json(candidate.observation_markers),
                        canonical_json(candidate.criteria),
                        candidate.rollback_start_ms,
                        candidate.rollback_end_ms,
                        candidate.rollback_step_ms,
                        canonical_json(candidate.depends_on),
                        canonical_json(candidate.raw),
                        canonical_json(candidate.migration),
                    ),
                )
            self.audit(
                {
                    "classification": classification,
                    "decision": "PACKAGE_STAGED",
                    "package_id": package.package_id,
                    "version": package.version,
                },
                conn,
            )
        return {
            "classification": classification,
            "package_id": package.package_id,
            "package_version": package.version,
            "status": "STAGED",
        }

    def load_package(self, package: LoadedPackage) -> dict[str, Any]:
        classification = self.classify(package)
        return self.stage_package(package, classification)

    def _read_candidates(self, package_id: str) -> tuple[CandidateBatch, ...]:
        rows = self.conn.execute(
            "SELECT * FROM source_candidates WHERE package_id = ?", (package_id,)
        ).fetchall()
        candidates = [
            CandidateBatch(
                service=row["service"],
                batch_id=row["batch_id"],
                source_version=row["source_version"],
                source_priority=int(row["source_priority"]),
                logical_clock_ms=int(row["logical_clock_ms"]),
                generation=int(row["generation"]),
                tiers=tuple(json.loads(row["tiers_json"])),
                observation_markers=tuple(json.loads(row["markers_json"])),
                earliest_rollout_ms=int(row["earliest_rollout_ms"]),
                criteria=json.loads(row["criteria_json"]),
                rollback_start_ms=int(row["rollback_start_ms"]),
                rollback_end_ms=int(row["rollback_end_ms"]),
                rollback_step_ms=int(row["rollback_step_ms"]),
                depends_on=tuple(json.loads(row["depends_json"])),
                raw=json.loads(row["raw_json"]),
                migration=json.loads(row["migration_json"]),
            )
            for row in rows
        ]
        return tuple(candidates)

    def _package_summary(
        self, package: dict[str, Any], classification: str, result: MergeResult
    ) -> dict[str, Any]:
        summary = result.summary()
        return {
            "classification": classification,
            "conflicts": [
                {
                    "batch_id": conflict.batch_id,
                    "evidence": conflict.evidence,
                    "service": conflict.service,
                }
                for conflict in result.conflicts
            ],
            "package_id": package["package_id"],
            "package_version": int(package["manifest_version"]),
            "status": "MERGED",
            **summary,
        }

    def merge_package(self, package_id: str) -> dict[str, Any]:
        package = self.get_package(package_id)
        if package["state"] == "ACTIVE":
            result = merge_candidates(self._read_candidates(package_id))
            return self._package_summary(package, package["classification"], result)
        candidates = self._read_candidates(package_id)
        result = merge_candidates(candidates)
        if result.conflicts:
            with self.immediate() as conn:
                conn.execute(
                    "UPDATE packages SET state = 'REVIEW_REQUIRED' WHERE package_id = ?",
                    (package_id,),
                )
                for conflict in result.conflicts:
                    review_id = (
                        f"review:{package_id}:{conflict.service}:{conflict.batch_id}"
                    )
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO merge_reviews(
                            review_id, package_id, service, batch_id,
                            evidence_json, status
                        ) VALUES (?, ?, ?, ?, ?, 'OPEN')
                        """,
                        (
                            review_id,
                            package_id,
                            conflict.service,
                            conflict.batch_id,
                            canonical_json(conflict.evidence),
                        ),
                    )
                self.audit(
                    {
                        "decision": "MERGE_REVIEW_REQUIRED",
                        "package_id": package_id,
                        "version": package["manifest_version"],
                    },
                    conn,
                )
            details = {
                "conflicts": [
                    {
                        "batch_id": conflict.batch_id,
                        "evidence": conflict.evidence,
                        "service": conflict.service,
                    }
                    for conflict in result.conflicts
                ],
                "package_id": package_id,
                "state": "REVIEW_REQUIRED",
            }
            raise GrayrollError(
                "E_MERGE_CONFLICT",
                "inconsistent tier sequences at equal arbitration rank; manual review required",
                details,
            )

        defaults = json.loads(package["defaults_json"])
        pointer = self.get_pointer()
        old_package_id = pointer["current_package_id"] if pointer else None
        with self.immediate() as conn:
            conn.execute(
                "DELETE FROM package_batches WHERE package_id = ?", (package_id,)
            )
            for batch in result.batches:
                conn.execute(
                    """
                    INSERT INTO package_batches(
                        package_id, service, batch_id, source_version,
                        source_priority, logical_clock_ms, generation,
                        earliest_rollout_ms, tiers_json, markers_json,
                        criteria_json, rollback_start_ms, rollback_end_ms,
                        rollback_step_ms, depends_json, raw_json,
                        decision_json, migration_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        package_id,
                        batch.service,
                        batch.batch_id,
                        batch.source_version,
                        batch.source_priority,
                        batch.logical_clock_ms,
                        batch.generation,
                        batch.earliest_rollout_ms,
                        canonical_json(batch.tiers),
                        canonical_json(batch.observation_markers),
                        canonical_json(batch.criteria),
                        batch.rollback_start_ms,
                        batch.rollback_end_ms,
                        batch.rollback_step_ms,
                        canonical_json(batch.depends_on),
                        canonical_json(batch.raw),
                        canonical_json(batch.decision),
                        canonical_json(batch.migration),
                    ),
                )
            if old_package_id:
                conn.execute(
                    "UPDATE packages SET state = 'SUPERSEDED' "
                    "WHERE package_id = ? AND package_id != ?",
                    (old_package_id, package_id),
                )
            conn.execute(
                "UPDATE packages SET state = 'ACTIVE', classification = ? "
                "WHERE package_id = ?",
                (package["classification"], package_id),
            )
            conn.execute(
                """
                INSERT INTO package_pointer(
                    singleton, current_package_id, current_version, current_tier_digest
                ) VALUES (1, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    current_package_id = excluded.current_package_id,
                    current_version = excluded.current_version,
                    current_tier_digest = excluded.current_tier_digest
                """,
                (package_id, package["manifest_version"], package["tier_digest"]),
            )
            conn.execute(
                "INSERT OR REPLACE INTO engine_meta(key, value) VALUES "
                "('defaults_json', ?)",
                (canonical_json(defaults),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO engine_meta(key, value) VALUES "
                "('clock_tolerance_ms', ?)",
                (str(defaults["clock_tolerance_ms"]),),
            )
            self._rebind_unstarted_runtimes(conn, package_id)
            self.audit(
                {
                    "classification": package["classification"],
                    "decision": "PACKAGE_ACTIVATED",
                    "package_id": package_id,
                    "previous_package_id": old_package_id,
                    "version": package["manifest_version"],
                },
                conn,
            )
        return self._package_summary(package, package["classification"], result)

    def _rebind_unstarted_runtimes(
        self, conn: sqlite3.Connection, package_id: str
    ) -> None:
        batches = conn.execute(
            "SELECT service, batch_id FROM package_batches WHERE package_id = ?",
            (package_id,),
        ).fetchall()
        available = {(row["service"], row["batch_id"]) for row in batches}
        runtimes = conn.execute("SELECT * FROM batch_runtime").fetchall()
        for runtime in runtimes:
            unstarted = (
                runtime["stage"] in UNSTARTED_STAGES
                and runtime["current_tier_index"] == -1
                and json.loads(runtime["completed_tiers_json"]) == []
                and runtime["rollback_id"] is None
            )
            key = (runtime["service"], runtime["batch_id"])
            if unstarted and key in available:
                conn.execute(
                    "UPDATE batch_runtime SET package_id = ? "
                    "WHERE service = ? AND batch_id = ?",
                    (package_id, runtime["service"], runtime["batch_id"]),
                )
            elif unstarted and key not in available:
                conn.execute(
                    "DELETE FROM batch_runtime WHERE service = ? AND batch_id = ?",
                    (runtime["service"], runtime["batch_id"]),
                )
        for batch in batches:
            conn.execute(
                """
                INSERT OR IGNORE INTO batch_runtime(
                    service, batch_id, package_id, stage, current_ratio,
                    current_tier_index, completed_tiers_json,
                    observation_results_json, pending_observation,
                    blockers_json, rollback_id, updated_clock_ms
                ) VALUES (?, ?, ?, 'PENDING', 0.0, -1, '[]', '[]', 0, '[]', NULL, 0)
                """,
                (batch["service"], batch["batch_id"], package_id),
            )

    def get_defaults(self) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT value FROM engine_meta WHERE key = 'defaults_json'"
        ).fetchone()
        if not row:
            raise GrayrollError(
                "E_NO_ACTIVE_PACKAGE", "engine defaults are not initialized"
            )
        return json.loads(row["value"])

    def get_tolerance(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM engine_meta WHERE key = 'clock_tolerance_ms'"
        ).fetchone()
        return int(row["value"]) if row else 0

    def get_clock(self) -> int:
        return int(
            self.conn.execute(
                "SELECT watermark_ms FROM clock_state WHERE singleton = 1"
            ).fetchone()[0]
        )

    def set_clock(self, logical_ms: int) -> int:
        if (
            isinstance(logical_ms, bool)
            or not isinstance(logical_ms, int)
            or logical_ms < 0
        ):
            raise GrayrollError(
                "E_BAD_EVENT", "logical clock must be a non-negative integer"
            )
        tolerance = self.get_tolerance()
        current = self.get_clock()
        if logical_ms + tolerance < current:
            raise GrayrollError(
                "E_CLOCK_REGRESSION",
                f"logical clock regression {current} -> {logical_ms} exceeds tolerance {tolerance}",
                {
                    "current_watermark_ms": current,
                    "requested_ms": logical_ms,
                    "tolerance_ms": tolerance,
                },
            )
        effective = max(current, logical_ms)
        if effective != current:
            with self.immediate() as conn:
                conn.execute(
                    "UPDATE clock_state SET watermark_ms = ? WHERE singleton = 1",
                    (effective,),
                )
        return effective

    def get_package_batch(
        self, package_id: str, service: str, batch_id: str
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM package_batches WHERE package_id = ? "
            "AND service = ? AND batch_id = ?",
            (package_id, service, batch_id),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        for key, target in (
            ("tiers_json", "tiers"),
            ("markers_json", "observation_markers"),
            ("criteria_json", "criteria"),
            ("depends_json", "depends_on"),
            ("raw_json", "raw"),
            ("decision_json", "decision"),
            ("migration_json", "migration"),
        ):
            result[target] = json.loads(result.pop(key))
        return result

    def package_batches(self, package_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT service, batch_id FROM package_batches WHERE package_id = ?",
            (package_id,),
        ).fetchall()
        pairs = sorted(
            ((row["service"], row["batch_id"]) for row in rows),
            key=lambda item: (item[0], natural_key(item[1])),
        )
        return [
            self.get_package_batch(package_id, service, batch_id)
            for service, batch_id in pairs
        ]

    def runtime_all(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM batch_runtime").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for key, target in (
                ("completed_tiers_json", "completed_tiers"),
                ("observation_results_json", "observation_results"),
                ("blockers_json", "blockers"),
            ):
                item[target] = json.loads(item.pop(key))
            result.append(item)
        result.sort(key=lambda item: (item["service"], natural_key(item["batch_id"])))
        return result

    def runtime_get(self, service: str, batch_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM batch_runtime WHERE service = ? AND batch_id = ?",
            (service, batch_id),
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        for key, target in (
            ("completed_tiers_json", "completed_tiers"),
            ("observation_results_json", "observation_results"),
            ("blockers_json", "blockers"),
        ):
            item[target] = json.loads(item.pop(key))
        return item

    def set_waiting(
        self,
        service: str,
        batch_id: str,
        stage: str,
        blockers: list[dict[str, Any]],
        logical_ms: int,
    ) -> None:
        with self.immediate() as conn:
            self._advance_clock_conn(conn, logical_ms)
            conn.execute(
                """
                UPDATE batch_runtime
                SET stage = ?, blockers_json = ?, updated_clock_ms = ?
                WHERE service = ? AND batch_id = ?
                """,
                (stage, canonical_json(blockers), logical_ms, service, batch_id),
            )

    def set_batch_stage(
        self, service: str, batch_id: str, stage: str, logical_ms: int
    ) -> None:
        with self.immediate() as conn:
            self._advance_clock_conn(conn, logical_ms)
            conn.execute(
                "UPDATE batch_runtime SET stage = ?, updated_clock_ms = ? "
                "WHERE service = ? AND batch_id = ?",
                (stage, logical_ms, service, batch_id),
            )

    def ensure_intent(
        self,
        *,
        operation_key: str,
        package_id: str,
        service: str,
        batch_id: str,
        direction: str,
        tier_index: int,
        target_ratio: float,
        scheduled_ms: int,
        stage_after: str,
        rollback_id: str | None,
        basis: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        with self.immediate() as conn:
            self._advance_clock_conn(conn, scheduled_ms)
            row = conn.execute(
                "SELECT * FROM tier_ledger WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
            if row:
                return dict(row), False
            conn.execute(
                """
                INSERT INTO tier_ledger(
                    operation_key, package_id, service, batch_id, direction,
                    tier_index, target_ratio, scheduled_ms, status,
                    effect_ms, stage_after, rollback_id, basis_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'INTENT', NULL, ?, ?, ?)
                """,
                (
                    operation_key,
                    package_id,
                    service,
                    batch_id,
                    direction,
                    tier_index,
                    target_ratio,
                    scheduled_ms,
                    stage_after,
                    rollback_id,
                    canonical_json(basis),
                ),
            )
        row = self.conn.execute(
            "SELECT * FROM tier_ledger WHERE operation_key = ?",
            (operation_key,),
        ).fetchone()
        return dict(row), True

    def ledger_row(self, operation_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM tier_ledger WHERE operation_key = ?", (operation_key,)
        ).fetchone()
        return dict(row) if row else None

    def ledger_intents(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM tier_ledger WHERE status = 'INTENT' ORDER BY operation_key"
        ).fetchall()
        return [dict(row) for row in rows]

    def commit_ramp_intent(self, operation_key: str, effect_ms: int) -> None:
        with self.immediate() as conn:
            self._advance_clock_conn(conn, effect_ms)
            intent = conn.execute(
                "SELECT * FROM tier_ledger WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
            if not intent or intent["status"] == "COMMITTED":
                return
            if intent["status"] != "INTENT":
                return
            runtime = conn.execute(
                "SELECT * FROM batch_runtime WHERE service = ? AND batch_id = ?",
                (intent["service"], intent["batch_id"]),
            ).fetchone()
            if not runtime:
                raise GrayrollError(
                    "E_INVALID_STATE", "ramp intent references missing batch runtime"
                )
            completed = json.loads(runtime["completed_tiers_json"])
            target = float(intent["target_ratio"])
            if not any(abs(float(item) - target) < 1e-12 for item in completed):
                completed.append(target)
            stage_after = intent["stage_after"]
            conn.execute(
                """
                UPDATE batch_runtime SET
                    current_ratio = ?, current_tier_index = ?,
                    completed_tiers_json = ?, stage = ?,
                    pending_observation = ?, blockers_json = '[]',
                    updated_clock_ms = ?
                WHERE service = ? AND batch_id = ?
                """,
                (
                    target,
                    int(intent["tier_index"]),
                    canonical_json(completed),
                    stage_after,
                    1 if stage_after == "OBSERVING" else 0,
                    effect_ms,
                    intent["service"],
                    intent["batch_id"],
                ),
            )
            conn.execute(
                "UPDATE tier_ledger SET status = 'COMMITTED', effect_ms = ? "
                "WHERE operation_key = ?",
                (effect_ms, operation_key),
            )

    def fail_rollback(
        self,
        rollback_id: str,
        *,
        logical_ms: int,
        first_failure: str,
    ) -> None:
        with self.immediate() as conn:
            self._advance_clock_conn(conn, logical_ms)
            rollback = conn.execute(
                "SELECT * FROM rollback_windows WHERE rollback_id = ?",
                (rollback_id,),
            ).fetchone()
            if not rollback or rollback["status"] == "SUCCEEDED":
                return
            if rollback["status"] == "WINDOW_FAILED":
                return
            conn.execute(
                "UPDATE rollback_windows SET status = 'WINDOW_FAILED', "
                "first_failure = ?, closed_ms = ? WHERE rollback_id = ?",
                (first_failure, logical_ms, rollback_id),
            )
            conn.execute(
                "UPDATE batch_runtime SET stage = 'ROLLBACK_FAILED', "
                "updated_clock_ms = ? WHERE service = ? AND batch_id = ?",
                (logical_ms, rollback["service"], rollback["batch_id"]),
            )
            conn.execute(
                "UPDATE tier_ledger SET status = 'FAILED', effect_ms = ? "
                "WHERE rollback_id = ? AND status = 'INTENT'",
                (logical_ms, rollback_id),
            )

    def commit_rollback_intent(
        self, operation_key: str, *, recovery_ms: int
    ) -> dict[str, Any]:
        with self.immediate() as conn:
            self._advance_clock_conn(conn, recovery_ms)
            intent = conn.execute(
                "SELECT * FROM tier_ledger WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
            if not intent or intent["status"] == "COMMITTED":
                return {"applied": False, "status": "COMMITTED"}
            if intent["status"] != "INTENT":
                return {"applied": False, "status": intent["status"]}
            rollback = conn.execute(
                "SELECT * FROM rollback_windows WHERE rollback_id = ?",
                (intent["rollback_id"],),
            ).fetchone()
            if not rollback:
                raise GrayrollError(
                    "E_INVALID_STATE", "rollback intent references missing window"
                )
            if rollback["status"] != "OPEN":
                conn.execute(
                    "UPDATE tier_ledger SET status = 'FAILED', effect_ms = ? "
                    "WHERE operation_key = ?",
                    (recovery_ms, operation_key),
                )
                return {"applied": False, "status": rollback["status"]}
            if recovery_ms > int(rollback["window_end_ms"]):
                conn.execute(
                    "UPDATE rollback_windows SET status = 'WINDOW_FAILED', "
                    "first_failure = 'RECOVERY_AFTER_WINDOW', closed_ms = ? "
                    "WHERE rollback_id = ?",
                    (rollback["window_end_ms"], rollback["rollback_id"]),
                )
                conn.execute(
                    "UPDATE tier_ledger SET status = 'FAILED', effect_ms = ? "
                    "WHERE rollback_id = ? AND status = 'INTENT'",
                    (rollback["window_end_ms"], rollback["rollback_id"]),
                )
                conn.execute(
                    "UPDATE batch_runtime SET stage = 'ROLLBACK_FAILED', "
                    "updated_clock_ms = ? WHERE service = ? AND batch_id = ?",
                    (
                        rollback["window_end_ms"],
                        rollback["service"],
                        rollback["batch_id"],
                    ),
                )
                return {"applied": False, "status": "WINDOW_FAILED"}
            effect_ms = int(intent["scheduled_ms"])
            target = float(intent["target_ratio"])
            completed = json.loads(rollback["completed_json"])
            pending = json.loads(rollback["pending_json"])
            if pending:
                pending.pop(0)
            if not any(abs(float(item) - target) < 1e-12 for item in completed):
                completed.append(target)
            status = "SUCCEEDED" if not pending else "OPEN"
            stage = "ROLLED_BACK" if status == "SUCCEEDED" else "ROLLING_BACK"
            conn.execute(
                "UPDATE rollback_windows SET completed_json = ?, pending_json = ?, "
                "status = ?, closed_ms = ? WHERE rollback_id = ?",
                (
                    canonical_json(completed),
                    canonical_json(pending),
                    status,
                    effect_ms if status == "SUCCEEDED" else None,
                    rollback["rollback_id"],
                ),
            )
            conn.execute(
                "UPDATE batch_runtime SET current_ratio = ?, stage = ?, "
                "updated_clock_ms = ? WHERE service = ? AND batch_id = ?",
                (
                    target,
                    stage,
                    effect_ms,
                    rollback["service"],
                    rollback["batch_id"],
                ),
            )
            conn.execute(
                "UPDATE tier_ledger SET status = 'COMMITTED', effect_ms = ? "
                "WHERE operation_key = ?",
                (effect_ms, operation_key),
            )
            return {"applied": True, "status": status}

    def _insert_rollback(
        self,
        conn: sqlite3.Connection,
        *,
        rollback_id: str,
        package_id: str,
        service: str,
        batch_id: str,
        reason: str,
        abort_ms: int,
        start_ms: int,
        end_ms: int,
        step_ms: int,
        targets: list[float],
    ) -> str:
        if abort_ms > end_ms:
            status = "WINDOW_FAILED"
            first_failure = "ABORT_AFTER_ROLLBACK_WINDOW"
            stage = "ROLLBACK_FAILED"
            closed_ms: int | None = abort_ms
        else:
            status = "OPEN"
            first_failure = None
            stage = "ROLLING_BACK"
            closed_ms = None
        conn.execute(
            """
            INSERT INTO rollback_windows(
                rollback_id, package_id, service, batch_id, status, reason,
                abort_ms, window_start_ms, window_end_ms, step_ms,
                completed_json, pending_json, first_failure, opened_ms, closed_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?)
            """,
            (
                rollback_id,
                package_id,
                service,
                batch_id,
                status,
                reason,
                abort_ms,
                start_ms,
                end_ms,
                step_ms,
                canonical_json(targets),
                first_failure,
                abort_ms,
                closed_ms,
            ),
        )
        conn.execute(
            "UPDATE batch_runtime SET stage = ?, rollback_id = ?, "
            "updated_clock_ms = ? WHERE service = ? AND batch_id = ?",
            (stage, rollback_id, abort_ms, service, batch_id),
        )
        return status

    def _event_already_applied(
        self, conn: sqlite3.Connection, event: dict[str, Any]
    ) -> bool:
        row = conn.execute(
            "SELECT payload_json FROM applied_events WHERE event_id = ?",
            (event["event_id"],),
        ).fetchone()
        if not row:
            return False
        if row["payload_json"] != canonical_json(event):
            raise GrayrollError(
                "E_BAD_EVENT",
                f"event_id {event['event_id']} was replayed with different content",
            )
        return True

    def _mark_event(
        self,
        conn: sqlite3.Connection,
        event: dict[str, Any],
        *,
        event_seq: int,
        effect_ms: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO applied_events(
                event_id, event_seq, logical_ms, kind, payload_json, effect_ms
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event["event_id"],
                event_seq,
                event["logical_ms"],
                event["type"],
                canonical_json(event),
                effect_ms,
            ),
        )

    def apply_snapshot(
        self,
        *,
        event: dict[str, Any],
        event_seq: int,
        effect_ms: int,
        verdict: str,
        basis: dict[str, Any],
        stage_after: str,
        rollback_spec: dict[str, Any] | None,
    ) -> bool:
        with self.immediate() as conn:
            self._advance_clock_conn(conn, effect_ms)
            if self._event_already_applied(conn, event):
                return False
            runtime = conn.execute(
                "SELECT * FROM batch_runtime WHERE service = ? AND batch_id = ?",
                (event["service"], event["batch_id"]),
            ).fetchone()
            if not runtime:
                raise GrayrollError(
                    "E_EVENT_BATCH_STATE", "snapshot references missing batch runtime"
                )
            results = json.loads(runtime["observation_results_json"])
            results.append(
                {
                    "event_id": event["event_id"],
                    "logical_ms": event["logical_ms"],
                    "tier_ratio": event["tier_ratio"],
                    "verdict": verdict,
                }
            )
            conn.execute(
                """
                UPDATE batch_runtime SET observation_results_json = ?,
                    stage = ?, pending_observation = ?, updated_clock_ms = ?
                WHERE service = ? AND batch_id = ?
                """,
                (
                    canonical_json(results),
                    stage_after,
                    1 if stage_after == "OBSERVING" else 0,
                    effect_ms,
                    event["service"],
                    event["batch_id"],
                ),
            )
            conn.execute(
                """
                INSERT INTO observation_ledger(
                    event_id, logical_ms, service, batch_id, tier_ratio,
                    sample_count, metrics_json, verdict, basis_json, effect_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_id"],
                    event["logical_ms"],
                    event["service"],
                    event["batch_id"],
                    event["tier_ratio"],
                    event["sample_count"],
                    canonical_json(event["metrics"]),
                    verdict,
                    canonical_json(basis),
                    effect_ms,
                ),
            )
            self._mark_event(conn, event, event_seq=event_seq, effect_ms=effect_ms)
            if rollback_spec:
                self._insert_rollback(conn, **rollback_spec)
            return True

    def apply_manual_abort(
        self,
        *,
        event: dict[str, Any],
        event_seq: int,
        effect_ms: int,
        rollback_spec: dict[str, Any],
    ) -> bool:
        with self.immediate() as conn:
            self._advance_clock_conn(conn, effect_ms)
            if self._event_already_applied(conn, event):
                return False
            runtime = conn.execute(
                "SELECT * FROM batch_runtime WHERE service = ? AND batch_id = ?",
                (event["service"], event["batch_id"]),
            ).fetchone()
            if not runtime:
                raise GrayrollError(
                    "E_EVENT_BATCH_STATE",
                    "manual abort references missing batch runtime",
                )
            self._mark_event(conn, event, event_seq=event_seq, effect_ms=effect_ms)
            self._insert_rollback(conn, **rollback_spec)
            return True

    def get_rollback(self, rollback_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM rollback_windows WHERE rollback_id = ?", (rollback_id,)
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["completed_targets"] = json.loads(item.pop("completed_json"))
        item["pending_targets"] = json.loads(item.pop("pending_json"))
        return item

    def event_is_applied(self, event: dict[str, Any]) -> bool:
        return self._event_already_applied(self.conn, event)

    def open_rollbacks(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT rollback_id FROM rollback_windows WHERE status = 'OPEN' "
            "ORDER BY rollback_id"
        ).fetchall()
        return [self.get_rollback(row["rollback_id"]) for row in rows]

    def rollback_ledger(self, rollback_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM tier_ledger WHERE rollback_id = ? "
            "ORDER BY tier_index, operation_key",
            (rollback_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def status(self) -> dict[str, Any]:
        pointer = self.get_pointer()
        batches = []
        for runtime in self.runtime_all():
            view = self.get_package_batch(
                runtime["package_id"], runtime["service"], runtime["batch_id"]
            )
            if view is None:
                raise GrayrollError(
                    "E_INVALID_STATE",
                    f"runtime batch has no bound package view: "
                    f"{runtime['service']}/{runtime['batch_id']}",
                )
            package = self.get_package(runtime["package_id"])
            rollback = (
                self.get_rollback(runtime["rollback_id"])
                if runtime["rollback_id"]
                else None
            )
            rollback_public = None
            if rollback:
                rollback_public = {
                    "abort_ms": rollback["abort_ms"],
                    "closed_ms": rollback["closed_ms"],
                    "completed_targets": rollback["completed_targets"],
                    "first_failure": rollback["first_failure"],
                    "opened_ms": rollback["opened_ms"],
                    "pending_targets": rollback["pending_targets"],
                    "reason": rollback["reason"],
                    "rollback_id": rollback["rollback_id"],
                    "status": rollback["status"],
                    "steps": [
                        {
                            "basis": json.loads(step["basis_json"]),
                            "effect_ms": step["effect_ms"],
                            "scheduled_ms": step["scheduled_ms"],
                            "status": step["status"],
                            "target_ratio": step["target_ratio"],
                            "tier_index": step["tier_index"],
                        }
                        for step in self.rollback_ledger(rollback["rollback_id"])
                    ],
                    "window": {
                        "end_ms": rollback["window_end_ms"],
                        "start_ms": rollback["window_start_ms"],
                        "step_ms": rollback["step_ms"],
                    },
                }
            batches.append(
                {
                    "batch_id": runtime["batch_id"],
                    "blockers": runtime["blockers"],
                    "bound_package_id": runtime["package_id"],
                    "bound_package_version": int(package["manifest_version"]),
                    "completed_tiers": runtime["completed_tiers"],
                    "current_ratio": runtime["current_ratio"],
                    "depends_on": view["depends_on"],
                    "earliest_rollout_ms": view["earliest_rollout_ms"],
                    "generation": view["generation"],
                    "logical_clock_ms": view["logical_clock_ms"],
                    "observation_results": runtime["observation_results"],
                    "rollback": rollback_public,
                    "service": runtime["service"],
                    "source_version": view["source_version"],
                    "stage": runtime["stage"],
                    "tiers": view["tiers"],
                }
            )
        reviews = [
            {
                "batch_id": row["batch_id"],
                "evidence": json.loads(row["evidence_json"]),
                "package_id": row["package_id"],
                "review_id": row["review_id"],
                "service": row["service"],
                "status": row["status"],
            }
            for row in self.conn.execute(
                "SELECT * FROM merge_reviews ORDER BY review_id"
            )
        ]
        pending_packages = [
            {
                "classification": row["classification"],
                "package_id": row["package_id"],
                "state": row["state"],
                "version": row["manifest_version"],
            }
            for row in self.conn.execute(
                "SELECT * FROM packages WHERE state IN ('STAGED', 'REVIEW_REQUIRED') "
                "ORDER BY staged_seq"
            )
        ]
        result = {
            "batches": batches,
            "clock": {"logical_watermark_ms": self.get_clock()},
            "current_package_id": pointer["current_package_id"] if pointer else None,
            "manual_review": bool(reviews),
            "pending_packages": pending_packages,
            "reviews": reviews,
            "rhythm_version": int(pointer["current_version"]) if pointer else None,
            "schema": "grayroll-status-v1",
        }
        return result

    def export_ledger(self) -> dict[str, Any]:
        applied_events = [
            {
                "effect_ms": row["effect_ms"],
                "event_id": row["event_id"],
                "event_seq": row["event_seq"],
                "kind": row["kind"],
                "logical_ms": row["logical_ms"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in self.conn.execute(
                "SELECT * FROM applied_events ORDER BY event_seq, event_id"
            )
        ]
        tiers = [
            {
                "basis": json.loads(row["basis_json"]),
                "batch_id": row["batch_id"],
                "direction": row["direction"],
                "effect_ms": row["effect_ms"],
                "operation_key": row["operation_key"],
                "package_id": row["package_id"],
                "rollback_id": row["rollback_id"],
                "scheduled_ms": row["scheduled_ms"],
                "service": row["service"],
                "stage_after": row["stage_after"],
                "status": row["status"],
                "target_ratio": row["target_ratio"],
                "tier_index": row["tier_index"],
            }
            for row in self.conn.execute(
                "SELECT * FROM tier_ledger ORDER BY operation_key"
            )
        ]
        observations = [
            {
                "batch_id": row["batch_id"],
                "effect_ms": row["effect_ms"],
                "event_id": row["event_id"],
                "logical_ms": row["logical_ms"],
                "metrics": json.loads(row["metrics_json"]),
                "sample_count": row["sample_count"],
                "service": row["service"],
                "tier_ratio": row["tier_ratio"],
                "verdict": row["verdict"],
            }
            for row in self.conn.execute(
                "SELECT * FROM observation_ledger ORDER BY logical_ms, event_id"
            )
        ]
        rollbacks = [
            {
                "abort_ms": row["abort_ms"],
                "batch_id": row["batch_id"],
                "closed_ms": row["closed_ms"],
                "completed_targets": json.loads(row["completed_json"]),
                "first_failure": row["first_failure"],
                "opened_ms": row["opened_ms"],
                "pending_targets": json.loads(row["pending_json"]),
                "reason": row["reason"],
                "rollback_id": row["rollback_id"],
                "service": row["service"],
                "status": row["status"],
                "window": {
                    "end_ms": row["window_end_ms"],
                    "start_ms": row["window_start_ms"],
                    "step_ms": row["step_ms"],
                },
            }
            for row in self.conn.execute(
                "SELECT * FROM rollback_windows ORDER BY rollback_id"
            )
        ]
        return {
            "applied_events": applied_events,
            "observations": observations,
            "rollbacks": rollbacks,
            "tiers": tiers,
        }
