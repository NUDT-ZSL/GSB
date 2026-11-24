from __future__ import annotations

import io
import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

from .io_utils import atomic_write_json, atomic_write_bytes, read_json_file, sha256_bytes, sha256_file


@dataclass
class CachedChunk:
    metadata: Dict[str, Any]
    arrays: Dict[str, np.ndarray]


class ChunkCache:
    """File-backed chunk cache.

    The JSON metadata is the commit marker.  A chunk is considered complete only
    when metadata parses and its payload SHA-256 matches.  Temporary files never
    use the final ``.json`` commit name.
    """

    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.chunk_dir = self.state_dir / "chunks"
        self.archive_dir = self.chunk_dir / "archive"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.chunk_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db_path = self.state_dir / "cache.sqlite3"
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def __enter__(self) -> "ChunkCache":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _init_db(self) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chunk_records (
                    chunk_id TEXT PRIMARY KEY,
                    chunk_index INTEGER,
                    global_version TEXT NOT NULL,
                    dependency_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    global_version TEXT NOT NULL,
                    exit_code INTEGER NOT NULL,
                    report_json TEXT,
                    created_order INTEGER NOT NULL
                )
                """
            )

    def paths_for(self, chunk_id: str) -> tuple[Path, Path]:
        return self.chunk_dir / f"{chunk_id}.npz", self.chunk_dir / f"{chunk_id}.json"

    def load_metadata(self, chunk_id: str) -> Optional[Dict[str, Any]]:
        _, metadata_path = self.paths_for(chunk_id)
        try:
            metadata = read_json_file(metadata_path)
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            return None
        if metadata.get("status") != "committed":
            return None
        if not metadata.get("global_version") or not metadata.get("dependency_digest"):
            return None
        return metadata

    def load_chunk(self, chunk_id: str) -> Optional[CachedChunk]:
        metadata = self.load_metadata(chunk_id)
        if metadata is None:
            return None
        payload_path, _ = self.paths_for(chunk_id)
        if not payload_path.is_file():
            return None
        try:
            actual_sha = sha256_file(payload_path)
            if actual_sha != metadata.get("payload_sha256"):
                return None
            with np.load(payload_path, allow_pickle=False) as npz:
                arrays = {key: np.asarray(npz[key]) for key in npz.files}
        except (OSError, ValueError, KeyError):
            return None
        for key in ("gray", "depth", "height", "normal", "hit"):
            if key not in arrays:
                return None
        return CachedChunk(metadata=metadata, arrays=arrays)

    def _archive_existing_version(self, chunk_id: str, new_version: str) -> Optional[str]:
        payload_path, metadata_path = self.paths_for(chunk_id)
        old_metadata = self.load_metadata(chunk_id)
        if old_metadata is None:
            return None
        old_version = str(old_metadata.get("global_version", "unknown"))
        if old_version == new_version:
            return None

        destination_dir = self.archive_dir / f"{old_version[:24]}_{str(old_metadata.get('dependency_digest',''))[:12]}"
        destination_dir.mkdir(parents=True, exist_ok=True)
        archived = []
        for source in (payload_path, metadata_path):
            if not source.exists():
                continue
            destination = destination_dir / source.name
            counter = 1
            while destination.exists():
                stem = source.stem
                suffix = "".join(source.suffixes)
                destination = destination_dir / f"{stem}.r{counter}{suffix}"
                counter += 1
            # Same-filesystem atomic directory entry replacement/move.
            source.replace(destination)
            archived.append(str(destination))
        return json.dumps(archived, ensure_ascii=False)

    def save_chunk(
        self,
        *,
        chunk_id: str,
        chunk_index: int,
        global_version: str,
        dependency_digest: str,
        dependency: Dict[str, Any],
        arrays: Dict[str, np.ndarray],
    ) -> Dict[str, Any]:
        with self.lock:
            archived = self._archive_existing_version(chunk_id, global_version)
            payload_path, metadata_path = self.paths_for(chunk_id)

            buffer = io.BytesIO()
            np.savez(
                buffer,
                gray=np.asarray(arrays["gray"]),
                shade=np.asarray(arrays["shade"]),
                depth=np.asarray(arrays["depth"]),
                height=np.asarray(arrays["height"]),
                normal=np.asarray(arrays["normal"]),
                hit=np.asarray(arrays["hit"]),
            )
            payload = buffer.getvalue()
            payload_sha = sha256_bytes(payload)

            metadata = {
                "schema": "terrain-baker-chunk/v1",
                "chunk_id": chunk_id,
                "chunk_index": chunk_index,
                "status": "committed",
                "global_version": global_version,
                "dependency_digest": dependency_digest,
                "dependency": dependency,
                "height_dependency_range": dependency.get("height_range"),
                "global_grid_coverage": dependency.get("global_grid_coverage"),
                "payload_file": payload_path.name,
                "payload_sha256": payload_sha,
                "archived_previous": archived,
                "arrays": {
                    name: {"shape": list(value.shape), "dtype": str(value.dtype)}
                    for name, value in arrays.items()
                    if isinstance(value, np.ndarray)
                },
            }

            # Payload is replaced first; metadata is the atomic commit marker.
            atomic_write_bytes(payload_path, payload)
            atomic_write_json(metadata_path, metadata)
            self.conn.execute(
                """
                INSERT INTO chunk_records(
                    chunk_id, chunk_index, global_version, dependency_digest,
                    status, payload_sha256, metadata_json
                ) VALUES(?, ?, ?, ?, 'committed', ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                    chunk_index=excluded.chunk_index,
                    global_version=excluded.global_version,
                    dependency_digest=excluded.dependency_digest,
                    status=excluded.status,
                    payload_sha256=excluded.payload_sha256,
                    metadata_json=excluded.metadata_json
                """,
                (
                    chunk_id,
                    chunk_index,
                    global_version,
                    dependency_digest,
                    payload_sha,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                ),
            )
            self.conn.commit()
            return metadata

    def record_run(
        self, run_id: str, mode: str, global_version: str, exit_code: int, report: Dict[str, Any]
    ) -> None:
        report_json = json.dumps(report, ensure_ascii=False, sort_keys=True)
        with self.lock, self.conn:
            count = self.conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
            self.conn.execute(
                """
                INSERT OR REPLACE INTO runs(run_id, mode, global_version, exit_code, report_json, created_order)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (run_id, mode, global_version, int(exit_code), report_json, int(count) + 1),
            )


def write_interrupt_marker(state_dir: str | Path, payload: Dict[str, Any]) -> Path:
    path = Path(state_dir) / "INTERRUPTED.json"
    atomic_write_json(path, payload)
    return path
