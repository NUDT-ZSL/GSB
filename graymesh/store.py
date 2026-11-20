"""Versioned rule persistence on SQLite.

Commit protocol (single IMMEDIATE transaction):
  1. insert versions row with committed=0
  2. insert all objects of the new version
  3. insert invalidation events of the new version
  4. flip versions.committed=1 and move the meta current_version pointer
A crash anywhere before the commit ends rolls the whole transaction back, so
readers only ever observe a complete committed version. recover() additionally
removes any torn (committed=0) version rows and marks in-flight mirrors as
interrupted, without ever rolling back a committed version.
"""

import json
import os
import sqlite3
import threading

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

from .errors import BusinessError, ErrorCode
from .hashing import canonical_json, short_digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions(
  version INTEGER PRIMARY KEY,
  patch_id TEXT NOT NULL UNIQUE,
  committed INTEGER NOT NULL DEFAULT 0,
  created_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS objects(
  version INTEGER NOT NULL,
  kind TEXT NOT NULL,
  id TEXT NOT NULL,
  body TEXT NOT NULL,
  PRIMARY KEY(version, kind, id)
);
CREATE TABLE IF NOT EXISTS invalidations(
  version INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  body TEXT NOT NULL,
  PRIMARY KEY(version, seq)
);
CREATE TABLE IF NOT EXISTS decisions(
  request_seq INTEGER NOT NULL,
  version INTEGER NOT NULL,
  body TEXT NOT NULL,
  PRIMARY KEY(request_seq, version)
);
CREATE TABLE IF NOT EXISTS mirrors(
  mirror_seq INTEGER PRIMARY KEY,
  request_seq INTEGER NOT NULL,
  version INTEGER NOT NULL,
  state TEXT NOT NULL,
  body TEXT NOT NULL
);
"""


class Snapshot:
    """Immutable view of one committed rule version."""

    def __init__(self, version, objects):
        self.version = version
        self.objects = objects  # dict (kind, id) -> object
        canonical = {f"{k[0]}/{k[1]}": v for k, v in sorted(objects.items())}
        self.digest = short_digest(canonical)

    def get(self, kind, oid):
        return self.objects.get((kind, oid))


class FileLock:
    """Cross-process advisory lock guarding patch commits."""

    def __init__(self, path):
        self.path = path
        self._fh = None
        self._local = threading.Lock()

    def __enter__(self):
        self._local.acquire()
        self._fh = open(self.path, "a+b")
        if fcntl is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if fcntl is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()
        self._fh = None
        self._local.release()
        return False


class RuleStore:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        self.lock_path = self.db_path + ".lock"
        parent = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(parent, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
        self.recover()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    # ------------------------------------------------------------------ recovery
    def recover(self):
        """Idempotent startup recovery. Never touches committed versions."""
        with self._connect() as conn:
            torn = [r[0] for r in conn.execute(
                "SELECT version FROM versions WHERE committed=0").fetchall()]
            for version in torn:
                conn.execute("DELETE FROM objects WHERE version=?", (version,))
                conn.execute("DELETE FROM invalidations WHERE version=?", (version,))
                conn.execute("DELETE FROM versions WHERE version=?", (version,))
            # mirrors that were in-flight when the process died
            cur = conn.execute(
                "UPDATE mirrors SET state='interrupted' WHERE state='started'")
            interrupted = cur.rowcount
        return {"torn_versions_removed": torn, "mirrors_interrupted": interrupted}

    # ------------------------------------------------------------------ reads
    def current_version(self, conn=None):
        own = conn is None
        conn = conn or self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='current_version'").fetchone()
            return int(row[0]) if row else 0
        finally:
            if own:
                conn.close()

    def load_snapshot(self, version=None):
        with self._connect() as conn:
            if version is None:
                version = self.current_version(conn)
            rows = conn.execute(
                "SELECT kind, id, body FROM objects WHERE version=? ORDER BY kind, id",
                (version,)).fetchall()
        objects = {(kind, oid): json.loads(body) for kind, oid, body in rows}
        return Snapshot(version, objects)

    def latest_invalidation(self, version=None):
        with self._connect() as conn:
            if version is None:
                version = self.current_version(conn)
            rows = conn.execute(
                "SELECT body FROM invalidations WHERE version=? ORDER BY seq",
                (version,)).fetchall()
        return [json.loads(b) for (b,) in rows]

    def list_decisions(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT request_seq, version, body FROM decisions ORDER BY request_seq, version"
            ).fetchall()
        return [{"request_seq": s, "version": v, **json.loads(b)} for s, v, b in rows]

    def list_mirrors(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT mirror_seq, request_seq, version, state, body FROM mirrors "
                "ORDER BY mirror_seq").fetchall()
        return [{"mirror_seq": ms, "request_seq": rs, "version": v, "state": st,
                 **json.loads(b)} for ms, rs, v, st, b in rows]

    # ------------------------------------------------------------------ commit
    def commit_patch(self, patch_id, expected_version, new_objects, invalidation_events,
                     created_ms):
        """Atomically commit a new rule version.

        Returns (version, applied). applied=False means the patch_id was already
        committed before (idempotent replay) and nothing was written.
        """
        with FileLock(self.lock_path):
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT version FROM versions WHERE patch_id=?", (patch_id,)).fetchone()
                if row:
                    conn.execute("ROLLBACK")
                    return row[0], False
                current = self.current_version(conn)
                if expected_version is not None and expected_version != current:
                    conn.execute("ROLLBACK")
                    raise BusinessError(
                        ErrorCode.VERSION_MISMATCH,
                        "expected version %s but current is %s" % (expected_version, current),
                        {"expected": expected_version, "current": current},
                    )
                new_version = current + 1
                conn.execute(
                    "INSERT INTO versions(version, patch_id, committed, created_ms) "
                    "VALUES (?,?,0,?)", (new_version, patch_id, int(created_ms)))
                conn.executemany(
                    "INSERT INTO objects(version, kind, id, body) VALUES (?,?,?,?)",
                    [(new_version, kind, oid, canonical_json(obj))
                     for (kind, oid), obj in sorted(new_objects.items())])
                conn.executemany(
                    "INSERT INTO invalidations(version, seq, body) VALUES (?,?,?)",
                    [(new_version, seq, canonical_json(event))
                     for seq, event in enumerate(invalidation_events)])
                conn.execute("UPDATE versions SET committed=1 WHERE version=?", (new_version,))
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('current_version', ?)",
                    (str(new_version),))
                conn.execute("COMMIT")
                return new_version, True
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()

    # ------------------------------------------------------------------ records
    def record_decision(self, request_seq, version, chain):
        """Idempotent: same (request_seq, version) recorded at most once."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO decisions(request_seq, version, body) VALUES (?,?,?)",
                (int(request_seq), int(version), canonical_json(chain)))
            return cur.rowcount == 1

    def mirror_start(self, request_seq, version, body):
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO mirrors(request_seq, version, state, body) VALUES (?,?,?,?)",
                (int(request_seq), int(version), "started", canonical_json(body)))
            return cur.lastrowid

    def mirror_finish(self, mirror_seq, state, body):
        """Complete a mirror exactly once; interrupted/finished rows are untouched."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE mirrors SET state=?, body=? WHERE mirror_seq=? AND state='started'",
                (state, canonical_json(body), int(mirror_seq)))
            return cur.rowcount == 1
