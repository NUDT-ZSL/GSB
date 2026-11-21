from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .errors import (
    DeployError,
    E_INCONSISTENT,
    E_MANIFEST_CHANGED_MID_RUN,
    E_PARTIAL_STAGE,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    E_STAGE_SEQUENCE,
    STAGES,
)
from .util import canonical_json, digest_bytes, digest_json


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SnapshotStore:
    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 0,
        error_busy_timeout_ms: int = 1500,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.error_busy_timeout_ms = int(error_busy_timeout_ms)
        self._init_schema()

    def _connect(self, *, busy_timeout_ms: int | None = None) -> sqlite3.Connection:
        timeout = self.busy_timeout_ms if busy_timeout_ms is None else busy_timeout_ms
        conn = sqlite3.connect(
            self.path,
            timeout=max(0, timeout) / 1000.0,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={max(0, int(timeout))}")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS generations (
                    generation INTEGER PRIMARY KEY,
                    manifest_digest TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    calibration_config_json TEXT NOT NULL,
                    invalidation_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    generation INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    version INTEGER NOT NULL UNIQUE,
                    completed_layers_json TEXT NOT NULL,
                    artifact_digests_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    content_checksum TEXT NOT NULL,
                    prev_version INTEGER,
                    prev_checksum TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(generation, stage),
                    FOREIGN KEY(generation) REFERENCES generations(generation)
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stage TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(stage, cache_key)
                );
                CREATE TABLE IF NOT EXISTS snapshot_artifacts (
                    snapshot_id INTEGER NOT NULL,
                    layer_id TEXT NOT NULL,
                    artifact_id INTEGER NOT NULL,
                    disposition TEXT NOT NULL,
                    PRIMARY KEY(snapshot_id, layer_id),
                    FOREIGN KEY(snapshot_id) REFERENCES snapshots(id) ON DELETE RESTRICT,
                    FOREIGN KEY(artifact_id) REFERENCES artifacts(id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    code TEXT NOT NULL,
                    layer_id TEXT,
                    field TEXT,
                    message TEXT NOT NULL,
                    injected INTEGER NOT NULL,
                    attributed INTEGER NOT NULL,
                    details_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_snapshots_generation
                    ON snapshots(generation, version);
                CREATE INDEX IF NOT EXISTS idx_errors_created
                    ON errors(id DESC);
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('unattributed_count', '0')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('latest_version', '0')"
            )
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def writer_guard(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower():
                    raise DeployError(
                        E_STAGE_BUSY,
                        "model_validation",
                        "another stage commit currently holds the write lock",
                    ) from exc
                raise
            try:
                yield conn
            finally:
                if conn.in_transaction:
                    conn.rollback()
        finally:
            conn.close()

    @staticmethod
    def _manifest_layers(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {layer["layer_id"]: layer for layer in manifest["layers"]}

    def _generation_row_tx(
        self, conn: sqlite3.Connection, generation: int
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM generations WHERE generation=?", (generation,)
        ).fetchone()

    def _ensure_generation_tx(
        self,
        conn: sqlite3.Connection,
        *,
        generation: int,
        stage: str,
        manifest: dict[str, Any],
        calibration_config: dict[str, Any],
        invalidation: dict[str, Any],
    ) -> sqlite3.Row:
        latest = conn.execute(
            "SELECT MAX(generation) AS generation FROM generations"
        ).fetchone()
        latest_generation = int(latest["generation"] or 0)
        existing = self._generation_row_tx(conn, generation)
        if generation < latest_generation:
            raise DeployError(
                E_SNAPSHOT_ROLLBACK,
                stage,
                f"generation {generation} is older than committed generation "
                f"{latest_generation}",
                details={
                    "requested_generation": generation,
                    "latest_generation": latest_generation,
                },
            )
        if existing is None:
            if generation != latest_generation + 1:
                raise DeployError(
                    E_STAGE_SEQUENCE,
                    stage,
                    f"new generation must be {latest_generation + 1}, got {generation}",
                )
            if latest_generation:
                previous = self._generation_row_tx(conn, latest_generation)
                assert previous is not None
                previous_manifest = json.loads(previous["manifest_json"])
                previous_calibration = json.loads(previous["calibration_config_json"])
                old_layers = self._manifest_layers(previous_manifest)
                new_layers = self._manifest_layers(manifest)
                if set(old_layers) != set(new_layers):
                    raise DeployError(
                        E_SNAPSHOT_ROLLBACK,
                        stage,
                        "layer lineage changed; layer additions/removals require a "
                        "new store",
                        details={
                            "old_layers": sorted(old_layers),
                            "new_layers": sorted(new_layers),
                        },
                    )
                for layer_id in sorted(new_layers):
                    old_revision = int(old_layers[layer_id]["weight_revision"])
                    new_revision = int(new_layers[layer_id]["weight_revision"])
                    if new_revision < old_revision:
                        raise DeployError(
                            E_SNAPSHOT_ROLLBACK,
                            stage,
                            f"weight_revision rollback for {layer_id}: "
                            f"{old_revision} -> {new_revision}",
                            layer_id=layer_id,
                            field="weight_revision",
                        )
                old_calib = previous_calibration.get("revisions", {})
                new_calib = calibration_config.get("revisions", {})
                for layer_id in sorted(new_layers):
                    old_revision = int(old_calib.get(layer_id, 0))
                    new_revision = int(new_calib.get(layer_id, 0))
                    if new_revision < old_revision:
                        raise DeployError(
                            E_SNAPSHOT_ROLLBACK,
                            stage,
                            f"calibration revision rollback for {layer_id}: "
                            f"{old_revision} -> {new_revision}",
                            layer_id=layer_id,
                            field="calibration_revision",
                        )
            conn.execute(
                """
                INSERT INTO generations(
                    generation, manifest_digest, manifest_json,
                    calibration_config_json, invalidation_json, created_at, completed
                ) VALUES (?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    generation,
                    manifest["manifest_digest"],
                    canonical_json(manifest),
                    canonical_json(calibration_config),
                    canonical_json(invalidation),
                    utc_now(),
                ),
            )
            existing = self._generation_row_tx(conn, generation)
        else:
            if existing["manifest_digest"] != manifest["manifest_digest"]:
                raise DeployError(
                    E_MANIFEST_CHANGED_MID_RUN,
                    stage,
                    "manifest changed while an incomplete generation exists",
                    details={
                        "committed_manifest_digest": existing["manifest_digest"],
                        "requested_manifest_digest": manifest["manifest_digest"],
                    },
                )
            if existing["calibration_config_json"] != canonical_json(
                calibration_config
            ):
                raise DeployError(
                    E_MANIFEST_CHANGED_MID_RUN,
                    stage,
                    "calibration configuration changed while an incomplete "
                    "generation exists",
                )
        assert existing is not None
        return existing

    def commit_stage(
        self,
        *,
        generation: int,
        stage: str,
        completed_layers: list[str],
        artifacts: list[dict[str, Any]],
        metadata: dict[str, Any],
        manifest: dict[str, Any],
        calibration_config: dict[str, Any],
        invalidation: dict[str, Any],
    ) -> dict[str, Any]:
        if stage not in STAGES:
            raise ValueError(f"unknown stage: {stage}")
        manifest_layer_ids = sorted(self._manifest_layers(manifest))
        if sorted(completed_layers) != manifest_layer_ids:
            raise DeployError(
                E_PARTIAL_STAGE,
                stage,
                "stage commit rejected because not all layers completed",
                details={
                    "completed": sorted(completed_layers),
                    "required": manifest_layer_ids,
                },
            )
        if {item["layer_id"] for item in artifacts} != set(manifest_layer_ids):
            raise DeployError(
                E_PARTIAL_STAGE,
                stage,
                "stage commit rejected because artifact set is incomplete",
            )
        conn = self._connect()
        try:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower():
                    raise DeployError(
                        E_STAGE_BUSY,
                        stage,
                        "another stage commit currently holds the write lock",
                    ) from exc
                raise
            generation_row = self._ensure_generation_tx(
                conn,
                generation=generation,
                stage=stage,
                manifest=manifest,
                calibration_config=calibration_config,
                invalidation=invalidation,
            )
            rows = conn.execute(
                "SELECT stage, version FROM snapshots WHERE generation=? "
                "ORDER BY version",
                (generation,),
            ).fetchall()
            committed_stages = [row["stage"] for row in rows]
            for existing_stage in committed_stages:
                if existing_stage == stage:
                    raise DeployError(
                        E_SNAPSHOT_ROLLBACK,
                        stage,
                        f"stage {stage} is already committed for generation "
                        f"{generation}; overwrite is forbidden",
                    )
            expected_stage = STAGES[len(committed_stages)]
            if stage != expected_stage:
                raise DeployError(
                    E_STAGE_SEQUENCE,
                    stage,
                    f"expected next stage {expected_stage}, got {stage}",
                    details={"committed_stages": committed_stages},
                )
            latest_row = conn.execute(
                "SELECT version, content_checksum FROM snapshots "
                "ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if latest_row is None:
                version = 1
                prev_version = None
                prev_checksum = None
            else:
                version = int(latest_row["version"]) + 1
                prev_version = int(latest_row["version"])
                prev_checksum = str(latest_row["content_checksum"])
            artifact_records: dict[str, dict[str, Any]] = {}
            artifact_links: list[tuple[str, int, str]] = []
            for item in sorted(artifacts, key=lambda value: value["layer_id"]):
                payload = item["payload"]
                if not isinstance(payload, str):
                    raise ValueError("artifact payload must be canonical JSON text")
                digest = digest_bytes(payload.encode("utf-8"))
                if item.get("digest") and item["digest"] != digest:
                    raise DeployError(
                        E_INCONSISTENT,
                        stage,
                        f"artifact digest mismatch for {item['layer_id']}",
                        layer_id=item["layer_id"],
                    )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO artifacts(
                        stage, cache_key, digest, payload, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        stage,
                        item["cache_key"],
                        digest,
                        payload,
                        utc_now(),
                    ),
                )
                artifact_row = conn.execute(
                    "SELECT id, digest FROM artifacts WHERE stage=? AND cache_key=?",
                    (stage, item["cache_key"]),
                ).fetchone()
                if artifact_row is None or artifact_row["digest"] != digest:
                    raise DeployError(
                        E_INCONSISTENT,
                        stage,
                        f"content-addressed artifact collision for {item['layer_id']}",
                        layer_id=item["layer_id"],
                    )
                artifact_records[item["layer_id"]] = {
                    "digest": digest,
                    "cache_key": item["cache_key"],
                    "disposition": item["disposition"],
                }
                artifact_links.append(
                    (item["layer_id"], int(artifact_row["id"]), item["disposition"])
                )
            body = {
                "generation": generation,
                "stage": stage,
                "version": version,
                "prev_version": prev_version,
                "prev_checksum": prev_checksum,
                "completed_layers": sorted(completed_layers),
                "artifacts": artifact_records,
                "metadata": metadata,
            }
            checksum = digest_json(body)
            cursor = conn.execute(
                """
                INSERT INTO snapshots(
                    generation, stage, version, completed_layers_json,
                    artifact_digests_json, metadata_json, content_checksum,
                    prev_version, prev_checksum, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generation,
                    stage,
                    version,
                    canonical_json(sorted(completed_layers)),
                    canonical_json(artifact_records),
                    canonical_json(metadata),
                    checksum,
                    prev_version,
                    prev_checksum,
                    utc_now(),
                ),
            )
            snapshot_id = int(cursor.lastrowid)
            conn.executemany(
                """
                INSERT INTO snapshot_artifacts(
                    snapshot_id, layer_id, artifact_id, disposition
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (snapshot_id, layer_id, artifact_id, disposition)
                    for layer_id, artifact_id, disposition in artifact_links
                ],
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('latest_version', ?)",
                (str(version),),
            )
            if stage == STAGES[-1]:
                conn.execute(
                    "UPDATE generations SET completed=1 WHERE generation=?",
                    (generation,),
                )
            conn.commit()
            return {
                "snapshot_id": snapshot_id,
                "generation": generation,
                "stage": stage,
                "version": version,
                "content_checksum": checksum,
                "completed_layers": sorted(completed_layers),
                "prev_version": prev_version,
                "prev_checksum": prev_checksum,
                "generation_completed": stage == STAGES[-1],
            }
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def latest_generation(self) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM generations ORDER BY generation DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return self._decode_generation(row)

    def get_generation(self, generation: int) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM generations WHERE generation=?", (generation,)
            ).fetchone()
        finally:
            conn.close()
        return self._decode_generation(row) if row else None

    @staticmethod
    def _decode_generation(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "generation": int(row["generation"]),
            "manifest_digest": row["manifest_digest"],
            "manifest": json.loads(row["manifest_json"]),
            "calibration_config": json.loads(row["calibration_config_json"]),
            "invalidation": json.loads(row["invalidation_json"]),
            "created_at": row["created_at"],
            "completed": bool(row["completed"]),
        }

    def generation_snapshots(self, generation: int) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM snapshots WHERE generation=? ORDER BY version",
                (generation,),
            ).fetchall()
        finally:
            conn.close()
        return [self._decode_snapshot(row) for row in rows]

    def latest_snapshot(
        self, *, generation: int | None = None, stage: str | None = None
    ) -> dict[str, Any] | None:
        clauses: list[str] = []
        params: list[Any] = []
        if generation is not None:
            clauses.append("generation=?")
            params.append(generation)
        if stage is not None:
            clauses.append("stage=?")
            params.append(stage)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM snapshots" + where + " ORDER BY version DESC LIMIT 1",
                params,
            ).fetchone()
        finally:
            conn.close()
        return self._decode_snapshot(row) if row else None

    @staticmethod
    def _decode_snapshot(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "generation": int(row["generation"]),
            "stage": row["stage"],
            "version": int(row["version"]),
            "completed_layers": json.loads(row["completed_layers_json"]),
            "artifacts": json.loads(row["artifact_digests_json"]),
            "metadata": json.loads(row["metadata_json"]),
            "content_checksum": row["content_checksum"],
            "prev_version": row["prev_version"],
            "prev_checksum": row["prev_checksum"],
            "created_at": row["created_at"],
        }

    def snapshot_artifacts(self, snapshot_id: int) -> dict[str, dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT sa.layer_id, sa.disposition, a.stage, a.cache_key,
                       a.digest, a.payload
                FROM snapshot_artifacts sa
                JOIN artifacts a ON a.id=sa.artifact_id
                WHERE sa.snapshot_id=?
                ORDER BY sa.layer_id
                """,
                (snapshot_id,),
            ).fetchall()
        finally:
            conn.close()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            payload_text = row["payload"]
            if digest_bytes(payload_text.encode("utf-8")) != row["digest"]:
                raise DeployError(
                    E_INCONSISTENT,
                    row["stage"],
                    f"stored artifact payload digest mismatch for {row['layer_id']}",
                    layer_id=row["layer_id"],
                )
            result[row["layer_id"]] = {
                "layer_id": row["layer_id"],
                "stage": row["stage"],
                "cache_key": row["cache_key"],
                "digest": row["digest"],
                "disposition": row["disposition"],
                "payload": json.loads(payload_text),
                "payload_text": payload_text,
            }
        return result

    def load_stage_artifacts(
        self, generation: int, stage: str
    ) -> dict[str, dict[str, Any]]:
        snapshot = self.latest_snapshot(generation=generation, stage=stage)
        if snapshot is None:
            return {}
        return self.snapshot_artifacts(snapshot["id"])

    def find_cached_artifact(
        self, stage: str, cache_key: str
    ) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE stage=? AND cache_key=?",
                (stage, cache_key),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        payload_text = row["payload"]
        if digest_bytes(payload_text.encode("utf-8")) != row["digest"]:
            raise DeployError(
                E_INCONSISTENT,
                stage,
                f"cache artifact digest mismatch for {cache_key}",
            )
        return {
            "stage": row["stage"],
            "cache_key": row["cache_key"],
            "digest": row["digest"],
            "payload": json.loads(payload_text),
            "payload_text": payload_text,
        }

    def record_error(
        self,
        error: DeployError,
        *,
        attributed: bool = True,
    ) -> dict[str, Any]:
        conn = self._connect(busy_timeout_ms=self.error_busy_timeout_ms)
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                INSERT INTO errors(
                    created_at, stage, code, layer_id, field, message,
                    injected, attributed, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    error.stage,
                    error.code,
                    error.layer_id,
                    error.field,
                    error.message,
                    1 if error.injected else 0,
                    1 if attributed else 0,
                    canonical_json(error.details),
                ),
            )
            if not attributed:
                conn.execute(
                    "UPDATE meta SET value=CAST(value AS INTEGER)+1 "
                    "WHERE key='unattributed_count'"
                )
            conn.commit()
            return {
                "id": int(cursor.lastrowid),
                "created_at": utc_now(),
                **error.to_record(),
                "attributed": attributed,
            }
        finally:
            conn.close()

    def recent_errors(self, limit: int = 20) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM errors ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "id": int(row["id"]),
                "created_at": row["created_at"],
                "stage": row["stage"],
                "code": row["code"],
                "layer_id": row["layer_id"],
                "field": row["field"],
                "message": row["message"],
                "injected": bool(row["injected"]),
                "attributed": bool(row["attributed"]),
                "details": json.loads(row["details_json"]),
            }
            for row in rows
        ]

    def unattributed_count(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='unattributed_count'"
            ).fetchone()
        finally:
            conn.close()
        return int(row["value"]) if row else 0

    def latest_version(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='latest_version'"
            ).fetchone()
            count = conn.execute("SELECT COUNT(*) AS count FROM snapshots").fetchone()
        finally:
            conn.close()
        if row and count and int(count["count"]) > 0:
            return int(row["value"])
        return 0

    def trace(self, *, generation: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            if generation is None:
                rows = conn.execute(
                    """
                    SELECT s.*,
                           (SELECT COUNT(*) FROM snapshot_artifacts sa
                            WHERE sa.snapshot_id=s.id) AS artifact_count
                    FROM snapshots s
                    ORDER BY s.version DESC LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT s.*,
                           (SELECT COUNT(*) FROM snapshot_artifacts sa
                            WHERE sa.snapshot_id=s.id) AS artifact_count
                    FROM snapshots s
                    WHERE s.generation=?
                    ORDER BY s.version DESC LIMIT ?
                    """,
                    (int(generation), int(limit)),
                ).fetchall()
        finally:
            conn.close()
        return [
            {
                **self._decode_snapshot(row),
                "artifact_count": int(row["artifact_count"]),
            }
            for row in rows
        ]

    def generations(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM generations ORDER BY generation"
            ).fetchall()
        finally:
            conn.close()
        return [self._decode_generation(row) for row in rows]

    def verify_chain(self) -> list[str]:
        problems: list[str] = []
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM snapshots ORDER BY version").fetchall()
        finally:
            conn.close()
        previous = None
        for row in rows:
            snapshot = self._decode_snapshot(row)
            if previous is None:
                if (
                    snapshot["prev_version"] is not None
                    or snapshot["prev_checksum"] is not None
                ):
                    problems.append(
                        f"v{snapshot['version']} has unexpected previous link"
                    )
            else:
                if snapshot["prev_version"] != previous["version"]:
                    problems.append(
                        f"v{snapshot['version']} prev_version does not match "
                        f"v{previous['version']}"
                    )
                if snapshot["prev_checksum"] != previous["content_checksum"]:
                    problems.append(
                        f"v{snapshot['version']} prev_checksum mismatch"
                    )
            body = {
                "generation": snapshot["generation"],
                "stage": snapshot["stage"],
                "version": snapshot["version"],
                "prev_version": snapshot["prev_version"],
                "prev_checksum": snapshot["prev_checksum"],
                "completed_layers": snapshot["completed_layers"],
                "artifacts": snapshot["artifacts"],
                "metadata": snapshot["metadata"],
            }
            if digest_json(body) != snapshot["content_checksum"]:
                problems.append(
                    f"v{snapshot['version']} content checksum mismatch"
                )
            previous = snapshot
        return problems

