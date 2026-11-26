from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .config import EngineConfig
from .errors import DiagnosticError
from .utils import (
    canonical_json_bytes,
    read_json,
    sha256_bytes,
    sha256_file,
    sha256_json,
    write_json_atomic,
)


V1_ANCHOR_MS = 1_700_000_000_000


@dataclass(frozen=True)
class MigrationResult:
    config: EngineConfig
    metadata_completions: tuple[dict[str, str], ...]
    dropped_fields: tuple[str, ...]
    output_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": self.config.to_dict(),
            "metadata_completions": list(self.metadata_completions),
            "dropped_fields": list(self.dropped_fields),
            "output_path": self.output_path,
        }


def _v1_parameters(document: dict[str, Any]) -> dict[str, Any]:
    if isinstance(document.get("parameters"), dict):
        return dict(document["parameters"])
    ignored = {
        "version",
        "snapshot_id",
        "relative_seconds",
        "metadata_sources",
        "injections",
    }
    parameters = {
        key: value for key, value in document.items() if key not in ignored
    }
    if "injections" in document:
        parameters["injections"] = document["injections"]
    return parameters


def parse_v1(path: str | Path) -> EngineConfig:
    source = Path(path)
    try:
        document = read_json(source)
    except Exception as exc:
        raise DiagnosticError(
            "E_SNAPSHOT_PARSE", "snapshot", f"cannot parse v1 snapshot: {exc}"
        ) from exc
    if not isinstance(document, dict) or int(document.get("version", -1)) != 1:
        raise DiagnosticError(
            "E_SNAPSHOT_VERSION",
            "snapshot",
            "v1 loader requires version=1",
            details={"path": str(source)},
        )
    snapshot_id = str(document.get("snapshot_id", source.stem))
    parameters = _v1_parameters(document)
    sources: dict[str, str] = dict(document.get("metadata_sources", {}))
    if "generation" in document:
        generation = int(document["generation"])
    else:
        generation = 1
        sources["generation"] = "v1.default_generation=1"
    if "created_at_ms" in document:
        created_at_ms = int(document["created_at_ms"])
        sources["created_at_ms"] = "v1.absolute_milliseconds"
    else:
        relative_seconds = float(document.get("relative_seconds", 0.0))
        created_at_ms = V1_ANCHOR_MS + int(round(relative_seconds * 1000.0))
        sources["created_at_ms"] = "V1_ANCHOR_MS+relative_seconds*1000"
    checksum = sha256_file(source)
    if "content_sha256" in document and document["content_sha256"] != checksum:
        stored = str(document["content_sha256"])
        raise DiagnosticError(
            "E_SNAPSHOT_CHECKSUM",
            "snapshot",
            "v1 content checksum does not match file bytes",
            details={"expected": stored, "actual": checksum},
        )
    return EngineConfig.from_parameters(
        parameters,
        snapshot_id=snapshot_id,
        version=1,
        generation=generation,
        created_at_ms=created_at_ms,
        checksum=checksum,
        metadata_sources=sources,
    )


def write_v1(config: EngineConfig, path: str | Path) -> EngineConfig:
    target = Path(path)
    document = {
        "version": 1,
        "snapshot_id": config.snapshot_id,
        "relative_seconds": round(
            (config.created_at_ms - V1_ANCHOR_MS) / 1000.0, 3
        ),
        **config.parameters(),
    }
    write_json_atomic(target, document)
    return replace(
        config,
        version=1,
        checksum=sha256_file(target),
        metadata_sources=dict(config.metadata_sources),
    )


def _split_parameters(parameters: dict[str, Any]) -> dict[str, dict[str, Any]]:
    feature_keys = {
        "standardization",
        "numeric_columns",
        "category_columns",
        "category_vocab",
    }
    solver_keys = {
        "regularization",
        "strength_path",
        "max_iter",
        "tolerance",
        "learning_rate",
        "l1_ratio",
    }
    execution_keys = {
        "precision",
        "fixed_scale",
        "rounding_mode",
        "block_size",
        "block_budget_bytes",
        "coefficient_tolerance",
        "objective_tolerance",
        "injections",
    }
    shards = {
        "features": {
            key: value for key, value in parameters.items() if key in feature_keys
        },
        "solver": {
            key: value for key, value in parameters.items() if key in solver_keys
        },
        "execution": {
            key: value for key, value in parameters.items() if key in execution_keys
        },
    }
    extras = {
        key: value
        for key, value in parameters.items()
        if key not in feature_keys | solver_keys | execution_keys
    }
    if extras:
        shards["extensions"] = extras
    return shards


