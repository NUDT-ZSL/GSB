"""Content-addressed object store, SQLite index and versioned snapshots.

* Artifact bytes live in sharded, sha256-named objects.
* ``cache_entries`` maps structured cache keys (clip_id, noise_seed, revision,
  block_grid and upstream hashes) to content hashes.
* ``snapshots`` / ``current_snapshot`` implement forward-only, wholesale
  snapshot replacement; stale writers and backward rollback are rejected with
  ``E_SNAPSHOT_ROLLBACK``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Workspace
from .errors import E_CHECKSUM_MISMATCH, E_NOT_FOUND, E_SNAPSHOT_ROLLBACK, KFError
from .hashing import canonical_json, hash_bytes, hash_json


SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
    hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cache_entries (
    cache_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    clip_id TEXT,
    revision INTEGER,
    block_grid INTEGER,
    upstream_sig TEXT,
    provenance_hash TEXT,
    trajectory_hash TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_upstream
    ON cache_entries(clip_id, kind, upstream_sig);
CREATE TABLE IF NOT EXISTS snapshots (
    clip_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    manifest_hash TEXT NOT NULL,
    plan_hash TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (clip_id, version)
);
CREATE TABLE IF NOT EXISTS current_snapshot (
    clip_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    manifest_hash TEXT NOT NULL,
    committed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ready_keyframes (
    clip_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    frame_index INTEGER NOT NULL,
    block_index INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    ready_at REAL NOT NULL,
    PRIMARY KEY (clip_id, ordinal)
);
CREATE TABLE IF NOT EXISTS cache_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    clip_id TEXT,
    kind TEXT NOT NULL,
    result TEXT NOT NULL,
    detail TEXT
);
"""


