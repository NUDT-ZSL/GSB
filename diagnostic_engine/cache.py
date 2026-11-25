"""Content-addressed JSON artifacts and SQLite query index."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable

import fcntl
import numpy as np

from .errors import stage_busy
from .hashing import canonical_json, read_json, sha256_json, write_json_atomic


def from_jsonable(value: Any) -> Any:
    if isinstance(value, dict) and value.get("__ndarray__") is True:
        return np.asarray(value["data"], dtype=np.dtype(value["dtype"]))
    if isinstance(value, dict):
        return {k: from_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_jsonable(v) for v in value]
    return value


def derive_cache_key(node: str, identity: dict[str, Any]) -> str:
    return sha256_json({"node": node, "identity": identity})


class CommitLock:
    """Exclusive non-blocking lock for one snapshot or incremental commit."""

    def __init__(self, lock_path: str | Path):
        self.lock_path = Path(lock_path)
        self._fd: int | None = None

    def __enter__(self) -> "CommitLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise stage_busy(
                "another stage snapshot or incremental commit is active",
                lock_path=str(self.lock_path),
            ) from exc
        self._fd = fd
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()}\n".encode("utf-8"))
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


class CacheStore:
    def __init__(self, root: str | Path, sqlite_path: str | Path):
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.tmp = self.root / "tmp"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.sqlite_path = Path(sqlite_path)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.sqlite_path, timeout=0.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    cache_key TEXT PRIMARY KEY,
                    node TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    path TEXT NOT NULL,
                    config_version TEXT,
                    precision_profile TEXT,
                    window_id TEXT,
                    committed INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS v_idx_index (
                    v_idx INTEGER PRIMARY KEY,
                    window_id TEXT NOT NULL,
                    src_name TEXT NOT NULL,
                    obs_time_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS traces (
                    trace_key TEXT PRIMARY KEY,
                    v_idx_list TEXT NOT NULL,
                    window_id TEXT NOT NULL,
                    config_version TEXT NOT NULL,
                    precision_profile TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )

    def object_path(self, cache_key: str) -> Path:
        return self.objects / cache_key[:2] / f"{cache_key}.json"

    def has(self, cache_key: str) -> bool:
        return self.object_path(cache_key).exists()

    def put(self, cache_key: str, node: str, body: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
        content_hash = sha256_json(body)
        envelope = {
            "cache_key": cache_key,
            "node": node,
            "content_hash": content_hash,
            "meta": meta,
            "body": body,
        }
        path = self.object_path(cache_key)
        if not path.exists():
            write_json_atomic(path, envelope)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO artifacts(cache_key,node,content_hash,path,config_version,
                                      precision_profile,window_id,committed)
                VALUES(?,?,?,?,?,?,?,0)
                ON CONFLICT(cache_key) DO NOTHING
                """,
                (
                    cache_key,
                    node,
                    content_hash,
                    str(path),
                    meta.get("config_version"),
                    meta.get("precision_profile"),
                    meta.get("window_id"),
                ),
            )
        return {"cache_key": cache_key, "content_hash": content_hash, "path": str(path)}

    def mark_committed(self, cache_keys: list[str]) -> None:
        if not cache_keys:
            return
        with self.connect() as conn:
            for key in cache_keys:
                conn.execute("UPDATE artifacts SET committed=1 WHERE cache_key=?", (key,))

    def get_envelope(self, cache_key: str) -> dict[str, Any]:
        path = self.object_path(cache_key)
        envelope = read_json(path)
        if envelope.get("content_hash") != sha256_json(envelope.get("body")):
            raise ValueError(f"cache object checksum mismatch: {cache_key}")
        return envelope

    def get_body(self, cache_key: str) -> dict[str, Any]:
        return from_jsonable(self.get_envelope(cache_key)["body"])

    def get_or_compute(
        self,
        cache_key: str,
        node: str,
        producer: Callable[[], dict[str, Any]],
        meta: dict[str, Any],
    ) -> tuple[dict[str, Any], str, str]:
        if self.has(cache_key):
            envelope = self.get_envelope(cache_key)
            return from_jsonable(envelope["body"]), "reused", envelope["content_hash"]
        body = producer()
        info = self.put(cache_key, node, body, meta)
        return body, "recomputed", info["content_hash"]

    def upsert_v_idx(self, v_idx: int, window_id: str, src_name: str, obs_time_ms: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO v_idx_index(v_idx,window_id,src_name,obs_time_ms)
                VALUES(?,?,?,?)
                ON CONFLICT(v_idx) DO UPDATE SET
                    window_id=excluded.window_id,
                    src_name=excluded.src_name,
                    obs_time_ms=excluded.obs_time_ms
                """,
                (int(v_idx), window_id, src_name, int(obs_time_ms)),
            )

    def save_trace(self, trace: dict[str, Any]) -> str:
        trace_key = str(trace["trace_key"])
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO traces(trace_key,v_idx_list,window_id,config_version,
                                   precision_profile,payload_json)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(trace_key) DO UPDATE SET payload_json=excluded.payload_json
                """,
                (
                    trace_key,
                    canonical_json(trace.get("v_idx", [])),
                    trace["window_id"],
                    trace["config_version"],
                    trace["precision"]["profile"],
                    canonical_json(trace),
                ),
            )
        return trace_key

    def query_v_idx(self, v_idx: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM v_idx_index WHERE v_idx=?", (int(v_idx),)).fetchone()
            if row is None:
                return None
            trace_row = conn.execute(
                """
                SELECT payload_json FROM traces
                WHERE window_id=? AND precision_profile=?
                ORDER BY rowid DESC LIMIT 1
                """,
                (row["window_id"], "float64"),
            ).fetchone()
        result = dict(row)
        if trace_row:
            result["trace"] = json.loads(trace_row["payload_json"])
        return result

    def query_v_idx_profile(self, v_idx: int, profile: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM v_idx_index WHERE v_idx=?", (int(v_idx),)).fetchone()
            if row is None:
                return None
            trace_row = conn.execute(
                "SELECT payload_json FROM traces WHERE window_id=? AND precision_profile=? ORDER BY rowid DESC LIMIT 1",
                (row["window_id"], profile),
            ).fetchone()
        result = dict(row)
        result["trace"] = json.loads(trace_row["payload_json"]) if trace_row else None
        return result

    def load_trace(self, trace_key: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT payload_json FROM traces WHERE trace_key=?", (trace_key,)).fetchone()
        if row is None:
            raise FileNotFoundError(trace_key)
        return json.loads(row["payload_json"])

    def gc(self, referenced_keys: set[str]) -> dict[str, int]:
        removed_objects = 0
        for path in self.objects.glob("*/*.json"):
            if path.stem not in referenced_keys:
                path.unlink()
                removed_objects += 1
        removed_temp = 0
        for path in self.root.rglob("*.tmp-*"):
            path.unlink()
            removed_temp += 1
        with self.connect() as conn:
            rows = conn.execute("SELECT cache_key FROM artifacts").fetchall()
            removed_rows = 0
            for row in rows:
                if row["cache_key"] not in referenced_keys:
                    conn.execute("DELETE FROM artifacts WHERE cache_key=?", (row["cache_key"],))
                    removed_rows += 1
        return {"removed_objects": removed_objects, "removed_temp": removed_temp, "removed_rows": removed_rows}