def _ensure_v2_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            snapshot_id TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            generation INTEGER NOT NULL,
            created_at_ms INTEGER NOT NULL,
            content_sha256 TEXT NOT NULL,
            manifest_path TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS shards (
            snapshot_id TEXT NOT NULL,
            name TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            PRIMARY KEY (snapshot_id, name)
        )
        """
    )


def write_v2(
    config: EngineConfig,
    directory: str | Path,
    *,
    metadata_completions: tuple[dict[str, str], ...] = (),
) -> EngineConfig:
    root = Path(directory)
    shard_dir = root / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)
    shards = _split_parameters(config.parameters())
    shard_records: list[dict[str, Any]] = []
    merged: dict[str, Any] = {}
    for name in sorted(shards):
        relative_path = f"shards/{name}.json"
        payload = shards[name]
        write_json_atomic(root / relative_path, payload)
        checksum = sha256_bytes(canonical_json_bytes(payload))
        shard_records.append(
            {
                "name": name,
                "path": relative_path,
                "sha256": checksum,
            }
        )
        merged.update(payload)
    digest_payload = {
        "version": 2,
        "snapshot_id": config.snapshot_id,
        "generation": config.generation,
        "created_at_ms": config.created_at_ms,
        "shards": shard_records,
    }
    content_sha256 = sha256_json(digest_payload)
    metadata_sources = dict(config.metadata_sources)
    for item in metadata_completions:
        metadata_sources[item["field"]] = item["source"]
    manifest = {
        **digest_payload,
        "content_sha256": content_sha256,
        "metadata_sources": metadata_sources,
        "metadata_completions": list(metadata_completions),
    }
    manifest_path = root / "manifest.json"
    write_json_atomic(manifest_path, manifest)
    manifest_sha256 = sha256_file(manifest_path)
    database_path = root / "meta.db"
    connection = sqlite3.connect(database_path, timeout=0)
    try:
        _ensure_v2_schema(connection)
        with connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO snapshots
                (snapshot_id, version, generation, created_at_ms,
                 content_sha256, manifest_path, manifest_sha256)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    config.snapshot_id,
                    2,
                    config.generation,
                    config.created_at_ms,
                    content_sha256,
                    "manifest.json",
                    manifest_sha256,
                ),
            )
            connection.execute(
                "DELETE FROM shards WHERE snapshot_id = ?", (config.snapshot_id,)
            )
            connection.executemany(
                """
                INSERT INTO shards
                (snapshot_id, name, relative_path, sha256)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        config.snapshot_id,
                        record["name"],
                        record["path"],
                        record["sha256"],
                    )
                    for record in shard_records
                ],
            )
    finally:
        connection.close()
    return replace(
        config,
        version=2,
        checksum=content_sha256,
        metadata_sources=metadata_sources,
    )


def parse_v2(directory: str | Path) -> EngineConfig:
    root = Path(directory)
    database_path = root / "meta.db"
    manifest_path = root / "manifest.json"
    if not database_path.exists() or not manifest_path.exists():
        raise DiagnosticError(
            "E_SNAPSHOT_PARSE",
            "snapshot",
            "v2 snapshot requires manifest.json and meta.db",
            details={"path": str(root)},
        )
    manifest = read_json(manifest_path)
    if int(manifest.get("version", -1)) != 2:
        raise DiagnosticError(
            "E_SNAPSHOT_VERSION", "snapshot", "v2 manifest requires version=2"
        )
    records = manifest.get("shards", [])
    merged: dict[str, Any] = {}
    for record in records:
        path = root / record["path"]
        actual = sha256_bytes(canonical_json_bytes(read_json(path)))
        if actual != record["sha256"]:
            raise DiagnosticError(
                "E_SNAPSHOT_CHECKSUM",
                "snapshot",
                "v2 shard checksum mismatch",
                details={
                    "shard": record["name"],
                    "expected": record["sha256"],
                    "actual": actual,
                },
            )
        merged.update(read_json(path))
    digest_payload = {
        "version": 2,
        "snapshot_id": manifest["snapshot_id"],
        "generation": int(manifest["generation"]),
        "created_at_ms": int(manifest["created_at_ms"]),
        "shards": records,
    }
    actual_content = sha256_json(digest_payload)
    if actual_content != manifest.get("content_sha256"):
        raise DiagnosticError(
            "E_SNAPSHOT_CHECKSUM",
            "snapshot",
            "v2 content checksum mismatch",
            details={
                "expected": manifest.get("content_sha256"),
                "actual": actual_content,
            },
        )
    connection = sqlite3.connect(database_path, timeout=0)
    try:
        row = connection.execute(
            """
            SELECT version, generation, created_at_ms, content_sha256,
                   manifest_sha256
            FROM snapshots WHERE snapshot_id = ?
            """,
            (manifest["snapshot_id"],),
        ).fetchone()
        shard_rows = connection.execute(
            """
            SELECT name, relative_path, sha256
            FROM shards WHERE snapshot_id = ?
            ORDER BY name
            """,
            (manifest["snapshot_id"],),
        ).fetchall()
    finally:
        connection.close()
    if row is None:
        raise DiagnosticError(
            "E_SNAPSHOT_METADATA",
            "snapshot",
            "snapshot id is absent from SQLite metadata",
        )
    version, generation, created_at_ms, content_sha256, manifest_sha256 = row
    if int(version) != 2 or int(generation) != int(manifest["generation"]):
        raise DiagnosticError(
            "E_SNAPSHOT_METADATA",
            "snapshot",
            "SQLite metadata disagrees with v2 manifest",
        )
    if int(created_at_ms) != int(manifest["created_at_ms"]):
        raise DiagnosticError(
            "E_SNAPSHOT_METADATA",
            "snapshot",
            "v2 created_at_ms must be absolute and match SQLite",
        )
    if content_sha256 != actual_content or manifest_sha256 != sha256_file(manifest_path):
        raise DiagnosticError(
            "E_SNAPSHOT_CHECKSUM",
            "snapshot",
            "SQLite metadata checksum does not match snapshot files",
        )
    manifest_shards = sorted(
        (record["name"], record["path"], record["sha256"])
        for record in records
    )
    sqlite_shards = [tuple(item) for item in shard_rows]
    if sqlite_shards != manifest_shards:
        raise DiagnosticError(
            "E_SNAPSHOT_METADATA",
            "snapshot",
            "SQLite shard metadata disagrees with manifest",
            details={
                "manifest_shards": manifest_shards,
                "sqlite_shards": sqlite_shards,
            },
        )
    return EngineConfig.from_parameters(
        merged,
        snapshot_id=str(manifest["snapshot_id"]),
        version=2,
        generation=int(generation),
        created_at_ms=int(created_at_ms),
        checksum=actual_content,
        metadata_sources=dict(manifest.get("metadata_sources", {})),
    )