class ContentStore:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.object_dir = workspace.object_dir
        self.object_dir.mkdir(parents=True, exist_ok=True)
        self._known_dirs: set[str] = set()

    def _path_for(self, digest: str) -> Path:
        directory = self.object_dir / digest[:2] / digest[2:4]
        if str(directory) not in self._known_dirs:
            directory.mkdir(parents=True, exist_ok=True)
            self._known_dirs.add(str(directory))
        return directory / digest

    def write_bytes(self, data: bytes, kind: str) -> Dict[str, Any]:
        digest = hash_bytes(data)
        path = self._path_for(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                os.replace(tmp_name, path)
            except BaseException:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
                raise
        return {"hash": digest, "kind": kind, "path": str(path), "size_bytes": len(data)}

    def write_json(self, obj: Any, kind: str) -> Dict[str, Any]:
        return self.write_bytes(canonical_json(obj).encode("utf-8"), kind)

    def read_bytes(self, digest: str) -> bytes:
        path = self._path_for(digest)
        if not path.exists():
            raise KFError(E_NOT_FOUND, f"content object {digest[:12]} is missing", {"hash": digest})
        return path.read_bytes()

    def read_json(self, digest: str) -> Any:
        return json.loads(self.read_bytes(digest).decode("utf-8"))

    def has(self, digest: str) -> bool:
        return self._path_for(digest).exists()

    def verify(self, digest: str) -> bool:
        data = self.read_bytes(digest)
        actual = hash_bytes(data)
        if actual != digest:
            raise KFError(
                E_CHECKSUM_MISMATCH,
                f"checksum mismatch for content object {digest[:12]}",
                {"expected": digest, "actual": actual},
            )
        return True


class IndexDB:
    """SQLite index with per-thread connections (sqlite.threadsafety == 1)."""

    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        workspace.cache_dir.mkdir(parents=True, exist_ok=True)
        self._tls = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.workspace.db_path), timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._tls.conn = conn
        return conn

    def init_schema(self) -> None:
        with self._init_lock:
            conn = self._connect()
            conn.executescript(SCHEMA)
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(cache_entries)")}
            for column in ("provenance_hash", "trajectory_hash"):
                if column not in existing:
                    conn.execute(f"ALTER TABLE cache_entries ADD COLUMN {column} TEXT")
            conn.commit()
            self._initialized = True

    def record_object(self, meta: Dict[str, Any]) -> None:
        conn = self._connect()
        conn.execute(
            "INSERT OR IGNORE INTO objects(hash, kind, path, size_bytes, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (meta["hash"], meta["kind"], meta["path"], meta["size_bytes"], time.time()),
        )
        conn.commit()

    def put_cache_entry(self, entry: Dict[str, Any]) -> None:
        conn = self._connect()
        conn.execute(
            "INSERT OR REPLACE INTO cache_entries(cache_key, kind, content_hash, clip_id, "
            "revision, block_grid, upstream_sig, provenance_hash, trajectory_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry["cache_key"], entry["kind"], entry["content_hash"], entry.get("clip_id"),
                entry.get("revision"), entry.get("block_grid"), entry.get("upstream_sig"),
                entry.get("provenance_hash"), entry.get("trajectory_hash"), time.time(),
            ),
        )
        conn.commit()

    def get_cache_entry(self, cache_key: str) -> Optional[Dict[str, Any]]:
        row = self._connect().execute(
            "SELECT * FROM cache_entries WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        return dict(row) if row else None

    def find_upstream_artifact(self, clip_id: str, kind: str, upstream_sig: str) -> Optional[Dict[str, Any]]:
        row = self._connect().execute(
            "SELECT * FROM cache_entries WHERE clip_id = ? AND kind = ? AND upstream_sig = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (clip_id, kind, upstream_sig),
        ).fetchone()
        return dict(row) if row else None

    def record_event(self, clip_id: Optional[str], kind: str, result: str, detail: str = "") -> None:
        conn = self._connect()
        conn.execute(
            "INSERT INTO cache_events(ts, clip_id, kind, result, detail) VALUES (?, ?, ?, ?, ?)",
            (time.time(), clip_id, kind, result, detail),
        )
        conn.commit()

    def event_summary(self) -> Dict[str, Any]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT result, COUNT(*) AS n FROM cache_events GROUP BY result"
        ).fetchall()
        by_result = {row["result"]: row["n"] for row in rows}
        recent = [
            dict(row)
            for row in conn.execute(
                "SELECT ts, clip_id, kind, result, detail FROM cache_events "
                "ORDER BY id DESC LIMIT 12"
            ).fetchall()
        ]
        return {"by_result": by_result, "recent": recent}

    def upsert_ready(self, clip_id: str, ordinal: int, frame_index: int,
                     block_index: int, content_hash: str) -> None:
        conn = self._connect()
        conn.execute(
            "INSERT OR REPLACE INTO ready_keyframes(clip_id, ordinal, frame_index, "
            "block_index, content_hash, ready_at) VALUES (?, ?, ?, ?, ?, ?)",
            (clip_id, ordinal, frame_index, block_index, content_hash, time.time()),
        )
        conn.commit()

    def list_ready(self, clip_id: str) -> List[Dict[str, Any]]:
        rows = self._connect().execute(
            "SELECT ordinal, frame_index, block_index, content_hash, ready_at "
            "FROM ready_keyframes WHERE clip_id = ? ORDER BY ordinal",
            (clip_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def clear_ready(self, clip_id: str) -> None:
        conn = self._connect()
        conn.execute("DELETE FROM ready_keyframes WHERE clip_id = ?", (clip_id,))
        conn.commit()

    def reconcile_ready(self, clip_id: str, items: List[Dict[str, Any]]) -> None:
        conn = self._connect()
        with conn:
            conn.execute("DELETE FROM ready_keyframes WHERE clip_id = ?", (clip_id,))
            conn.executemany(
                "INSERT INTO ready_keyframes(clip_id, ordinal, frame_index, block_index, "
                "content_hash, ready_at) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (clip_id, item["ordinal"], item["frame_index"], item["block_index"],
                     item["content_hash"], time.time())
                    for item in items
                ],
            )

    def stats(self) -> Dict[str, Any]:
        conn = self._connect()
        objects = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(size_bytes),0) bytes FROM objects").fetchone()
        entries = conn.execute("SELECT COUNT(*) n FROM cache_entries").fetchone()
        snapshots = conn.execute("SELECT COUNT(*) n FROM snapshots").fetchone()
        return {
            "object_count": objects["n"],
            "object_bytes": objects["bytes"],
            "cache_entry_count": entries["n"],
            "snapshot_count": snapshots["n"],
        }


