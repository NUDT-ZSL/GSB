"""SQLite persistence for coverage commits with resume support."""
from __future__ import annotations
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

from .generator import ENGINE_VERSION
from .models import PackagingProject
from .rasterizer import RasterResult
from .thumbnail import ThumbnailResult


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    seed INTEGER NOT NULL,
    version TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS commits (
    commit_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    version TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    parent_commit_id TEXT,
    engine_version TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    full_total REAL NOT NULL,
    thumb_total_scaled REAL NOT NULL,
    conservation_error REAL NOT NULL,
    coverage_blob BLOB NOT NULL,
    thumbnail_blob BLOB NOT NULL,
    contribution_blob BLOB NOT NULL,
    material_stats_json TEXT NOT NULL,
    thumbnail_meta_json TEXT NOT NULL,
    note TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(project_id)
);
CREATE TABLE IF NOT EXISTS recompute_jobs (
    job_id TEXT PRIMARY KEY,
    commit_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    version TEXT NOT NULL,
    request_json TEXT NOT NULL,
    report_json TEXT,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    finished_at REAL,
    FOREIGN KEY(commit_id) REFERENCES commits(commit_id)
);
"""


def content_hash(project: PackagingProject) -> str:
    import hashlib
    payload = json.dumps(
        project.to_dict(), sort_keys=True, ensure_ascii=False,
        default=lambda value: list(value) if isinstance(value, tuple) else value,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class StoredCommit:
    commit_id: str
    project_id: str
    version: str
    content_hash: str
    parent_commit_id: Optional[str]
    engine_version: str
    width: int
    height: int
    full_total: float
    thumb_total_scaled: float
    conservation_error: float
    material_stats: List[Dict]
    thumbnail_meta: Dict
    note: Optional[str]
    created_at: float


class CoverageStore:
    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        if not os.access(os.path.dirname(self.db_path), os.W_OK):
            raise PermissionError(f"database directory not writable: {self.db_path}")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
        self._conn.close()

    def __enter__(self) -> "CoverageStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _execute(self, sql: str, params=()):
        with self._lock:
            return self._conn.execute(sql, params)

    def _commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def _fetchone(self, sql: str, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _fetchall(self, sql: str, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def upsert_project(self, project: PackagingProject) -> None:
        self._execute(
            """INSERT INTO projects(project_id, seed, version, content_hash,
                                    payload_json, updated_at)
               VALUES(?, ?, ?, ?, ?, ?)
               ON CONFLICT(project_id) DO UPDATE SET
                 seed=excluded.seed, version=excluded.version,
                 content_hash=excluded.content_hash,
                 payload_json=excluded.payload_json,
                 updated_at=excluded.updated_at""",
            (project.project_id, project.seed, project.version,
             content_hash(project), json.dumps(project.to_dict()), time.time()),
        )
        self._commit()

    def load_project(self, project_id: str) -> PackagingProject:
        row = self._execute(
            "SELECT payload_json FROM projects WHERE project_id=?", (project_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown project {project_id}")
        return PackagingProject.from_dict(json.loads(row["payload_json"]))

    def latest_commit(self, project_id: str) -> Optional[StoredCommit]:
        row = self._execute(
            """SELECT * FROM commits WHERE project_id=?
               ORDER BY created_at DESC, commit_id DESC LIMIT 1""",
            (project_id,),
        ).fetchone()
        return self._row_to_commit(row) if row else None

    def list_commits(self, project_id: str) -> List[StoredCommit]:
        rows = self._execute(
            "SELECT * FROM commits WHERE project_id=? ORDER BY created_at",
            (project_id,),
        ).fetchall()
        return [self._row_to_commit(row) for row in rows]

    def assert_version_current(self, project: PackagingProject,
                               commit: Optional[StoredCommit]) -> None:
        """Reject stale coverage before a commit or a resume."""
        if commit is None:
            return
        digest = content_hash(project)
        if commit.content_hash != digest:
            raise ValueError(
                "version drift detected: stored coverage was computed for "
                f"content hash {commit.content_hash[:12]}, current project is "
                f"{digest[:12]}; full recompute required",
            )
        if commit.engine_version != ENGINE_VERSION:
            raise ValueError(
                f"engine version drift: stored {commit.engine_version} "
                f"vs current {ENGINE_VERSION}",
            )

    def save_commit(self, project: PackagingProject, result: RasterResult,
                    thumbnail: ThumbnailResult, material_stats: List[Dict],
                    note: str = "", commit_id: Optional[str] = None) -> StoredCommit:
        latest = self.latest_commit(project.project_id)
        self.upsert_project(project)
        import uuid
        commit_id = commit_id or f"cmt-{uuid.uuid4().hex[:12]}"
        coverage_blob = _encode_float_array(result.coverage)
        thumb_blob = _encode_float_array(thumbnail.averaged)
        contribution_blob = _encode_contributions(result.contributions)
        self._execute(
            """INSERT INTO commits(commit_id, project_id, version, content_hash,
                 parent_commit_id, engine_version, width, height, full_total,
                 thumb_total_scaled, conservation_error, coverage_blob,
                 thumbnail_blob, contribution_blob, material_stats_json,
                 thumbnail_meta_json, note, created_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (commit_id, project.project_id, project.version,
             content_hash(project), latest.commit_id if latest else None,
             ENGINE_VERSION, result.width, result.height,
             float(result.coverage.sum()),
             float(thumbnail.averaged.sum() * thumbnail.factor_x * thumbnail.factor_y),
             thumbnail.conservation_error,
             coverage_blob, thumb_blob, contribution_blob,
             json.dumps(material_stats),
             json.dumps({
                 "thumb_width": thumbnail.thumb_width,
                 "thumb_height": thumbnail.thumb_height,
                 "factor": [thumbnail.factor_x, thumbnail.factor_y],
                 "tolerance": thumbnail.tolerance,
                 "direct_total": thumbnail.direct_total,
                 "attenuation_pixels": int(thumbnail.attenuation.sum()),
                 "fracture_pixels": int(thumbnail.fracture.sum()),
             }),
             note, time.time()),
        )
        self._commit()
        return self.latest_commit(project.project_id)  # type: ignore[return-value]

    def load_commit_artifacts(self, commit_id: str
                              ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        row = self._execute(
            "SELECT coverage_blob, thumbnail_blob, contribution_blob "
            "FROM commits WHERE commit_id=?",
            (commit_id,),
        ).fetchone()
        if row is None:
            raise KeyError(commit_id)
        return (
            _decode_float_array(row["coverage_blob"]),
            _decode_float_array(row["thumbnail_blob"]),
            _decode_contributions(row["contribution_blob"]),
        )

    def create_job(self, project_id: str, commit_id: str, version: str,
                   request: Dict, job_id: Optional[str] = None) -> str:
        import uuid
        job_id = job_id or f"job-{uuid.uuid4().hex[:12]}"
        self._execute(
            """INSERT INTO recompute_jobs(job_id, commit_id, project_id, version,
                                          request_json, status, created_at)
               VALUES(?, ?, ?, ?, ?, 'running', ?)""",
            (job_id, commit_id, project_id, version,
             json.dumps(request), time.time()),
        )
        self._commit()
        return job_id

    def finish_job(self, job_id: str, report: Dict) -> None:
        self._execute(
            "UPDATE recompute_jobs SET report_json=?, status='done', "
            "finished_at=? WHERE job_id=?",
            (json.dumps(report), time.time(), job_id),
        )
        self._commit()

    def resume_jobs(self, project_id: str) -> List[sqlite3.Row]:
        return list(self._execute(
            "SELECT * FROM recompute_jobs WHERE project_id=? AND status='running'",
            (project_id,),
        ).fetchall())

    def _row_to_commit(self, row: sqlite3.Row) -> StoredCommit:
        return StoredCommit(
            commit_id=row["commit_id"],
            project_id=row["project_id"],
            version=row["version"],
            content_hash=row["content_hash"],
            parent_commit_id=row["parent_commit_id"],
            engine_version=row["engine_version"],
            width=row["width"],
            height=row["height"],
            full_total=row["full_total"],
            thumb_total_scaled=row["thumb_total_scaled"],
            conservation_error=row["conservation_error"],
            material_stats=json.loads(row["material_stats_json"]),
            thumbnail_meta=json.loads(row["thumbnail_meta_json"]),
            note=row["note"],
            created_at=row["created_at"],
        )


def _encode_float_array(array: np.ndarray) -> sqlite3.Binary:
    return sqlite3.Binary(
        np.asarray(array, dtype=np.float64).tobytes() +
        np.asarray(array.shape, dtype=np.int64).tobytes()
    )


def _decode_float_array(blob: bytes) -> np.ndarray:
    raw = bytes(blob)
    shape_bytes = len(raw) - 16
    values = np.frombuffer(raw[:shape_bytes], dtype=np.float64).copy()
    shape = tuple(np.frombuffer(raw[shape_bytes:], dtype=np.int64).tolist())
    return values.reshape(shape)


def _encode_contributions(contributions: np.ndarray) -> sqlite3.Binary:
    if len(contributions) == 0:
        return sqlite3.Binary(b"")
    return sqlite3.Binary(contributions.tobytes())


def _decode_contributions(blob: bytes) -> np.ndarray:
    raw = bytes(blob)
    if not raw:
        return np.zeros(0, dtype=_contribution_dtype())
    return np.frombuffer(raw, dtype=_contribution_dtype()).copy()


def _contribution_dtype() -> np.dtype:
    return np.dtype([
        ("curve_id", "U24"), ("x", np.int32), ("y", np.int32),
        ("area", np.float64), ("alpha", np.float64),
    ])