def load_snapshot(path: str | Path) -> EngineConfig:
    target = Path(path)
    if target.is_dir():
        if (target / "meta.db").exists() and (target / "manifest.json").exists():
            return parse_v2(target)
        if (target / "config.json").exists():
            return parse_v1(target / "config.json")
        raise DiagnosticError(
            "E_SNAPSHOT_PARSE",
            "snapshot",
            "snapshot directory contains no supported manifest",
            details={"path": str(target)},
        )
    return parse_v1(target)


def upgrade_v1_to_v2(
    v1_path: str | Path, output_directory: str | Path
) -> MigrationResult:
    config = parse_v1(v1_path)
    completions = (
        {
            "field": "generation",
            "source": config.metadata_sources.get("generation", "v1.default_generation=1"),
        },
        {
            "field": "created_at_ms",
            "source": config.metadata_sources.get(
                "created_at_ms", "V1_ANCHOR_MS+relative_seconds*1000"
            ),
        },
        {"field": "content_sha256", "source": "sha256(canonical_v2_shards)"},
        {"field": "shards", "source": "split(v1.parameters, feature/solver/execution)"},
    )
    upgraded = write_v2(
        replace(config, version=2),
        output_directory,
        metadata_completions=completions,
    )
    return MigrationResult(
        config=upgraded,
        metadata_completions=completions,
        dropped_fields=(),
        output_path=str(Path(output_directory)),
    )


def downgrade_v2_to_v1(
    v2_directory: str | Path, output_file: str | Path
) -> MigrationResult:
    config = parse_v2(v2_directory)
    dropped = (
        "generation",
        "created_at_ms",
        "content_sha256",
        "manifest_sha256",
        "shards",
    )
    downgraded = write_v1(replace(config, version=1), output_file)
    completions = tuple(
        {
            "field": field,
            "source": f"v2.{field}",
        }
        for field in ("version", "relative_seconds")
    )
    return MigrationResult(
        config=downgraded,
        metadata_completions=completions,
        dropped_fields=dropped,
        output_path=str(Path(output_file)),
    )


