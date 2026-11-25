"""Atomic snapshots, staged cache publication, and non-blocking commit gate."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import (
    E_FORMAT_CORRUPT,
    E_RESOURCE_LIMIT,
    E_STAGE_BUSY,
    E_STATE_INCONSISTENT,
    EngineError,
)
from .util import (
    atomic_write_bytes,
    canonical_json_bytes,
    sha256_bytes,
)


STAGES = ("envelope", "align", "drift", "report")


class EngineStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.payload_dir = self.root / "payloads"
        self.payload_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = self.root / "engine.sqlite3"
        self._initialize()
        self._discard_uncommitted_cache()
        self._remove_orphan_payloads()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=0.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    version INTEGER PRIMARY KEY,
                    stage TEXT NOT NULL,
                    input_fingerprint TEXT NOT NULL,
                    completed_utterances TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    payload_path TEXT NOT NULL,
                    stage_token TEXT NOT NULL,
                    previous_version INTEGER,
                    business_code TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS snapshots_stage_fingerprint
                    ON snapshots(stage, input_fingerprint);
                CREATE TABLE IF NOT EXISTS cache_entries (
                    key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    pcm_sha256 TEXT NOT NULL,
                    rule_version TEXT NOT NULL,
                    frame_length INTEGER NOT NULL,
                    frame_shift INTEGER NOT NULL,
                    upstream_hash TEXT NOT NULL,
                    stage_token TEXT,
                    committed INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS cache_entries_stage_token
                    ON cache_entries(stage_token);
                """
            )
            connection.commit()
        finally:
            connection.close()

    def _discard_uncommitted_cache(self) -> None:
        connection = self._connect()
        try:
            connection.execute("DELETE FROM cache_entries WHERE committed = 0")
            connection.commit()
        finally:
            connection.close()

    def _remove_orphan_payloads(self) -> None:
        connection = self._connect()
        try:
            referenced = {
                str(row["payload_path"])
                for row in connection.execute("SELECT payload_path FROM snapshots")
            }
        finally:
            connection.close()
        for path in self.payload_dir.glob("*.json"):
            if str(path) not in referenced:
                try:
                    path.unlink()
                except OSError:
                    pass

    def latest_snapshot(
        self, stage: str | None = None, input_fingerprint: str | None = None
    ) -> dict[str, Any] | None:
        query = "SELECT * FROM snapshots"
        clauses: list[str] = []
        params: list[Any] = []
        if stage is not None:
            clauses.append("stage = ?")
            params.append(stage)
        if input_fingerprint is not None:
            clauses.append("input_fingerprint = ?")
            params.append(input_fingerprint)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY version DESC LIMIT 1"
        connection = self._connect()
        try:
            row = connection.execute(query, params).fetchone()
        finally:
            connection.close()
        return self._row_to_record(row) if row else None

    def latest_global(self) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM snapshots ORDER BY version DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        return self._row_to_record(row) if row else None

    def _row_to_record(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "version": int(row["version"]),
            "stage": row["stage"],
            "input_fingerprint": row["input_fingerprint"],
            "completed_utterances": sorted(json.loads(row["completed_utterances"])),
            "checksum": row["checksum"],
            "payload_path": row["payload_path"],
            "stage_token": row["stage_token"],
            "previous_version": row["previous_version"],
            "business_code": row["business_code"],
        }

    def load_snapshot_payload(self, record: dict[str, Any]) -> Any:
        path = Path(record["payload_path"])
        try:
            payload_bytes = path.read_bytes()
        except OSError as exc:
            raise EngineError(
                E_STATE_INCONSISTENT,
                "committed snapshot payload is missing",
                {"version": record["version"], "path": str(path)},
            ) from exc
        actual = sha256_bytes(payload_bytes)
        if actual != record["checksum"]:
            raise EngineError(
                E_STATE_INCONSISTENT,
                "committed snapshot checksum mismatch",
                {
                    "version": record["version"],
                    "expected": record["checksum"],
                    "actual": actual,
                },
            )
        try:
            return json.loads(payload_bytes)
        except json.JSONDecodeError as exc:
            raise EngineError(
                E_STATE_INCONSISTENT,
                "committed snapshot payload is not valid JSON",
                {"version": record["version"]},
            ) from exc

    def commit_snapshot(
        self,
        *,
        stage: str,
        input_fingerprint: str,
        completed_utterances: list[str],
        payload: Any,
        stage_token: str,
        business_code: str,
    ) -> dict[str, Any]:
        if stage not in STAGES:
            raise EngineError(E_FORMAT_CORRUPT, "unknown snapshot stage", {"stage": stage})
        payload_bytes = canonical_json_bytes(payload) + b"\n"
        checksum = sha256_bytes(payload_bytes)
        payload_path = self.payload_dir / f"{checksum}.json"
        if not payload_path.exists():
            atomic_write_bytes(payload_path, payload_bytes)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM snapshots WHERE stage = ? AND input_fingerprint = ?",
                (stage, input_fingerprint),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                return self._row_to_record(existing)
            row = connection.execute("SELECT COALESCE(MAX(version), 0) AS version FROM snapshots").fetchone()
            version = int(row["version"]) + 1
            previous = connection.execute(
                "SELECT MAX(version) AS version FROM snapshots"
            ).fetchone()["version"]
            connection.execute(
                """
                UPDATE cache_entries
                SET committed = 1, stage_token = NULL
                WHERE stage_token = ? AND committed = 0
                """,
                (stage_token,),
            )
            connection.execute(
                """
                INSERT INTO snapshots(
                    version, stage, input_fingerprint, completed_utterances,
                    checksum, payload_path, stage_token, previous_version, business_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version,
                    stage,
                    input_fingerprint,
                    canonical_json_bytes(sorted(completed_utterances)).decode("utf-8"),
                    checksum,
                    str(payload_path),
                    stage_token,
                    int(previous) if previous is not None else None,
                    business_code,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {
            "version": version,
            "stage": stage,
            "input_fingerprint": input_fingerprint,
            "completed_utterances": sorted(completed_utterances),
            "checksum": checksum,
            "payload_path": str(payload_path),
            "stage_token": stage_token,
            "previous_version": int(previous) if previous is not None else None,
            "business_code": business_code,
        }

    def cache_lookup(self, key: str) -> Any | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT payload, payload_sha256 FROM cache_entries WHERE key = ?",
                (key,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        payload_text = row["payload"]
        if sha256_bytes(payload_text.encode("utf-8")) != row["payload_sha256"]:
            raise EngineError(
                E_STATE_INCONSISTENT,
                "cache entry checksum mismatch",
                {"key": key},
            )
        return json.loads(payload_text)

    def cache_count(self) -> int:
        connection = self._connect()
        try:
            return int(connection.execute("SELECT COUNT(*) AS count FROM cache_entries").fetchone()["count"])
        finally:
            connection.close()

    def uncommitted_cache_count(self) -> int:
        connection = self._connect()
        try:
            return int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM cache_entries WHERE committed = 0"
                ).fetchone()["count"]
            )
        finally:
            connection.close()

    def cache_put_staged(
        self,
        *,
        key: str,
        payload_text: str,
        payload_sha256: str,
        pcm_sha256: str,
        rule_version: str,
        frame_length: int,
        frame_shift: int,
        upstream_hash: str,
        stage_token: str,
        max_entries: int,
    ) -> bool:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT committed, stage_token, payload_sha256 FROM cache_entries WHERE key = ?",
                (key,),
            ).fetchone()
            if existing is not None:
                if existing["payload_sha256"] != payload_sha256:
                    connection.rollback()
                    raise EngineError(
                        E_STATE_INCONSISTENT,
                        "content-addressed cache key has conflicting payload",
                        {"key": key},
                    )
                if int(existing["committed"]) == 0 and existing["stage_token"] == stage_token:
                    connection.execute(
                        "UPDATE cache_entries SET payload = ?, committed = 0 WHERE key = ?",
                        (payload_text, key),
                    )
                    connection.commit()
                else:
                    connection.rollback()
                return False
            count = int(
                connection.execute("SELECT COUNT(*) AS count FROM cache_entries").fetchone()["count"]
            )
            if count >= max_entries:
                connection.rollback()
                raise EngineError(
                    E_RESOURCE_LIMIT,
                    "cache entry budget exceeded",
                    {"limit": max_entries, "current": count, "key": key},
                )
            connection.execute(
                """
                INSERT INTO cache_entries(
                    key, payload, payload_sha256, pcm_sha256, rule_version,
                    frame_length, frame_shift, upstream_hash, stage_token, committed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    key,
                    payload_text,
                    payload_sha256,
                    pcm_sha256,
                    rule_version,
                    frame_length,
                    frame_shift,
                    upstream_hash,
                    stage_token,
                ),
            )
            connection.commit()
            return True
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def snapshot_history(self) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute("SELECT * FROM snapshots ORDER BY version").fetchall()
        finally:
            connection.close()
        return [self._row_to_record(row) for row in rows]


class CommitGate:
    """Linux advisory lock. Contenders fail immediately with E_STAGE_BUSY."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle: Any | None = None

    def acquire(self, *, stage: str, owner: str) -> None:
        if self._handle is not None:
            raise EngineError(E_STAGE_BUSY, "commit gate already held by this object")
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise EngineError(
                E_STAGE_BUSY,
                "another rule snapshot or drift adjudication is committing",
                {"stage": stage, "lock_path": str(self.path)},
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"stage": stage, "owner": owner}, sort_keys=True))
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    @contextmanager
    def held(self, *, stage: str, owner: str) -> Iterator[None]:
        self.acquire(stage=stage, owner=owner)
        try:
            yield
        finally:
            self.release()

    def __enter__(self) -> "CommitGate":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()