def keyframe_upstream_signature(provenance: Dict[str, Any]) -> str:
    """Inputs that actually determine keyframe pixels (revision independent)."""
    return hash_json(
        {
            "noise_hash": provenance["noise_hash"],
            "noise_stats_hash": provenance["noise_stats"]["stats_hash"],
            "embedding_hash": provenance["embedding_hash"],
            "norm_stats_hash": provenance["norm_stats_hash"],
            "schedule_hash": provenance["schedule_hash"],
            "sampler_config_hash": provenance.get("sampler_config_hash"),
            "effective_steps": provenance["effective_steps"],
            "strength": provenance["strength"],
            "step_indices": provenance["step_indices"],
            "coupling_neighbors": provenance["coupling"]["neighbors"],
            "coupling_links": provenance["coupling"].get("links"),
        }
    )


def keyframe_cache_key(task_identity: Dict[str, Any], block_grid: int, upstream_sig: str) -> str:
    """Structured cache key including clip_id, noise_seed, revision, grid."""
    return hash_json(
        {
            "cache": "keyframe",
            "clip_id": task_identity["clip_id"],
            "noise_seed": task_identity["noise_seed"],
            "revision": task_identity["revision"],
            "block_grid": int(block_grid),
            "upstream": upstream_sig,
        }
    )