class SnapshotCommitLock:
    """Non-blocking process/thread lock for snapshot replacement."""

    def __init__(self, store_root: str | Path, *, lease_ms: int = 30_000) -> None:
        self.store_root = Path(store_root)
        self.lease_ms = lease_ms
        self.database_path = self.store_root / "control.db"
        self.connection: sqlite3.Connection | None = None
        self.token = uuid.uuid4().hex

    def __enter__(self) -> "SnapshotCommitLock":
        self.store_root.mkdir(parents=True, exist_ok=True)
        setup = sqlite3.connect(self.database_path, timeout=0)
        try:
            setup.execute(
                """
                CREATE TABLE IF NOT EXISTS commit_lock (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    token TEXT,
                    acquired_at_ms INTEGER,
                    lease_expires_ms INTEGER
                )
                """
            )
            setup.commit()
        finally:
            setup.close()
        connection = sqlite3.connect(
            self.database_path, timeout=0, isolation_level=None
        )
        try:
            connection.execute("PRAGMA busy_timeout = 0")
            connection.execute("BEGIN IMMEDIATE")
            now_ms = int(time.time() * 1000)
            row = connection.execute(
                "SELECT token, lease_expires_ms FROM commit_lock WHERE id = 1"
            ).fetchone()
            if row and row[0] and int(row[1]) > now_ms:
                connection.execute("ROLLBACK")
                raise DiagnosticError(
                    "E_STAGE_BUSY",
                    "snapshot_commit",
                    "another snapshot commit is in progress",
                    degrade_category="commit_retry_later",
                )
            connection.execute(
                """
                INSERT INTO commit_lock
                (id, token, acquired_at_ms, lease_expires_ms)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    token = excluded.token,
                    acquired_at_ms = excluded.acquired_at_ms,
                    lease_expires_ms = excluded.lease_expires_ms
                """,
                (self.token, now_ms, now_ms + self.lease_ms),
            )
            connection.execute("COMMIT")
        except DiagnosticError:
            connection.close()
            raise
        except sqlite3.OperationalError as exc:
            connection.close()
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise DiagnosticError(
                    "E_STAGE_BUSY",
                    "snapshot_commit",
                    "another snapshot commit already holds the commit lock",
                    degrade_category="commit_retry_later",
                ) from exc
            raise
        self.connection = connection
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.connection is None:
            return
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            now_ms = int(time.time() * 1000)
            self.connection.execute(
                """
                UPDATE commit_lock SET token = NULL, lease_expires_ms = ?
                WHERE id = 1 AND token = ? AND lease_expires_ms >= ?
                """,
                (now_ms, self.token, now_ms),
            )
            self.connection.execute("COMMIT")
        finally:
            self.connection.close()


@dataclass(frozen=True)
class InstallResult:
    snapshot_id: str
    version: int
    generation: int
    checksum: str
    active_path: str
    replacement_mode: str
    previous: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "version": self.version,
            "generation": self.generation,
            "checksum": self.checksum,
            "active_path": self.active_path,
            "replacement_mode": self.replacement_mode,
            "previous": self.previous,
        }


def install_snapshot(
    source: str | Path,
    store_root: str | Path,
    *,
    slot: str = "active",
) -> InstallResult:
    """Install a complete snapshot and atomically switch the active pointer.

    Snapshot directories are immutable. A version/checksum change always creates
    a new complete directory and replaces the pointer, never patches old files.
    """

    root = Path(store_root)
    root.mkdir(parents=True, exist_ok=True)
    with SnapshotCommitLock(root):
        config = load_snapshot(source)
        pointer_path = root / f"{slot}.json"
        previous = read_json(pointer_path) if pointer_path.exists() else None
        if previous and int(config.version) < int(previous["version"]):
            raise DiagnosticError(
                "E_SNAPSHOT_ROLLBACK",
                "snapshot_commit",
                "snapshot version rollback is forbidden; use explicit migration output",
                degrade_category="keep_active_snapshot",
                details={
                    "active_version": previous["version"],
                    "candidate_version": config.version,
                },
            )
        if (
            previous
            and int(config.version) == int(previous["version"])
            and config.checksum == previous["checksum"]
            and config.snapshot_id == previous["snapshot_id"]
            and int(config.generation) == int(previous["generation"])
        ):
            return InstallResult(
                snapshot_id=config.snapshot_id,
                version=config.version,
                generation=config.generation,
                checksum=config.checksum,
                active_path=str(previous["path"]),
                replacement_mode="no_change",
                previous=previous,
            )
        target_name = (
            f"{config.snapshot_id}-v{config.version}-{config.checksum[:12]}"
        )
        target = root / "snapshots" / target_name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temp_root = Path(
                tempfile.mkdtemp(prefix=".snapshot-install-", dir=str(root))
            )
            try:
                staged = temp_root / "snapshot"
                source_path = Path(source)
                if source_path.is_dir():
                    shutil.copytree(source_path, staged)
                else:
                    staged.mkdir()
                    shutil.copy2(source_path, staged / "config.json")
                os.replace(staged, target)
            finally:
                shutil.rmtree(temp_root, ignore_errors=True)
        pointer = {
            "snapshot_id": config.snapshot_id,
            "version": config.version,
            "generation": config.generation,
            "checksum": config.checksum,
            "path": str(target),
            "installed_at_ms": max(
                int(time.time() * 1000),
                previous["installed_at_ms"] if previous else 0,
            ),
        }
        write_json_atomic(pointer_path, pointer)
        return InstallResult(
            snapshot_id=config.snapshot_id,
            version=config.version,
            generation=config.generation,
            checksum=config.checksum,
            active_path=str(target),
            replacement_mode="whole_snapshot_pointer_swap",
            previous=previous,
        )

