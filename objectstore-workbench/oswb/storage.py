"""Persistence core: local chunk/object files plus SQLite metadata.

This module owns the cross-module consistency contract. The TCP server is a
thin wrapper around :class:`Storage`, and the same class is used directly by
the in-process tests so behaviour under sockets and unit tests is identical.
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager

from .digest import digest_file, digest_bytes, normalize_algorithm
from .errors import (
    BusinessError,
    E_CHUNK,
    E_INTEGRITY,
    E_VERSION,
    E_AUDIT,
    E_REPLAY,
    E_STATE,
    E_NOT_FOUND,
)
from .logging_utils import get_logger

SESSION_STATES = ("created", "uploading", "verifying", "committed", "aborted")
TERMINAL_STATES = ("committed", "aborted")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
    object_key      TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL DEFAULT 0,
    object_path     TEXT,
    content_digest  TEXT,
    object_size     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS object_versions (
    object_key      TEXT NOT NULL,
    version         INTEGER NOT NULL,
    chunk_size      INTEGER NOT NULL,
    algorithm       TEXT NOT NULL,
    total_chunks    INTEGER NOT NULL,
    chunk_digests   TEXT NOT NULL,
    chunk_files     TEXT NOT NULL,
    labels          TEXT NOT NULL,
    content_digest  TEXT NOT NULL,
    object_size     INTEGER NOT NULL,
    session_id      TEXT,
    created_at      REAL NOT NULL,
    PRIMARY KEY (object_key, version)
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    object_key    TEXT NOT NULL,
    state         TEXT NOT NULL,
    total_chunks  INTEGER NOT NULL,
    chunk_size    INTEGER NOT NULL,
    algorithm     TEXT NOT NULL,
    labels        TEXT NOT NULL,
    expect_version INTEGER NOT NULL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS session_chunks (
    session_id   TEXT NOT NULL,
    chunk_index  INTEGER NOT NULL,
    chunk_digest TEXT NOT NULL,
    chunk_length INTEGER NOT NULL,
    chunk_file   TEXT NOT NULL,
    uploaded     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, chunk_index)
);
CREATE TABLE IF NOT EXISTS audit_runs (
    run_id     TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    ok         INTEGER NOT NULL,
    findings   TEXT NOT NULL
);
"""


def safe_key(object_key):
    """Map an object key onto a safe relative filesystem path."""
    if not isinstance(object_key, str) or not object_key.strip():
        raise BusinessError(E_STATE, "object key must be a non-empty string")
    parts = [part for part in re.split(r"[\\/]+", object_key.strip()) if part not in ("", ".", "..")]
    if not parts:
        raise BusinessError(E_STATE, "object key has no usable path component")
    if any(not re.fullmatch(r"[A-Za-z0-9._\-]+", part) for part in parts):
        raise BusinessError(E_STATE, "object key contains unsupported characters")
    return os.path.join(*parts)


def ordered_chunk_digest(chunk_entries, algorithm):
    """Stable digest over the full ordered chunk byte stream description."""
    hasher = hashlib.new(normalize_algorithm(algorithm))
    for entry in sorted(chunk_entries, key=lambda item: item["index"]):
        hasher.update(str(entry["index"]).encode("ascii"))
        hasher.update(b":")
        hasher.update(entry["digest"].encode("ascii"))
        hasher.update(b";")
    return hasher.hexdigest()


def chunk_set_digest(chunk_entries, algorithm):
    """Digest of the *set* of referenced chunk files, order-independent."""
    hasher = hashlib.new(normalize_algorithm(algorithm))
    for entry in sorted(chunk_entries, key=lambda item: item["chunk_file"]):
        hasher.update(entry["chunk_file"].encode("utf-8"))
        hasher.update(b":")
        hasher.update(entry["digest"].encode("ascii"))
        hasher.update(b";")
    return hasher.hexdigest()


