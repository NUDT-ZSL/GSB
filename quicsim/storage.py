from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import (
    E_DB,
    E_NO_STATE,
    E_RECOVERY,
    E_SCENE_MISMATCH,
    EngineError,
)
from .model import hash_json
from .scene import Scene, canonical_json

SCHEMA_VERSION = "1"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scenes (
    scene_id TEXT PRIMARY KEY,
    scene_hash TEXT NOT NULL,
    scene_json TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    scene_id TEXT NOT NULL,
    scene_hash TEXT NOT NULL,
    started_at_ms INTEGER NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    watermark_event INTEGER NOT NULL,
    event_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    scene_hash TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    UNIQUE(session_id, version)
);

CREATE TABLE IF NOT EXISTS current_state (
    scene_id TEXT PRIMARY KEY,
    scene_hash TEXT NOT NULL,
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    watermark_event INTEGER NOT NULL,
    event_count INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS event_commits (
    session_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    event_hash TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    version INTEGER NOT NULL,
    committed_at_ms INTEGER NOT NULL,
    PRIMARY KEY(session_id, event_seq)
);

CREATE TABLE IF NOT EXISTS trace_events (
    run_id TEXT NOT NULL,
    path TEXT NOT NULL CHECK(path IN ('incremental', 'full')),
    event_seq INTEGER NOT NULL,
    time_ms INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    PRIMARY KEY(run_id, path, event_seq)
);

CREATE TABLE IF NOT EXISTS trace_verifications (
    run_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    consistent INTEGER NOT NULL,
    detail_json TEXT,
    PRIMARY KEY(run_id, event_seq)
);

CREATE TABLE IF NOT EXISTS rounding_ledger (
    session_id TEXT NOT NULL,
    entry_seq INTEGER NOT NULL,
    event_seq INTEGER NOT NULL,
    entry_json TEXT NOT NULL,
    entry_hash TEXT NOT NULL,
    PRIMARY KEY(session_id, entry_seq)
);

CREATE TABLE IF NOT EXISTS divergence_reports (
    report_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_id TEXT NOT NULL,
    session_id TEXT,
    version INTEGER,
    event_seq INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    detail_json TEXT NOT NULL
);
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


@contextmanager
def file_lock(db_path: str | Path, exclusive: bool = True) -> Iterator[None]:
    lock_path = Path(f"{db_path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(fd, mode)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    row = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()
    if row is None or row["value"] != SCHEMA_VERSION:
        raise EngineError(
            E_DB,
            "SQLite schema version is unsupported",
            details={"found": None if row is None else row["value"], "expected": SCHEMA_VERSION},
        )


@contextmanager
def connect_write(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        ensure_schema(conn)
        yield conn
    finally:
        conn.close()


@contextmanager
def connect_read(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    path = Path(db_path)
    if not path.exists():
        raise EngineError(E_NO_STATE, f"database does not exist: {path}")
    uri = f"file:{path.resolve()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
    except sqlite3.Error as exc:
        raise EngineError(E_DB, f"cannot open database: {exc}") from None
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        yield conn
    finally:
        conn.close()


def begin_immediate(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")


def commit(conn: sqlite3.Connection) -> None:
    conn.execute("COMMIT")


def rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def ensure_scene_row(conn: sqlite3.Connection, scene: Scene) -> None:
    row = conn.execute(
        "SELECT scene_hash FROM scenes WHERE scene_id=?", (scene.scene_id,)
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO scenes(scene_id, scene_hash, scene_json, event_count, created_at_ms)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                scene.scene_id,
                scene.scene_hash,
                canonical_json(scene.to_source_dict()),
                scene.event_count,
                _now_ms(),
            ),
        )
        return
    if row["scene_hash"] != scene.scene_hash:
        raise EngineError(
            E_SCENE_MISMATCH,
            "stored scene differs from the requested scene",
            details={
                "scene_id": scene.scene_id,
                "stored_hash": row["scene_hash"],
                "requested_hash": scene.scene_hash,
            },
        )


def load_stored_scene(conn: sqlite3.Connection, scene_id: str) -> Scene:
    from .scene import scene_from_json

    row = conn.execute(
        "SELECT scene_json FROM scenes WHERE scene_id=?", (scene_id,)
    ).fetchone()
    if row is None:
        raise EngineError(
            E_NO_STATE,
            "no stored scene exists for the requested scene id",
            details={"scene_id": scene_id},
        )
    return scene_from_json(row["scene_json"])


def current_row(conn: sqlite3.Connection, scene_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM current_state WHERE scene_id=?", (scene_id,)
    ).fetchone()


def choose_current_row(
    conn: sqlite3.Connection, scene_id: str | None = None
) -> sqlite3.Row:
    if scene_id is not None:
        row = current_row(conn, scene_id)
        if row is None:
            raise EngineError(
                E_NO_STATE,
                "no current state exists for scene",
                details={"scene_id": scene_id},
            )
        return row
    rows = conn.execute(
        "SELECT * FROM current_state ORDER BY scene_id"
    ).fetchall()
    if not rows:
        raise EngineError(E_NO_STATE, "database has no current state")
    if len(rows) > 1:
        raise EngineError(
            E_NO_STATE,
            "multiple scenes exist; specify --scene-id",
            details={"scene_ids": [row["scene_id"] for row in rows]},
        )
    return rows[0]


def run_row(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()


def new_session_id() -> str:
    return uuid.uuid4().hex


def new_run_id() -> str:
    return uuid.uuid4().hex


def recover_committed_state(
    conn: sqlite3.Connection,
    scene: Scene,
    row: sqlite3.Row,
) -> dict[str, Any]:
    """Validate the committed watermark and roll back only provably orphaned metadata."""

    conn.execute(
        "UPDATE runs SET status='aborted' WHERE status='pending' AND scene_id=?",
        (scene.scene_id,),
    )
    run = run_row(conn, row["run_id"])
    if run is None:
        raise EngineError(
            E_RECOVERY,
            "current trace pointer references a missing run",
            details={"run_id": row["run_id"]},
        )
    watermark = int(row["watermark_event"])
    commits = conn.execute(
        """
        SELECT event_seq, event_hash, state_hash
        FROM event_commits WHERE session_id=?
        ORDER BY event_seq
        """,
        (row["session_id"],),
    ).fetchall()
    commit_sequences = [int(item["event_seq"]) for item in commits]
    if commit_sequences != list(range(1, watermark + 1)):
        raise EngineError(
            E_RECOVERY,
            "event commit ledger does not match the current logical-clock watermark",
            details={
                "session_id": row["session_id"],
                "watermark_event": watermark,
                "committed_sequences": commit_sequences,
            },
        )
    for item in commits:
        event_seq = int(item["event_seq"])
        if event_seq > len(scene.events) or item["event_hash"] != hash_json(
            scene.events[event_seq - 1]
        ):
            raise EngineError(
                E_RECOVERY,
                "committed event hash does not match the scene",
                details={
                    "session_id": row["session_id"],
                    "event_seq": event_seq,
                    "committed_hash": item["event_hash"],
                },
            )
    for path in ("incremental", "full"):
        trace_rows = conn.execute(
            """
            SELECT event_seq, snapshot_json, snapshot_hash
            FROM trace_events WHERE run_id=? AND path=?
            ORDER BY event_seq
            """,
            (row["run_id"], path),
        ).fetchall()
        sequences = [int(item["event_seq"]) for item in trace_rows]
        if sequences != list(range(1, watermark + 1)):
            raise EngineError(
                E_RECOVERY,
                "trace is incomplete for a committed watermark",
                details={
                    "run_id": row["run_id"],
                    "path": path,
                    "expected_events": watermark,
                    "stored_sequences": sequences,
                },
            )
        for item in trace_rows:
            if hash_json(json.loads(item["snapshot_json"])) != item["snapshot_hash"]:
                raise EngineError(
                    E_RECOVERY,
                    "persisted trace snapshot hash does not match its content",
                    details={
                        "run_id": row["run_id"],
                        "path": path,
                        "event_seq": item["event_seq"],
                    },
                )
    state = json.loads(row["state_json"])
    if hash_json(state) != row["state_hash"]:
        raise EngineError(
            E_RECOVERY,
            "persisted engine state hash does not match the stored state",
            details={"run_id": row["run_id"]},
        )
    if int(run["watermark_event"]) != watermark:
        raise EngineError(
            E_RECOVERY,
            "run watermark and current-state watermark disagree",
            details={
                "run_id": row["run_id"],
                "run_watermark": int(run["watermark_event"]),
                "state_watermark": watermark,
            },
        )
    return state


def insert_divergence_report(
    conn: sqlite3.Connection,
    *,
    scene_id: str,
    session_id: str | None,
    version: int | None,
    event_seq: int,
    detail: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO divergence_reports(
            scene_id, session_id, version, event_seq, created_at_ms, detail_json
        ) VALUES(?, ?, ?, ?, ?, ?)
        """,
        (
            scene_id,
            session_id,
            version,
            event_seq,
            _now_ms(),
            canonical_json(detail),
        ),
    )


def read_rounding_ledger(
    conn: sqlite3.Connection, session_id: str
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT entry_seq, event_seq, entry_json, entry_hash
        FROM rounding_ledger WHERE session_id=? ORDER BY entry_seq
        """,
        (session_id,),
    ).fetchall()


def read_trace(
    conn: sqlite3.Connection, run_id: str, path: str
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT event_seq, time_ms, snapshot_json, snapshot_hash
        FROM trace_events WHERE run_id=? AND path=? ORDER BY event_seq
        """,
        (run_id, path),
    ).fetchall()


def read_verifications(
    conn: sqlite3.Connection, run_id: str
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT event_seq, consistent, detail_json
        FROM trace_verifications WHERE run_id=? ORDER BY event_seq
        """,
        (run_id,),
    ).fetchall()


def copy_trace_graph(
    conn: sqlite3.Connection, source_run_id: str | None, target_run_id: str
) -> None:
    if source_run_id is None:
        return
    conn.execute(
        """
        INSERT INTO trace_events(run_id, path, event_seq, time_ms, snapshot_json, snapshot_hash)
        SELECT ?, path, event_seq, time_ms, snapshot_json, snapshot_hash
        FROM trace_events WHERE run_id=?
        """,
        (target_run_id, source_run_id),
    )
    conn.execute(
        """
        INSERT INTO trace_verifications(run_id, event_seq, consistent, detail_json)
        SELECT ?, event_seq, consistent, detail_json
        FROM trace_verifications WHERE run_id=?
        """,
        (target_run_id, source_run_id),
    )


def insert_trace_event(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    path: str,
    event_seq: int,
    time_ms: int,
    snapshot: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO trace_events(
            run_id, path, event_seq, time_ms, snapshot_json, snapshot_hash
        ) VALUES(?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            path,
            event_seq,
            time_ms,
            canonical_json(snapshot),
            hash_json(snapshot),
        ),
    )


def insert_verification(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    event_seq: int,
    consistent: bool,
    detail: dict[str, Any] | None,
) -> None:
    conn.execute(
        """
        INSERT INTO trace_verifications(run_id, event_seq, consistent, detail_json)
        VALUES(?, ?, ?, ?)
        """,
        (
            run_id,
            event_seq,
            1 if consistent else 0,
            None if detail is None else canonical_json(detail),
        ),
    )


def insert_event_commit(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    scene_id: str,
    event_seq: int,
    event_hash: str,
    state_hash: str,
    version: int,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO event_commits(
            session_id, scene_id, event_seq, event_hash, state_hash, version, committed_at_ms
        ) VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            scene_id,
            event_seq,
            event_hash,
            state_hash,
            version,
            _now_ms(),
        ),
    )


def insert_rounding_entry(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    entry: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO rounding_ledger(
            session_id, entry_seq, event_seq, entry_json, entry_hash
        ) VALUES(?, ?, ?, ?, ?)
        """,
        (
            session_id,
            entry["seq"],
            entry["event_seq"],
            canonical_json(entry),
            hash_json(entry),
        ),
    )


def create_session(conn: sqlite3.Connection, scene: Scene) -> str:
    session_id = new_session_id()
    conn.execute(
        """
        INSERT INTO sessions(session_id, scene_id, scene_hash, started_at_ms, status)
        VALUES(?, ?, ?, ?, 'active')
        """,
        (session_id, scene.scene_id, scene.scene_hash, _now_ms()),
    )
    return session_id


def next_version(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS version FROM runs WHERE session_id=?",
        (session_id,),
    ).fetchone()
    return int(row["version"]) + 1


def insert_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    scene: Scene,
    session_id: str,
    version: int,
    watermark_event: int,
    status: str,
    state_hash: str,
) -> None:
    conn.execute(
        """
        INSERT INTO runs(
            run_id, session_id, scene_id, version, watermark_event, event_count,
            status, state_hash, scene_hash, created_at_ms
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            session_id,
            scene.scene_id,
            version,
            watermark_event,
            scene.event_count,
            status,
            state_hash,
            scene.scene_hash,
            _now_ms(),
        ),
    )


def replace_current_pointer(
    conn: sqlite3.Connection,
    *,
    scene: Scene,
    session_id: str,
    run_id: str,
    version: int,
    watermark_event: int,
    state: dict[str, Any],
    state_hash: str,
) -> None:
    conn.execute(
        """
        INSERT INTO current_state(
            scene_id, scene_hash, session_id, run_id, version, watermark_event,
            event_count, state_json, state_hash, updated_at_ms
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(scene_id) DO UPDATE SET
            scene_hash=excluded.scene_hash,
            session_id=excluded.session_id,
            run_id=excluded.run_id,
            version=excluded.version,
            watermark_event=excluded.watermark_event,
            event_count=excluded.event_count,
            state_json=excluded.state_json,
            state_hash=excluded.state_hash,
            updated_at_ms=excluded.updated_at_ms
        """,
        (
            scene.scene_id,
            scene.scene_hash,
            session_id,
            run_id,
            version,
            watermark_event,
            scene.event_count,
            canonical_json(state),
            state_hash,
            _now_ms(),
        ),
    )


def set_session_status(
    conn: sqlite3.Connection, session_id: str, status: str
) -> None:
    conn.execute(
        "UPDATE sessions SET status=? WHERE session_id=?", (status, session_id)
    )
