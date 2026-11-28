from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.config import DB_PATH

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS athletes (
    athlete_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    specialty TEXT NOT NULL,
    training_years INTEGER NOT NULL CHECK (training_years >= 0),
    injury_tags TEXT NOT NULL,
    profile_snapshot TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS training_clips (
    clip_id TEXT PRIMARY KEY,
    athlete_id TEXT NOT NULL REFERENCES athletes(athlete_id),
    action_category TEXT NOT NULL,
    repetition_count INTEGER NOT NULL,
    duration_sec REAL NOT NULL,
    manual_quality_score REAL NOT NULL,
    sampling_rate_hz REAL NOT NULL,
    acceleration TEXT NOT NULL,
    angular_velocity TEXT NOT NULL,
    metadata TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ranking_requests (
    request_id TEXT PRIMARY KEY,
    athlete_id TEXT NOT NULL REFERENCES athletes(athlete_id),
    phase TEXT NOT NULL,
    weights TEXT NOT NULL,
    profile_snapshot TEXT NOT NULL,
    candidate_ids TEXT NOT NULL,
    prior_rank TEXT NOT NULL,
    config TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS request_candidates (
    request_id TEXT NOT NULL REFERENCES ranking_requests(request_id),
    clip_id TEXT NOT NULL REFERENCES training_clips(clip_id),
    prior_rank INTEGER NOT NULL,
    input_order INTEGER NOT NULL,
    PRIMARY KEY (request_id, clip_id)
);

CREATE TABLE IF NOT EXISTS ranking_results (
    request_id TEXT PRIMARY KEY REFERENCES ranking_requests(request_id),
    result TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_clips_athlete ON training_clips(athlete_id);
CREATE INDEX IF NOT EXISTS idx_requests_athlete_phase ON ranking_requests(athlete_id, phase);
CREATE INDEX IF NOT EXISTS idx_request_candidates_clip ON request_candidates(clip_id);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    db_path = Path(path or DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(path: str | Path | None = None, *, reset: bool = False) -> Path:
    db_path = Path(path or DB_PATH)
    if reset and db_path.exists():
        db_path.unlink()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db_path) + suffix)
            if sidecar.exists():
                sidecar.unlink()
    with connect(db_path) as connection:
        connection.executescript(SCHEMA)
    return db_path


@contextmanager
def transaction(path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    connection = connect(path)
    try:
        connection.execute("BEGIN")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def json_load(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


def fetch_athlete(connection: sqlite3.Connection, athlete_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM athletes WHERE athlete_id = ?", (athlete_id,)
    ).fetchone()


def fetch_clip(connection: sqlite3.Connection, clip_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM training_clips WHERE clip_id = ?", (clip_id,)
    ).fetchone()


def fetch_request(connection: sqlite3.Connection, request_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM ranking_requests WHERE request_id = ?", (request_id,)
    ).fetchone()