class Storage:
    """File + SQLite backed storage service implementation."""

    def __init__(self, base_dir, logger=None):
        self.base_dir = os.path.abspath(base_dir)
        self.db_dir = os.path.join(self.base_dir, "db")
        self.chunk_dir = os.path.join(self.base_dir, "chunks")
        self.object_dir = os.path.join(self.base_dir, "objects")
        self.snapshot_dir = os.path.join(self.base_dir, "snapshots")
        for directory in (self.db_dir, self.chunk_dir, self.object_dir, self.snapshot_dir):
            os.makedirs(directory, exist_ok=True)
        self.db_path = os.path.join(self.db_dir, "workbench.sqlite3")
        self.logger = logger or get_logger("oswb.server")
        self._lock = threading.RLock()
        self._chunk_locks = {}
        self._session_locks = {}
        self._init_db()

    # ------------------------------------------------------------------ infra
    @contextmanager
    def _db(self):
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init_db(self):
        with self._db() as connection:
            connection.executescript(_SCHEMA)

    def _chunk_lock(self, session_id, index):
        key = (session_id, index)
        with self._lock:
            lock = self._chunk_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._chunk_locks[key] = lock
            return lock

    def _session_lock(self, session_id):
        with self._lock:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_id] = lock
            return lock

    def _chunk_path(self, session_id, index):
        safe_session = re.fullmatch(r"[A-Za-z0-9._\-]+", session_id).group(0)
        session_dir = os.path.join(self.chunk_dir, safe_session)
        os.makedirs(session_dir, exist_ok=True)
        return os.path.join(session_dir, f"chunk-{index:08d}.part")

    def _object_path(self, object_key):
        relative = safe_key(object_key)
        target = os.path.join(self.object_dir, relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        return target

    # ---------------------------------------------------------------- sessions
    def create_session(self, object_key, manifest, labels=None, session_id=None,
                       expect_version=None):
        """Persist a new upload session and its planned chunk manifest."""
        safe_key(object_key)
        labels = labels or {}
        if not isinstance(labels, dict):
            raise BusinessError(E_STATE, "labels must be an object")
        session_id = session_id or uuid.uuid4().hex
        with self._lock, self._db() as connection:
            existing = connection.execute(
                "SELECT session_id FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if existing:
                return self.get_session(session_id)
            current = connection.execute(
                "SELECT current_version FROM objects WHERE object_key=?", (object_key,)
            ).fetchone()
            current_version = current["current_version"] if current else 0
            if expect_version is None:
                expect_version = current_version
            if expect_version != current_version:
                raise BusinessError(
                    E_VERSION,
                    "object metadata version conflict at session creation",
                    {"object_key": object_key, "expected": expect_version,
                     "current": current_version},
                )
            now = _now()
            connection.execute(
                "INSERT INTO sessions(session_id, object_key, state, total_chunks,"
                " chunk_size, algorithm, labels, expect_version, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (session_id, object_key, "created", manifest["total_chunks"],
                 manifest["chunk_size"], manifest["algorithm"],
                 json.dumps(labels, sort_keys=True), expect_version, now, now),
            )
            connection.executemany(
                "INSERT INTO session_chunks(session_id, chunk_index, chunk_digest,"
                " chunk_length, chunk_file, uploaded) VALUES(?,?,?,?,?,0)",
                [
                    (session_id, chunk["index"], chunk["digest"], chunk["length"],
                     os.path.relpath(self._chunk_path(session_id, chunk["index"]),
                                     self.base_dir).replace(os.sep, "/"))
                    for chunk in manifest["chunks"]
                ],
            )
        self._log("session_created", session_id=session_id, object_key=object_key,
                  total_chunks=manifest["total_chunks"], expect_version=expect_version)
        return self.get_session(session_id)

    def get_session(self, session_id):
        with self._db() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if not row:
                raise BusinessError(E_NOT_FOUND, "unknown session",
                                    {"session_id": session_id})
            chunks = connection.execute(
                "SELECT * FROM session_chunks WHERE session_id=? ORDER BY chunk_index",
                (session_id,),
            ).fetchall()
        return self._session_view(row, chunks)

    def list_sessions(self, state=None):
        with self._db() as connection:
            if state:
                rows = connection.execute(
                    "SELECT session_id FROM sessions WHERE state=? ORDER BY session_id",
                    (state,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT session_id FROM sessions ORDER BY session_id"
                ).fetchall()
        return [self.get_session(row["session_id"]) for row in rows]

    def _session_view(self, row, chunk_rows):
        completed = [item["chunk_index"] for item in chunk_rows if item["uploaded"]]
        return {
            "session_id": row["session_id"],
            "object_key": row["object_key"],
            "state": row["state"],
            "total_chunks": row["total_chunks"],
            "chunk_size": row["chunk_size"],
            "algorithm": row["algorithm"],
            "labels": json.loads(row["labels"]),
            "expect_version": row["expect_version"],
            "completed": completed,
            "chunks": [
                {
                    "index": item["chunk_index"],
                    "digest": item["chunk_digest"],
                    "length": item["chunk_length"],
                    "chunk_file": item["chunk_file"],
                    "uploaded": bool(item["uploaded"]),
                }
                for item in chunk_rows
            ],
        }

    def _transition(self, connection, session_id, allowed, target):
        row = connection.execute(
            "SELECT state FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if not row:
            raise BusinessError(E_NOT_FOUND, "unknown session",
                                {"session_id": session_id})
        if row["state"] not in allowed:
            raise BusinessError(
                E_STATE,
                f"illegal session transition {row['state']} -> {target}",
                {"session_id": session_id, "from": row["state"], "to": target},
            )
        connection.execute(
            "UPDATE sessions SET state=?, updated_at=? WHERE session_id=?",
            (target, _now(), session_id),
        )
        return row["state"]

    def abort_session(self, session_id):
        with self._lock, self._db() as connection:
            self._transition(connection, session_id,
                             ("created", "uploading", "verifying"), "aborted")
        self._log("session_aborted", session_id=session_id)
        return self.get_session(session_id)

    # ------------------------------------------------------------- chunk upload
    def upload_chunk(self, session_id, index, data):
        """Persist one chunk idempotently and reconcile duplicate uploads."""
        with self._lock:
            chunk_lock = self._chunk_lock(session_id, index)
            session_lock = self._session_lock(session_id)
        with session_lock, chunk_lock:
            with self._db() as connection:
                row = connection.execute(
                    "SELECT * FROM session_chunks WHERE session_id=? AND chunk_index=?",
                    (session_id, index),
                ).fetchone()
                if not row:
                    raise BusinessError(E_NOT_FOUND, "unknown chunk",
                                        {"session_id": session_id, "index": index})
                session = connection.execute(
                    "SELECT state FROM sessions WHERE session_id=?", (session_id,)
                ).fetchone()
                if session["state"] in ("committed", "aborted"):
                    raise BusinessError(
                        E_STATE,
                        f"cannot upload to {session['state']} session",
                        {"session_id": session_id, "state": session["state"]},
                    )
                if row["uploaded"]:
                    # Concurrent duplicate uploads converge to the single stored result.
                    return {
                        "session_id": session_id,
                        "index": index,
                        "deduplicated": True,
                        "digest": row["chunk_digest"],
                    }
                if len(data) != row["chunk_length"]:
                    raise BusinessError(
                        E_CHUNK,
                        "chunk length does not match registered manifest",
                        {"index": index, "expected": row["chunk_length"],
                         "actual": len(data)},
                    )
                actual_digest = digest_bytes(data, self._algorithm_of(connection, session_id))
                if actual_digest != row["chunk_digest"]:
                    raise BusinessError(
                        E_INTEGRITY,
                        "chunk digest does not match registered manifest",
                        {"index": index, "registered": row["chunk_digest"],
                         "actual": actual_digest},
                    )
                target = os.path.join(self.base_dir, row["chunk_file"])
                os.makedirs(os.path.dirname(target), exist_ok=True)
                _atomic_write(target, data)
                connection.execute(
                    "UPDATE session_chunks SET uploaded=1 WHERE session_id=? AND chunk_index=?",
                    (session_id, index),
                )
                if session["state"] == "created":
                    self._transition(connection, session_id, ("created",), "uploading")
                elif session["state"] != "uploading":
                    raise BusinessError(
                        E_STATE,
                        f"cannot accept chunk in {session['state']} state",
                        {"session_id": session_id, "state": session["state"]},
                    )
            self._log("chunk_uploaded", session_id=session_id, index=index,
                      length=len(data), digest=actual_digest)
            return {
                "session_id": session_id,
                "index": index,
                "deduplicated": False,
                "digest": actual_digest,
            }

    def _algorithm_of(self, connection, session_id):
        row = connection.execute(
            "SELECT algorithm FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        return row["algorithm"]

    def verify_session(self, session_id):
        """Re-hash every persisted chunk and compare with registered metadata."""
        with self._lock, self._db() as connection:
            session = connection.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if not session:
                raise BusinessError(E_NOT_FOUND, "unknown session",
                                    {"session_id": session_id})
            if session["state"] in ("committed", "aborted"):
                raise BusinessError(E_STATE, "session already terminal",
                                    {"state": session["state"]})
            chunks = connection.execute(
                "SELECT * FROM session_chunks WHERE session_id=? ORDER BY chunk_index",
                (session_id,),
            ).fetchall()
            missing = [item["chunk_index"] for item in chunks if not item["uploaded"]]
            if missing:
                raise BusinessError(
                    E_INTEGRITY,
                    "session has chunks that were never uploaded",
                    {"session_id": session_id, "missing": missing},
                )
            for item in chunks:
                path = os.path.join(self.base_dir, item["chunk_file"])
                if not os.path.exists(path):
                    raise BusinessError(
                        E_INTEGRITY,
                        "persisted chunk data file is missing",
                        {"index": item["chunk_index"], "chunk_file": item["chunk_file"]},
                    )
                actual_size = os.path.getsize(path)
                if actual_size != item["chunk_length"]:
                    raise BusinessError(
                        E_CHUNK,
                        "persisted chunk length is inconsistent (truncated data)",
                        {"index": item["chunk_index"], "expected": item["chunk_length"],
                         "actual": actual_size},
                    )
                actual_digest = digest_file(path, session["algorithm"])
                if actual_digest != item["chunk_digest"]:
                    raise BusinessError(
                        E_INTEGRITY,
                        "persisted chunk digest is inconsistent",
                        {"index": item["chunk_index"],
                         "registered": item["chunk_digest"], "actual": actual_digest},
                    )
            self._transition(connection, session_id,
                             ("created", "uploading", "verifying"), "verifying")
        self._log("session_verified", session_id=session_id)
        return self.get_session(session_id)

    # ------------------------------------------------------------- commit/merge
    def commit_session(self, session_id, expect_version=None):
        """Verify, merge chunks into a visible object, then publish metadata.

        The object bytes are written and fsynced first; the completion marker
        and metadata version are published in one SQLite transaction so a
        committed marker never exists without visible object content.
        """
        with self._lock:
            try:
                self.verify_session(session_id)
            except BusinessError:
                # Keep the session alive for resume on integrity failures.
                self._mark_verifying_failed(session_id)
                raise
            with self._db() as connection:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE session_id=?", (session_id,)
                ).fetchone()
                object_key = session["object_key"]
                current = connection.execute(
                    "SELECT current_version FROM objects WHERE object_key=?",
                    (object_key,),
                ).fetchone()
                current_version = current["current_version"] if current else 0
                expected = session["expect_version"] if expect_version is None else expect_version
                if expected != current_version:
                    self._transition(connection, session_id, ("verifying",), "uploading")
                    raise BusinessError(
                        E_VERSION,
                        "object metadata version conflict during commit",
                        {"object_key": object_key, "expected": expected,
                         "current": current_version},
                    )
                chunks = connection.execute(
                    "SELECT * FROM session_chunks WHERE session_id=? ORDER BY chunk_index",
                    (session_id,),
                ).fetchall()
                content_digest = self._merge_object(connection, session, chunks)
                new_version = current_version + 1
                chunk_digests = [item["chunk_digest"] for item in chunks]
                chunk_files = [item["chunk_file"] for item in chunks]
                object_size = sum(item["chunk_length"] for item in chunks)
                connection.execute(
                    "INSERT INTO object_versions(object_key, version, chunk_size,"
                    " algorithm, total_chunks, chunk_digests, chunk_files, labels,"
                    " content_digest, object_size, session_id, created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (object_key, new_version, session["chunk_size"],
                     session["algorithm"], len(chunks),
                     json.dumps(chunk_digests), json.dumps(chunk_files),
                     session["labels"], content_digest, object_size,
                     session_id, _now()),
                )
                connection.execute(
                    "INSERT INTO objects(object_key, current_version, object_path,"
                    " content_digest, object_size) VALUES(?,?,?,?,?)"
                    " ON CONFLICT(object_key) DO UPDATE SET current_version=excluded.current_version,"
                    " object_path=excluded.object_path, content_digest=excluded.content_digest,"
                    " object_size=excluded.object_size",
                    (object_key, new_version,
                     os.path.relpath(self._object_path(object_key), self.base_dir)
                     .replace(os.sep, "/"),
                     content_digest, object_size),
                )
                self._transition(connection, session_id, ("verifying",), "committed")
        self._log("session_committed", session_id=session_id, object_key=object_key,
                  version=new_version, content_digest=content_digest)
        return {
            "session_id": session_id,
            "object_key": object_key,
            "version": new_version,
            "content_digest": content_digest,
            "object_size": object_size,
        }

    def _mark_verifying_failed(self, session_id):
        with self._db() as connection:
            row = connection.execute(
                "SELECT state FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row and row["state"] == "verifying":
                connection.execute(
                    "UPDATE sessions SET state='uploading', updated_at=? WHERE session_id=?",
                    (_now(), session_id),
                )

    def _merge_object(self, connection, session, chunks):
        target = self._object_path(session["object_key"])
        tmp_target = target + ".tmp"
        hasher = hashlib.new(session["algorithm"])
        with open(tmp_target, "wb") as out:
            for item in chunks:
                source = os.path.join(self.base_dir, item["chunk_file"])
                with open(source, "rb") as handle:
                    while True:
                        block = handle.read(1024 * 1024)
                        if not block:
                            break
                        hasher.update(block)
                        out.write(block)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp_target, target)
        return hasher.hexdigest()

    # -------------------------------------------------------------- versions
    def get_object(self, object_key):
        with self._db() as connection:
            row = connection.execute(
                "SELECT * FROM objects WHERE object_key=?", (object_key,)
            ).fetchone()
            if not row:
                raise BusinessError(E_NOT_FOUND, "unknown object",
                                    {"object_key": object_key})
            view = dict(row)
            view["object_path_abs"] = os.path.join(self.base_dir, row["object_path"])
            return view

    def get_version(self, object_key, version):
        with self._db() as connection:
            row = connection.execute(
                "SELECT * FROM object_versions WHERE object_key=? AND version=?",
                (object_key, version),
            ).fetchone()
            if not row:
                raise BusinessError(E_NOT_FOUND, "unknown object version",
                                    {"object_key": object_key, "version": version})
            return self._version_view(row)

    def list_versions(self, object_key):
        with self._db() as connection:
            rows = connection.execute(
                "SELECT * FROM object_versions WHERE object_key=? ORDER BY version",
                (object_key,),
            ).fetchall()
        if not rows:
            raise BusinessError(E_NOT_FOUND, "unknown object",
                                {"object_key": object_key})
        return [self._version_view(row) for row in rows]

    def _version_view(self, row):
        return {
            "object_key": row["object_key"],
            "version": row["version"],
            "chunk_size": row["chunk_size"],
            "algorithm": row["algorithm"],
            "total_chunks": row["total_chunks"],
            "chunk_digests": json.loads(row["chunk_digests"]),
            "chunk_files": json.loads(row["chunk_files"]),
            "labels": json.loads(row["labels"]),
            "content_digest": row["content_digest"],
            "object_size": row["object_size"],
            "session_id": row["session_id"],
            "created_at": row["created_at"],
            "read_only": row["version"]
            != self._current_version(row["object_key"]),
        }

    def _current_version(self, object_key):
        with self._db() as connection:
            row = connection.execute(
                "SELECT current_version FROM objects WHERE object_key=?", (object_key,)
            ).fetchone()
            return row["current_version"] if row else None

    def diff_versions(self, object_key, left_version, right_version):
        """Stable field-level + chunk-manifest diff between two read-only versions."""
        left = self.get_version(object_key, left_version)
        right = self.get_version(object_key, right_version)
        field_changes = []
        for field in ("chunk_size", "algorithm", "total_chunks",
                      "content_digest", "object_size", "labels"):
            if left[field] != right[field]:
                field_changes.append(
                    {"field": field, "left": left[field], "right": right[field]}
                )
        left_chunks = [
            {"index": index, "digest": digest, "file": path}
            for index, (digest, path) in enumerate(
                zip(left["chunk_digests"], left["chunk_files"])
            )
        ]
        right_chunks = [
            {"index": index, "digest": digest, "file": path}
            for index, (digest, path) in enumerate(
                zip(right["chunk_digests"], right["chunk_files"])
            )
        ]
        left_map = {item["index"]: item for item in left_chunks}
        right_map = {item["index"]: item for item in right_chunks}
        chunk_changes = []
        for index in sorted(set(left_map) | set(right_map)):
            if index not in left_map:
                chunk_changes.append({"index": index, "change": "added",
                                      "right": right_map[index]})
            elif index not in right_map:
                chunk_changes.append({"index": index, "change": "removed",
                                      "left": left_map[index]})
            elif left_map[index] != right_map[index]:
                chunk_changes.append({"index": index, "change": "modified",
                                      "left": left_map[index],
                                      "right": right_map[index]})
        return {
            "object_key": object_key,
            "left_version": left_version,
            "right_version": right_version,
            "field_changes": field_changes,
            "chunk_changes": chunk_changes,
        }

    # ------------------------------------------------------------------ audit
    def audit(self, persist_run=True):
        """Full, order-stable comparison of metadata, chunk data and objects."""
        findings = []
        with self._db() as connection:
            objects = connection.execute(
                "SELECT * FROM objects ORDER BY object_key"
            ).fetchall()
            for obj in objects:
                object_key = obj["object_key"]
                version_row = connection.execute(
                    "SELECT * FROM object_versions WHERE object_key=? AND version=?",
                    (object_key, obj["current_version"]),
                ).fetchone()
                if not version_row:
                    findings.append({
                        "object_key": object_key,
                        "first_chunk_index": None,
                        "difference_type": "missing_current_version",
                    })
                    continue
                chunk_files = json.loads(version_row["chunk_files"])
                chunk_digests = json.loads(version_row["chunk_digests"])
                referenced = set(chunk_files)
                session_id = version_row["session_id"]
                on_disk = self._session_chunk_files(session_id)
                if referenced != on_disk:
                    findings.append({
                        "object_key": object_key,
                        "first_chunk_index": self._first_set_difference(
                            chunk_files, on_disk),
                        "difference_type": "chunk_file_set_mismatch",
                    })
                    continue
                mismatch = self._check_chunk_data(
                    object_key, session_id, chunk_files, chunk_digests,
                    version_row["algorithm"])
                if mismatch:
                    findings.append(mismatch)
                    continue
                object_path = os.path.join(self.base_dir, obj["object_path"])
                if not os.path.exists(object_path):
                    findings.append({
                        "object_key": object_key,
                        "first_chunk_index": None,
                        "difference_type": "object_content_missing",
                    })
                    continue
                actual_digest = digest_file(object_path, version_row["algorithm"])
                if actual_digest != version_row["content_digest"]:
                    findings.append({
                        "object_key": object_key,
                        "first_chunk_index": None,
                        "difference_type": "object_content_digest_mismatch",
                    })
                    continue
                if actual_digest != obj["content_digest"]:
                    findings.append({
                        "object_key": object_key,
                        "first_chunk_index": None,
                        "difference_type": "object_table_digest_mismatch",
                    })
        ok = not findings
        run_id = uuid.uuid4().hex
        if persist_run:
            with self._db() as connection:
                connection.execute(
                    "INSERT INTO audit_runs(run_id, created_at, ok, findings)"
                    " VALUES(?,?,?,?)",
                    (run_id, _now(), 1 if ok else 0,
                     json.dumps(findings, sort_keys=True)),
                )
        result = {"ok": ok, "run_id": run_id, "findings": findings,
                  "objects_audited": len(objects)}
        self._log("audit_completed", ok=ok, findings=len(findings))
        if not ok:
            raise BusinessError(E_AUDIT, "consistency audit found differences",
                                result)
        return result

    def _session_chunk_files(self, session_id):
        if not session_id:
            return set()
        session_dir = os.path.join(self.chunk_dir, session_id)
        if not os.path.isdir(session_dir):
            return set()
        return {
            os.path.relpath(os.path.join(session_dir, name), self.base_dir)
            .replace(os.sep, "/")
            for name in os.listdir(session_dir)
            if name.endswith(".part")
        }

    def _first_set_difference(self, chunk_files, on_disk):
        for index, path in enumerate(chunk_files):
            if path not in on_disk:
                return index
        return None

    def _check_chunk_data(self, object_key, session_id, chunk_files,
                          chunk_digests, algorithm):
        for index, (relative_path, registered_digest) in enumerate(
                zip(chunk_files, chunk_digests)):
            absolute = os.path.join(self.base_dir, relative_path)
            if not os.path.exists(absolute):
                return {"object_key": object_key, "first_chunk_index": index,
                        "difference_type": "chunk_data_missing"}
            actual_digest = digest_file(absolute, algorithm)
            if actual_digest != registered_digest:
                return {"object_key": object_key, "first_chunk_index": index,
                        "difference_type": "chunk_digest_mismatch"}
        return None

    # --------------------------------------------------------------- snapshots
    def create_snapshot(self, snapshot_id=None):
        """Persist sessions, object metadata, chunk/object bytes and audit result."""
        snapshot_id = snapshot_id or uuid.uuid4().hex
        snapshot_root = os.path.join(self.snapshot_dir, snapshot_id)
        if os.path.exists(snapshot_root):
            shutil.rmtree(snapshot_root)
        bundle_dir = os.path.join(snapshot_root, "bundle")
        os.makedirs(bundle_dir, exist_ok=True)
        for name in ("chunks", "objects"):
            source = os.path.join(self.base_dir, name)
            if os.path.isdir(source):
                shutil.copytree(source, os.path.join(bundle_dir, name))
        shutil.copy2(self.db_path, os.path.join(bundle_dir, "workbench.sqlite3"))

        audit = self._safe_audit()
        fingerprint = self.state_fingerprint()
        manifest = {
            "snapshot_id": snapshot_id,
            "created_at": _now(),
            "fingerprint": fingerprint,
            "audit": audit,
        }
        with open(os.path.join(snapshot_root, "snapshot.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, sort_keys=True, indent=2)
        self._log("snapshot_created", snapshot_id=snapshot_id,
                  fingerprint=fingerprint)
        return {"snapshot_id": snapshot_id,
                "path": snapshot_root,
                "fingerprint": fingerprint,
                "audit": audit}

    def _safe_audit(self):
        try:
            return self.audit(persist_run=False)
        except BusinessError as error:
            return error.detail

    def state_fingerprint(self):
        """Per-object metadata version + chunk-set digest, stable for comparison."""
        fingerprint = {}
        with self._db() as connection:
            objects = connection.execute(
                "SELECT * FROM objects ORDER BY object_key"
            ).fetchall()
            for obj in objects:
                version_row = connection.execute(
                    "SELECT * FROM object_versions WHERE object_key=? AND version=?",
                    (obj["object_key"], obj["current_version"]),
                ).fetchone()
                entries = [
                    {"chunk_file": path, "digest": digest}
                    for path, digest in zip(
                        json.loads(version_row["chunk_files"]),
                        json.loads(version_row["chunk_digests"]),
                    )
                ]
                fingerprint[obj["object_key"]] = {
                    "version": obj["current_version"],
                    "content_digest": obj["content_digest"],
                    "chunk_set_digest": chunk_set_digest(
                        entries, version_row["algorithm"]),
                }
        return fingerprint

    def restore_snapshot(self, snapshot_id):
        """Replace (never merge) the workbench state from a snapshot bundle."""
        snapshot_root = os.path.join(self.snapshot_dir, snapshot_id)
        manifest_path = os.path.join(snapshot_root, "snapshot.json")
        if not os.path.exists(manifest_path):
            raise BusinessError(E_NOT_FOUND, "unknown snapshot",
                                {"snapshot_id": snapshot_id})
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        bundle_dir = os.path.join(snapshot_root, "bundle")

        # Compare metadata version + chunk-set digest before touching state.
        staged_root = os.path.join(bundle_dir, "_staged_check")
        shutil.rmtree(staged_root, ignore_errors=True)
        try:
            os.makedirs(os.path.join(staged_root, "db"), exist_ok=True)
            shutil.copytree(os.path.join(bundle_dir, "chunks"),
                            os.path.join(staged_root, "chunks"))
            shutil.copytree(os.path.join(bundle_dir, "objects"),
                            os.path.join(staged_root, "objects"))
            shutil.copy2(os.path.join(bundle_dir, "workbench.sqlite3"),
                         os.path.join(staged_root, "db", "workbench.sqlite3"))
            staged = Storage(staged_root, logger=self.logger)
            staged_fingerprint = staged.state_fingerprint()
        except (OSError, BusinessError) as error:
            shutil.rmtree(staged_root, ignore_errors=True)
            raise BusinessError(
                E_REPLAY,
                "snapshot bundle cannot reproduce its recorded state",
                {"snapshot_id": snapshot_id, "cause": str(error)},
            )
        shutil.rmtree(staged_root, ignore_errors=True)
        if staged_fingerprint != manifest["fingerprint"]:
            raise BusinessError(
                E_REPLAY,
                "snapshot bundle does not reproduce its recorded fingerprint",
                {"snapshot_id": snapshot_id},
            )

        current_fingerprint = self.state_fingerprint()
        replaced = current_fingerprint != manifest["fingerprint"]
        # Whole-state replacement: wipe current data and copy the bundle back.
        for name in ("chunks", "objects"):
            target = os.path.join(self.base_dir, name)
            if os.path.isdir(target):
                shutil.rmtree(target)
            shutil.copytree(os.path.join(bundle_dir, name), target)
        shutil.copy2(os.path.join(bundle_dir, "workbench.sqlite3"), self.db_path)
        self._init_db()

        replay = self._safe_audit()
        if replay.get("ok") != manifest["audit"].get("ok") or replay.get(
                "findings") != manifest["audit"].get("findings"):
            raise BusinessError(
                E_REPLAY,
                "post-restore audit does not reproduce the snapshot conclusion",
                {"snapshot_id": snapshot_id, "snapshot_audit": manifest["audit"],
                 "replay_audit": replay},
            )
        self._log("snapshot_restored", snapshot_id=snapshot_id, replaced=replaced)
        return {"snapshot_id": snapshot_id, "replaced": replaced,
                "fingerprint": staged_fingerprint, "audit": replay}

    def list_snapshots(self):
        if not os.path.isdir(self.snapshot_dir):
            return []
        result = []
        for name in sorted(os.listdir(self.snapshot_dir)):
            manifest_path = os.path.join(self.snapshot_dir, name, "snapshot.json")
            if os.path.exists(manifest_path):
                with open(manifest_path, encoding="utf-8") as handle:
                    result.append(json.load(handle))
        return result

    # -------------------------------------------------------- fault injection
    def inject_chunk_truncation(self, session_id, index, keep_bytes=0):
        """Fault 1: truncate a persisted chunk data file (input data change only)."""
        path = self._require_chunk_file(session_id, index)
        with open(path, "r+b") as handle:
            handle.truncate(max(0, keep_bytes))
        self._log("fault_injected", fault="chunk_truncation",
                  session_id=session_id, index=index, keep_bytes=keep_bytes)
        return {"fault": "chunk_truncation", "session_id": session_id,
                "index": index, "path": path}

    def inject_metadata_digest_corruption(self, session_id, index):
        """Fault 2: corrupt the registered chunk digest inside metadata."""
        with self._db() as connection:
            row = connection.execute(
                "SELECT chunk_digest FROM session_chunks WHERE session_id=? AND chunk_index=?",
                (session_id, index),
            ).fetchone()
            if not row:
                raise BusinessError(E_NOT_FOUND, "unknown chunk",
                                    {"session_id": session_id, "index": index})
            corrupted = "0" * len(row["chunk_digest"])
            connection.execute(
                "UPDATE session_chunks SET chunk_digest=? WHERE session_id=? AND chunk_index=?",
                (corrupted, session_id, index),
            )
        self._log("fault_injected", fault="metadata_digest_corruption",
                  session_id=session_id, index=index)
        return {"fault": "metadata_digest_corruption",
                "session_id": session_id, "index": index,
                "corrupted_digest": corrupted}

    def bump_object_version(self, object_key):
        """Fault 3 helper: advance the current version externally to create conflict."""
        with self._db() as connection:
            row = connection.execute(
                "SELECT current_version FROM objects WHERE object_key=?",
                (object_key,),
            ).fetchone()
            if not row:
                raise BusinessError(E_NOT_FOUND, "unknown object",
                                    {"object_key": object_key})
            connection.execute(
                "UPDATE objects SET current_version=? WHERE object_key=?",
                (row["current_version"] + 1, object_key),
            )
        self._log("fault_injected", fault="version_conflict", object_key=object_key)
        return {"object_key": object_key,
                "forced_version": row["current_version"] + 1}

    def _require_chunk_file(self, session_id, index):
        with self._db() as connection:
            row = connection.execute(
                "SELECT chunk_file FROM session_chunks WHERE session_id=? AND chunk_index=?",
                (session_id, index),
            ).fetchone()
            if not row:
                raise BusinessError(E_NOT_FOUND, "unknown chunk",
                                    {"session_id": session_id, "index": index})
        path = os.path.join(self.base_dir, row["chunk_file"])
        if not os.path.exists(path):
            raise BusinessError(E_NOT_FOUND, "chunk data not uploaded yet",
                                {"session_id": session_id, "index": index})
        return path

    def _log(self, event, **context):
        from .logging_utils import log_event
        import logging
        log_event(self.logger, logging.INFO, event, **context)


def _now():
    import time
    return time.time()


def _atomic_write(path, data):
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def _replace_dir(target, source):
    os.makedirs(target, exist_ok=True)
    for name in ("chunks", "objects", "workbench.sqlite3"):
        src = os.path.join(source, name)
        dst = os.path.join(target, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        elif os.path.exists(src):
            shutil.copy2(src, dst)
