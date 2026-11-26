"""Generation-aware strategy snapshots with atomic, single-writer commits."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import (
    BusinessError,
    E_INVALID_INPUT,
    E_SNAPSHOT_CORRUPT,
    E_STAGE_BUSY,
    E_STALE_VERSION,
)
from .utils import (
    atomic_write_json,
    canonical_json,
    iso_to_ms,
    json_pretty,
    now_ms,
    read_json,
    sha256_json,
)


DEFAULT_OBJECTIVE_PRIORITY = [
    "progress",
    "memory",
    "sampling_target",
    "dedup_quality",
]


def _without_checksum(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in snapshot.items()
        if key != "checksum" and not key.startswith("_")
    }


def strategy_checksum(snapshot: dict[str, Any]) -> str:
    return sha256_json(_without_checksum(snapshot))


def _validate_common_snapshot(snapshot: dict[str, Any], expected_generation: int) -> None:
    if not isinstance(snapshot, dict):
        raise BusinessError(E_INVALID_INPUT, "strategy snapshot must be an object")
    if not isinstance(snapshot.get("config"), dict):
        raise BusinessError(E_INVALID_INPUT, "strategy snapshot is missing config")
    if not snapshot.get("strategy_id"):
        raise BusinessError(E_INVALID_INPUT, "strategy snapshot is missing strategy_id")
    if not isinstance(snapshot.get("snapshot_version"), int):
        raise BusinessError(
            E_INVALID_INPUT, "strategy snapshot_version must be an integer"
        )
    if expected_generation == 1:
        if int(snapshot.get("version", 0)) != 1:
            raise BusinessError(E_INVALID_INPUT, "v1 snapshot must declare version=1")
        if snapshot.get("tombstones"):
            raise BusinessError(
                E_INVALID_INPUT,
                "v1 cannot represent tombstones; use explicit v2-to-v1 downgrade",
            )
    else:
        if int(snapshot.get("schema_version", 0)) != 2:
            raise BusinessError(
                E_INVALID_INPUT, "v2 snapshot must declare schema_version=2"
            )
        if int(snapshot.get("strategy_generation", 0)) != 2:
            raise BusinessError(
                E_INVALID_INPUT, "v2 snapshot must declare strategy_generation=2"
            )


class V1StrategyStore:
    """Single-file JSON strategy store with an exclusive commit lock."""

    generation = 1

    def __init__(self, path: str | Path, *, lock_ttl_ms: int = 30_000) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.lock_ttl_ms = lock_ttl_ms

    def _acquire_lock(self, owner: str) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        token = now_ms()
        for _ in range(2):
            try:
                fd = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(
                        canonical_json(
                            {"owner": owner, "pid": os.getpid(), "at_ms": token}
                        )
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                return token
            except FileExistsError:
                try:
                    age = now_ms() - int(self.lock_path.stat().st_mtime * 1000)
                except FileNotFoundError:
                    continue
                if age > self.lock_ttl_ms:
                    try:
                        self.lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                raise BusinessError(
                    E_STAGE_BUSY,
                    "another strategy snapshot commit is in progress",
                    details={"lock_path": str(self.lock_path), "owner": owner},
                )
        raise BusinessError(
            E_STAGE_BUSY,
            "unable to acquire strategy snapshot commit lock",
            details={"lock_path": str(self.lock_path), "owner": owner},
        )

    def _release_lock(self, token: int) -> None:
        try:
            raw = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return
        if int(raw.get("at_ms", -1)) == token:
            self.lock_path.unlink(missing_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            raise BusinessError(
                E_INVALID_INPUT,
                "v1 strategy snapshot does not exist",
                details={"path": str(self.path)},
            )
        snapshot = read_json(self.path)
        _validate_common_snapshot(snapshot, 1)
        actual = strategy_checksum(snapshot)
        if snapshot.get("checksum") != actual:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "v1 strategy checksum mismatch",
                details={"expected": snapshot.get("checksum"), "actual": actual},
            )
        return snapshot

    def commit(
        self,
        snapshot: dict[str, Any],
        *,
        owner: str = "local",
        expected_version: int | None = None,
        _test_hold_ms: int = 0,
    ) -> dict[str, Any]:
        _validate_common_snapshot(snapshot, 1)
        incoming = dict(snapshot)
        incoming["checksum"] = strategy_checksum(incoming)
        token = self._acquire_lock(owner)
        try:
            if _test_hold_ms:
                time.sleep(_test_hold_ms / 1000.0)
            if self.path.exists():
                current = self.load()
                current_version = int(current["snapshot_version"])
                if expected_version is not None and expected_version != current_version:
                    raise BusinessError(
                        E_STALE_VERSION,
                        "expected active strategy version does not match",
                        details={
                            "expected": expected_version,
                            "active": current_version,
                        },
                    )
                if int(incoming["snapshot_version"]) < current_version:
                    raise BusinessError(
                        E_STALE_VERSION,
                        "strategy snapshot version must not move backwards",
                        details={
                            "incoming": incoming["snapshot_version"],
                            "active": current_version,
                        },
                    )
                if (
                    int(incoming["snapshot_version"]) == current_version
                    and incoming["checksum"] == current.get("checksum")
                ):
                    return current
            elif expected_version not in (None, 0, 1):
                raise BusinessError(
                    E_STALE_VERSION,
                    "no active snapshot exists for the expected version",
                    details={"expected": expected_version},
                )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.path, incoming)
            return incoming
        finally:
            self._release_lock(token)


class V2StrategyStore:
    """V2 store: JSON shards plus SQLite active-pointer/commit guard."""

    generation = 2

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.db_path = self.root / "strategy.sqlite"
        self.generations_dir = self.root / "generations"
        self.active_json_path = self.root / "active.json"
        self._initialize()

    def _connect(self, timeout: float = 0.0) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=timeout,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 0")
        return connection

    def _initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.generations_dir.mkdir(parents=True, exist_ok=True)
        connection = self._connect(timeout=5.0)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    snapshot_version INTEGER PRIMARY KEY,
                    checksum TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    generation_dir TEXT NOT NULL,
                    committed_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS active_snapshot (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    snapshot_version INTEGER NOT NULL,
                    checksum TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS commit_guard (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    owner TEXT,
                    acquired_at_ms INTEGER,
                    expires_at_ms INTEGER
                );
                CREATE TABLE IF NOT EXISTS snapshot_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    at_ms INTEGER NOT NULL,
                    owner TEXT NOT NULL,
                    snapshot_version INTEGER NOT NULL,
                    checksum TEXT NOT NULL,
                    action TEXT NOT NULL
                );
                INSERT OR IGNORE INTO commit_guard
                    (singleton, owner, acquired_at_ms, expires_at_ms)
                VALUES (1, NULL, NULL, NULL);
                """
            )
        finally:
            connection.close()

    def _write_generation(
        self,
        snapshot: dict[str, Any],
        checksum: str,
        generation_dir: Path,
    ) -> dict[str, Any]:
        config = snapshot["config"]
        shard_dir = generation_dir / "shards"
        shard_dir.mkdir(parents=True, exist_ok=True)
        shard_checksums: dict[str, str] = {}
        for key in sorted(config):
            safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)
            filename = f"{safe_key}.json"
            payload = config[key]
            atomic_write_json(shard_dir / filename, payload)
            shard_checksums[key] = sha256_json(payload)
        metadata = {
            key: value
            for key, value in snapshot.items()
            if key not in {"config", "checksum"}
        }
        manifest = dict(metadata)
        manifest["checksum"] = checksum
        manifest["config_shards"] = {
            key: f"shards/{re.sub(r'[^A-Za-z0-9_.-]+', '_', key)}.json"
            for key in sorted(config)
        }
        manifest["config_checksums"] = shard_checksums
        atomic_write_json(generation_dir / "manifest.json", manifest)
        return manifest

    def _materialize_generation(
        self,
        snapshot: dict[str, Any],
        checksum: str,
    ) -> tuple[str, dict[str, Any]]:
        digest = checksum.split(":", 1)[-1][:12]
        name = f"v{int(snapshot['snapshot_version']):06d}-{digest}"
        target = self.generations_dir / name
        if target.exists():
            manifest_path = target / "manifest.json"
            if manifest_path.exists():
                manifest = read_json(manifest_path)
                if manifest.get("checksum") == checksum:
                    return name, manifest
            shutil.rmtree(target)
        temp = Path(
            tempfile.mkdtemp(prefix=".tmp-", dir=str(self.generations_dir))
        )
        try:
            manifest = self._write_generation(snapshot, checksum, temp)
            os.replace(temp, target)
        except BaseException:
            shutil.rmtree(temp, ignore_errors=True)
            raise
        return name, manifest

    def _read_active_manifest(self) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT s.manifest_json, s.generation_dir
                FROM active_snapshot a
                JOIN snapshots s
                  ON s.snapshot_version = a.snapshot_version
                 AND s.checksum = a.checksum
                WHERE a.singleton = 1
                """
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        manifest = json.loads(row["manifest_json"])
        manifest["_generation_dir"] = row["generation_dir"]
        return manifest

    def load_active(self) -> dict[str, Any]:
        manifest = self._read_active_manifest()
        if manifest is None:
            raise BusinessError(
                E_INVALID_INPUT,
                "no active v2 strategy snapshot",
                details={"store": str(self.root)},
            )
        generation_dir = self.generations_dir / manifest["_generation_dir"]
        config: dict[str, Any] = {}
        for key, relative in manifest["config_shards"].items():
            shard_path = generation_dir / relative
            if not shard_path.exists():
                raise BusinessError(
                    E_SNAPSHOT_CORRUPT,
                    "v2 strategy shard is missing",
                    details={"shard": str(shard_path)},
                )
            payload = read_json(shard_path)
            expected = manifest["config_checksums"].get(key)
            actual = sha256_json(payload)
            if expected != actual:
                raise BusinessError(
                    E_SNAPSHOT_CORRUPT,
                    "v2 strategy shard checksum mismatch",
                    details={
                        "shard": str(shard_path),
                        "expected": expected,
                        "actual": actual,
                    },
                )
            config[key] = payload
        snapshot = {
            key: value
            for key, value in manifest.items()
            if key
            not in {
                "_generation_dir",
                "config_shards",
                "config_checksums",
            }
        }
        snapshot["config"] = config
        actual_checksum = strategy_checksum(snapshot)
        if actual_checksum != snapshot.get("checksum"):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "v2 strategy manifest checksum mismatch",
                details={
                    "expected": snapshot.get("checksum"),
                    "actual": actual_checksum,
                },
            )
        snapshot["_generation_dir"] = manifest["_generation_dir"]
        return snapshot

    def commit(
        self,
        snapshot: dict[str, Any],
        *,
        owner: str = "local",
        expected_version: int | None = None,
        _test_hold_ms: int = 0,
    ) -> dict[str, Any]:
        _validate_common_snapshot(snapshot, 2)
        incoming = {
            key: value
            for key, value in snapshot.items()
            if key != "_generation_dir"
        }
        incoming["checksum"] = strategy_checksum(incoming)
        checksum = incoming["checksum"]
        version = int(incoming["snapshot_version"])
        self.root.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise BusinessError(
                    E_STAGE_BUSY,
                    "another strategy snapshot commit owns the database lock",
                    details={"owner": owner, "sqlite_error": str(exc)},
                ) from exc
            try:
                guard = connection.execute(
                    "SELECT owner, expires_at_ms FROM commit_guard WHERE singleton = 1"
                ).fetchone()
                current_ms = now_ms()
                if (
                    guard
                    and guard["owner"]
                    and guard["owner"] != owner
                    and int(guard["expires_at_ms"] or 0) > current_ms
                ):
                    raise BusinessError(
                        E_STAGE_BUSY,
                        "another strategy snapshot commit is in progress",
                        details={"owner": owner, "lock_owner": guard["owner"]},
                    )
                connection.execute(
                    """
                    UPDATE commit_guard
                    SET owner = ?, acquired_at_ms = ?, expires_at_ms = ?
                    WHERE singleton = 1
                    """,
                    (owner, current_ms, current_ms + 30_000),
                )
                active = connection.execute(
                    """
                    SELECT snapshot_version, checksum
                    FROM active_snapshot WHERE singleton = 1
                    """
                ).fetchone()
                active_version = int(active["snapshot_version"]) if active else 0
                if expected_version is not None and expected_version != active_version:
                    raise BusinessError(
                        E_STALE_VERSION,
                        "expected active strategy version does not match",
                        details={
                            "expected": expected_version,
                            "active": active_version,
                        },
                    )
                if version < active_version:
                    raise BusinessError(
                        E_STALE_VERSION,
                        "strategy snapshot version must not move backwards",
                        details={"incoming": version, "active": active_version},
                    )
                if (
                    active
                    and version == active_version
                    and checksum == active["checksum"]
                ):
                    return self.load_active()
                if _test_hold_ms:
                    time.sleep(_test_hold_ms / 1000.0)
                generation_name, manifest = self._materialize_generation(
                    incoming, checksum
                )
                committed_at = now_ms()
                connection.execute(
                    """
                    INSERT INTO snapshots
                        (snapshot_version, checksum, manifest_json,
                         generation_dir, committed_at_ms)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(snapshot_version) DO UPDATE SET
                        checksum = excluded.checksum,
                        manifest_json = excluded.manifest_json,
                        generation_dir = excluded.generation_dir,
                        committed_at_ms = excluded.committed_at_ms
                    """,
                    (
                        version,
                        checksum,
                        json_pretty(manifest),
                        generation_name,
                        committed_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO active_snapshot
                        (singleton, snapshot_version, checksum)
                    VALUES (1, ?, ?)
                    ON CONFLICT(singleton) DO UPDATE SET
                        snapshot_version = excluded.snapshot_version,
                        checksum = excluded.checksum
                    """,
                    (version, checksum),
                )
                connection.execute(
                    """
                    INSERT INTO snapshot_events
                        (at_ms, owner, snapshot_version, checksum, action)
                    VALUES (?, ?, ?, ?, 'commit')
                    """,
                    (committed_at, owner, version, checksum),
                )
                connection.execute(
                    "UPDATE commit_guard SET owner = NULL WHERE singleton = 1"
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()
        active_pointer = {
            "snapshot_version": version,
            "checksum": checksum,
            "generation_dir": generation_name,
            "manifest": "manifest.json",
        }
        atomic_write_json(self.active_json_path, active_pointer)
        active_manifest = read_json(
            self.generations_dir / generation_name / "manifest.json"
        )
        active_manifest["generation_dir"] = generation_name
        atomic_write_json(self.root / "manifest.json", active_manifest)
        return self.load_active()

    def history(self) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT snapshot_version, checksum, generation_dir, committed_at_ms
                FROM snapshots ORDER BY snapshot_version
                """
            ).fetchall()
            active = connection.execute(
                "SELECT snapshot_version, checksum FROM active_snapshot WHERE singleton = 1"
            ).fetchone()
        finally:
            connection.close()
        active_version = active["snapshot_version"] if active else None
        return [
            {
                "snapshot_version": row["snapshot_version"],
                "checksum": row["checksum"],
                "generation_dir": row["generation_dir"],
                "committed_at_ms": row["committed_at_ms"],
                "active": row["snapshot_version"] == active_version,
            }
            for row in rows
        ]


def _iso_from_ms(value: int) -> str:
    return (
        datetime.fromtimestamp(value / 1000.0, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def migrate_v1_to_v2(
    v1: dict[str, Any],
    *,
    source_path: str = "",
    fallback_ms: int | None = None,
    snapshot_version: int | None = None,
) -> dict[str, Any]:
    """Complete v1-only gaps explicitly while preserving the whole config."""

    _validate_common_snapshot(v1, 1)
    if v1.get("checksum") != strategy_checksum(v1):
        raise BusinessError(
            E_SNAPSHOT_CORRUPT,
            "source v1 strategy checksum mismatch",
        )
    fallback = now_ms() if fallback_ms is None else fallback_ms
    created_ms = iso_to_ms(
        v1.get("created_at_ms") or v1.get("created_at"),
        fallback,
    )
    updated_ms = iso_to_ms(
        v1.get("updated_at_ms") or v1.get("updated_at"),
        created_ms,
    )
    tombstones: list[dict[str, Any]] = []
    for item in v1.get("retired_keys", []):
        if isinstance(item, str):
            tombstones.append(
                {
                    "key": item,
                    "reason": "retired in v1; tombstone materialized by migration",
                    "since_ms": updated_ms,
                    "source": "v1-retired_keys",
                }
            )
        elif isinstance(item, dict):
            tombstone = dict(item)
            tombstone.setdefault("reason", "retired in v1")
            tombstone.setdefault("source", "v1-retired_keys")
            tombstone["since_ms"] = iso_to_ms(
                tombstone.get("since_ms") or tombstone.get("since"),
                updated_ms,
            )
            tombstones.append(tombstone)
    v2 = {
        "schema_version": 2,
        "strategy_generation": 2,
        "strategy_id": v1["strategy_id"],
        "revision": int(v1.get("revision", 1)),
        "snapshot_version": int(
            snapshot_version
            if snapshot_version is not None
            else max(2, int(v1["snapshot_version"]) + 1)
        ),
        "created_at_ms": created_ms,
        "updated_at_ms": updated_ms,
        "priorities": list(v1.get("priorities", DEFAULT_OBJECTIVE_PRIORITY)),
        "tombstones": tombstones,
        "config": v1["config"],
        "source": {
            "kind": "v1-upgrade",
            "source_path": source_path,
            "source_checksum": v1.get("checksum"),
            "completed_by": "ifdata.strategy.migrate_v1_to_v2",
            "completed_fields": {
                "strategy_generation": "set to 2",
                "tombstones": (
                    "converted-from-v1-retired_keys"
                    if tombstones
                    else "initialized-empty"
                ),
                "timestamps": "converted-to-absolute-unix-ms",
            },
        },
    }
    v2["checksum"] = strategy_checksum(v2)
    return v2


def migrate_v2_to_v1(
    v2: dict[str, Any],
    *,
    source_path: str = "",
    fallback_ms: int | None = None,
    audit_path: str | Path | None = None,
) -> dict[str, Any]:
    """Downgrade to v1 and record an explicit discard rule for tombstones."""

    _validate_common_snapshot(v2, 2)
    if v2.get("checksum") != strategy_checksum(v2):
        raise BusinessError(
            E_SNAPSHOT_CORRUPT,
            "source v2 strategy checksum mismatch",
        )
    fallback = now_ms() if fallback_ms is None else fallback_ms
    created_ms = int(v2.get("created_at_ms", fallback))
    updated_ms = int(v2.get("updated_at_ms", created_ms))
    tombstones = list(v2.get("tombstones", []))
    audit = {
        "schema_version": 1,
        "operation": "v2-to-v1",
        "source_path": source_path,
        "source_checksum": v2.get("checksum"),
        "discard_rule": (
            "V1_CANNOT_REPRESENT_TOMBSTONES: drop every tombstone from the "
            "v1 snapshot and preserve the full payload in this audit file"
        ),
        "discarded": {"tombstones": tombstones},
        "preserved": {
            "config": True,
            "strategy_id": True,
            "revision": True,
            "snapshot_version": True,
        },
    }
    audit_checksum = sha256_json(audit)
    if audit_path is not None:
        atomic_write_json(audit_path, audit)
    v1 = {
        "version": 1,
        "strategy_id": v2["strategy_id"],
        "revision": int(v2.get("revision", 1)),
        "snapshot_version": int(v2["snapshot_version"]),
        "created_at": _iso_from_ms(created_ms),
        "updated_at": _iso_from_ms(updated_ms),
        "priorities": list(v2.get("priorities", DEFAULT_OBJECTIVE_PRIORITY)),
        "config": v2["config"],
        "migration_note": {
            "from_generation": 2,
            "source_checksum": v2.get("checksum"),
            "discarded_tombstones": len(tombstones),
            "discard_rule": "V1_CANNOT_REPRESENT_TOMBSTONES",
            "audit_checksum": audit_checksum,
            **(
                {"audit_path": str(audit_path)}
                if audit_path is not None
                else {}
            ),
        },
    }
    v1["checksum"] = strategy_checksum(v1)
    return v1


def locate_strategy(data_dir: str | Path, generation: int = 2) -> Path:
    root = Path(data_dir)
    if generation == 1:
        candidates = [
            root / "strategy" / "v1" / "strategy.json",
            root / "strategy" / "v1.json",
        ]
    else:
        candidates = [
            root / "strategy" / "v2",
            root / "strategy" / "v2.json",
        ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise BusinessError(
        E_INVALID_INPUT,
        f"strategy generation {generation} not found",
        details={"data_dir": str(root), "candidates": [str(p) for p in candidates]},
    )


def load_strategy_file(path: str | Path, generation: int) -> dict[str, Any]:
    candidate = Path(path)
    if generation == 1:
        return V1StrategyStore(candidate).load()
    return V2StrategyStore(candidate).load_active()