class SnapshotManager:
    def __init__(self, workspace: Workspace, store: ContentStore, db: IndexDB):
        self.workspace = workspace
        self.store = store
        self.db = db
        self.snapshot_root = workspace.snapshot_dir
        self.snapshot_root.mkdir(parents=True, exist_ok=True)

    def _clip_dir(self, clip_id: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in clip_id)
        path = self.snapshot_root / safe
        path.mkdir(parents=True, exist_ok=True)
        return path

    def current_version(self, clip_id: str) -> int:
        row = self.db._connect().execute(
            "SELECT version FROM current_snapshot WHERE clip_id = ?", (clip_id,)
        ).fetchone()
        return int(row["version"]) if row else 0

    def current_manifest(self, clip_id: str) -> Optional[Dict[str, Any]]:
        row = self.db._connect().execute(
            "SELECT manifest_hash FROM current_snapshot WHERE clip_id = ?", (clip_id,)
        ).fetchone()
        if not row:
            pointer = self._clip_dir(clip_id) / "current.json"
            if pointer.exists():
                data = json.loads(pointer.read_text())
                manifest = self.store.read_json(data["manifest_hash"])
                manifest["manifest_hash"] = data["manifest_hash"]
                return manifest
            return None
        manifest = self.store.read_json(row["manifest_hash"])
        manifest["manifest_hash"] = row["manifest_hash"]
        return manifest

    def commit_new(
        self,
        clip_id: str,
        revision: int,
        body: Dict[str, Any],
        *,
        expected_base_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Wholesale-write a new immutable snapshot version and switch pointer."""
        conn = self.db._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT version, manifest_hash FROM current_snapshot WHERE clip_id = ?",
                (clip_id,),
            ).fetchone()
            current = int(row["version"]) if row else 0
            if expected_base_version is not None and int(expected_base_version) != current:
                raise KFError(
                    E_SNAPSHOT_ROLLBACK,
                    "stale writer rejected: snapshot base version is no longer current",
                    {
                        "clip_id": clip_id,
                        "expected_base_version": expected_base_version,
                        "current_version": current,
                        "action": "reload current snapshot and retry; state not overwritten",
                    },
                )
            new_version = current + 1
            manifest_body = {
                "clip_id": clip_id,
                "version": new_version,
                "revision": int(revision),
                "parent_version": current or None,
                "committed_at": time.time(),
                **body,
            }
            manifest_hash = hash_json(manifest_body)
            record = self.store.write_json(manifest_body, "snapshot_manifest")
            self.db.record_object({
                "hash": record["hash"], "kind": "snapshot_manifest",
                "path": record["path"], "size_bytes": record["size_bytes"],
            })

            manifest = dict(manifest_body)
            manifest["manifest_hash"] = manifest_hash
            version_dir = self._write_version_tree(clip_id, new_version, manifest)

            conn.execute(
                "INSERT INTO snapshots(clip_id, version, revision, manifest_hash, plan_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (clip_id, new_version, int(revision), manifest_hash,
                 manifest.get("plan_hash"), time.time()),
            )
            conn.execute(
                "INSERT INTO current_snapshot(clip_id, version, revision, manifest_hash, committed_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(clip_id) DO UPDATE SET "
                "version=excluded.version, revision=excluded.revision, "
                "manifest_hash=excluded.manifest_hash, committed_at=excluded.committed_at",
                (clip_id, new_version, int(revision), manifest_hash, time.time()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        pointer = self._clip_dir(clip_id) / "current.json"
        fd, tmp_name = tempfile.mkstemp(dir=str(pointer.parent), prefix=".current-")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"clip_id": clip_id, "version": new_version,
                       "manifest_hash": manifest_hash}, handle)
        os.replace(tmp_name, pointer)
        return manifest

    def _write_version_tree(self, clip_id: str, version: int, manifest: Dict[str, Any]) -> Path:
        version_dir = self._clip_dir(clip_id) / f"v{version}"
        artifacts_dir = version_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (version_dir / "manifest.json").write_text(
            canonical_json(manifest), encoding="utf-8"
        )
        for artifact in manifest.get("artifacts", []):
            digest = artifact["content_hash"]
            source = self.store._path_for(digest)
            target = artifacts_dir / f"{artifact['name']}-{digest[:12]}.bin"
            if source.exists() and not target.exists():
                try:
                    os.link(source, target)
                except OSError:
                    target.write_bytes(source.read_bytes())
        return version_dir

    def list_versions(self, clip_id: str) -> List[Dict[str, Any]]:
        rows = self.db._connect().execute(
            "SELECT version, revision, manifest_hash, plan_hash, created_at "
            "FROM snapshots WHERE clip_id = ? ORDER BY version",
            (clip_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def request_rollback(self, clip_id: str, target_version: int) -> None:
        """Forward-only repository: moving the current pointer backwards is rejected."""
        current = self.current_version(clip_id)
        if target_version < current:
            raise KFError(
                E_SNAPSHOT_ROLLBACK,
                "backward snapshot rollback rejected; old versions must not overwrite new ones",
                {
                    "clip_id": clip_id,
                    "requested_version": target_version,
                    "current_version": current,
                    "hint": "use restore_version_forward to republish old content as a new version",
                },
            )
        raise KFError(
            E_SNAPSHOT_ROLLBACK,
            "rollback request does not target an older version",
            {"clip_id": clip_id, "requested_version": target_version, "current_version": current},
        )

    def restore_version_forward(self, clip_id: str, target_version: int) -> Dict[str, Any]:
        """Republish an old manifest's content as a strictly new version."""
        rows = self.list_versions(clip_id)
        match = next((row for row in rows if row["version"] == target_version), None)
        if match is None:
            raise KFError(E_NOT_FOUND, f"snapshot v{target_version} not found for {clip_id}")
        old_manifest = self.store.read_json(match["manifest_hash"])
        body = {k: v for k, v in old_manifest.items()
                if k not in ("clip_id", "version", "revision", "parent_version",
                             "committed_at", "manifest_hash")}
        body["restored_from_version"] = target_version
        return self.commit_new(clip_id, old_manifest["revision"], body)

    def verify_current(self, clip_id: str) -> Dict[str, Any]:
        manifest = self.current_manifest(clip_id)
        if manifest is None:
            raise KFError(E_NOT_FOUND, f"no snapshot for clip {clip_id}")
        self.store.verify(manifest["manifest_hash"])
        checked = 0
        for artifact in manifest.get("artifacts", []):
            self.store.verify(artifact["content_hash"])
            checked += 1
        return {"clip_id": clip_id, "version": manifest["version"],
                "artifacts_checked": checked, "ok": True}
