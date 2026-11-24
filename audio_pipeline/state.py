"""Durable measurement chunk state and interruption markers."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .errors import PipelineError
from .util import (
    atomic_write_json,
    canonical_json_bytes,
    load_json,
    sha256_bytes,
)


class StateStore:
    """SQLite index plus content-addressed JSON measurement chunks."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.chunk_dir = self.root / "chunks"
        self.chunk_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "state.sqlite3"
        self._lock = threading.RLock()
        try:
            self._connection = sqlite3.connect(
                self.db_path, check_same_thread=False, timeout=30.0
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_key TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    input_id TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    measurement_revision TEXT NOT NULL,
                    total_windows INTEGER NOT NULL,
                    start_window INTEGER NOT NULL,
                    end_window INTEGER NOT NULL,
                    window_count INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    PRIMARY KEY (chunk_key, chunk_index)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    version TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    report_path TEXT,
                    metadata_json TEXT NOT NULL
                );
                """
            )
            self._connection.commit()
        except sqlite3.Error as exc:
            raise PipelineError("E_IO", f"cannot open state database: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def chunk_key(self, input_id: str, input_sha256: str,
                  measurement_revision: str, checkpoint_windows: int) -> str:
        return sha256_bytes(canonical_json_bytes({
            "schema": "measurement-cache-key/1",
            "input_id": input_id,
            "input_sha256": input_sha256,
            "measurement_revision": measurement_revision,
            "checkpoint_windows": checkpoint_windows,
        }))

    def _chunk_path(self, key: str, index: int) -> Path:
        return self.chunk_dir / key / f"{index:08d}.json"

    def _is_safe_chunk_path(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.chunk_dir.resolve())
            return True
        except ValueError:
            return False

    def _delete_from(self, key: str, index: int) -> None:
        with self._lock:
            rows = self._connection.execute(
                "SELECT chunk_index, path FROM chunks "
                "WHERE chunk_key=? AND chunk_index>=?",
                (key, index),
            ).fetchall()
            self._connection.execute(
                "DELETE FROM chunks WHERE chunk_key=? AND chunk_index>=?",
                (key, index),
            )
            self._connection.commit()
        for row in rows:
            try:
                path = Path(row["path"])
                if self._is_safe_chunk_path(path):
                    path.unlink(missing_ok=True)
            except OSError:
                pass

    def clear_key(self, key: str) -> None:
        self._delete_from(key, 0)

    def load_chunks(
        self,
        *,
        key: str,
        input_id: str,
        input_sha256: str,
        measurement_revision: str,
        total_windows: int,
        frame_count: int,
        window_frames: int,
        hop_frames: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, bool]:
        """Return valid window records, issues, next index, and complete flag."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM chunks WHERE chunk_key=? ORDER BY chunk_index",
                (key,),
            ).fetchall()
        valid: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        expected_index = 0
        expected_start = 0
        invalid = False
        for row in rows:
            index = int(row["chunk_index"])
            path = Path(row["path"])
            problem: dict[str, Any] | None = None
            payload: dict[str, Any] | None = None
            try:
                if not self._is_safe_chunk_path(path):
                    raise PipelineError(
                        "E_DIGEST", "chunk index path escapes the state chunk directory"
                    )
                raw = path.read_bytes()
                actual_digest = sha256_bytes(raw)
                if not row["sha256"]:
                    raise PipelineError("E_DIGEST", "chunk digest is missing")
                if actual_digest != row["sha256"]:
                    raise PipelineError(
                        "E_DIGEST",
                        "measurement chunk digest mismatch",
                        details={"expected": row["sha256"], "actual": actual_digest},
                    )
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise PipelineError("E_DIGEST", "measurement chunk is not an object")
                checks = {
                    "chunk_key": key,
                    "chunk_index": index,
                    "input_id": input_id,
                    "input_sha256": input_sha256,
                    "measurement_revision": measurement_revision,
                    "total_windows": total_windows,
                    "total_frames": frame_count,
                    "window_frames": window_frames,
                    "hop_frames": hop_frames,
                }
                for field, expected in checks.items():
                    if payload.get(field) != expected:
                        raise PipelineError(
                            "E_DIGEST",
                            f"chunk field {field} is inconsistent with current input",
                        )
                windows = payload.get("windows")
                if not isinstance(windows, list):
                    raise PipelineError("E_DIGEST", "chunk windows must be an array")
                declared_count = int(row["window_count"])
                if int(payload.get("window_count", -1)) != declared_count:
                    raise PipelineError(
                        "E_DIGEST", "chunk declared window count differs from index"
                    )
                if len(windows) != declared_count:
                    raise PipelineError(
                        "E_DIGEST", "chunk actual window count differs from declaration"
                    )
                if index != expected_index:
                    raise PipelineError("E_DIGEST", "chunk indexes are not contiguous")
                if int(payload.get("start_window", -1)) != expected_start:
                    raise PipelineError("E_DIGEST", "chunk window ranges are not contiguous")
                if int(payload.get("end_window", -1)) != expected_start + declared_count:
                    raise PipelineError("E_DIGEST", "chunk end window is inconsistent")
                if declared_count <= 0:
                    raise PipelineError("E_DIGEST", "chunk declares no windows")
                if expected_start + declared_count > total_windows:
                    raise PipelineError(
                        "E_DIGEST", "chunk extends beyond the total window count"
                    )
                for offset, window in enumerate(windows):
                    if not isinstance(window, dict):
                        raise PipelineError("E_DIGEST", "chunk window entry is not an object")
                    if window.get("index") != expected_start + offset:
                        raise PipelineError("E_DIGEST", "chunk window index is inconsistent")
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError,
                    PipelineError) as exc:
                if isinstance(exc, PipelineError):
                    message = exc.message
                    details = exc.details
                else:
                    message = f"cannot read measurement chunk {path}: {exc}"
                    details = {}
                problem = {
                    "code": "E_DIGEST",
                    "message": message,
                    "input_id": input_id,
                    "source": {"kind": "measurement-chunk", "path": str(path),
                               "chunk_index": index},
                    "details": details,
                    "recoverable": True,
                }
            if problem is not None or payload is None:
                issues.append(problem or {
                    "code": "E_DIGEST",
                    "message": "measurement chunk is invalid",
                    "input_id": input_id,
                })
                self._delete_from(key, index)
                invalid = True
                break
            declared_count = int(payload["window_count"])
            valid.extend(payload["windows"])
            expected_index += 1
            expected_start += declared_count
        complete = len(valid) == total_windows
        if complete and rows:
            last = rows[-1]
            if int(last["end_window"]) != total_windows:
                complete = False
        if total_windows == 0:
            complete = True
        return valid, issues, expected_start, complete

    def save_chunk(
        self,
        *,
        key: str,
        index: int,
        input_id: str,
        input_sha256: str,
        measurement_revision: str,
        total_windows: int,
        total_frames: int,
        window_frames: int,
        hop_frames: int,
        start_window: int,
        end_window: int,
        windows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = {
            "schema": "measurement-chunk/1",
            "chunk_key": key,
            "chunk_index": index,
            "input_id": input_id,
            "input_sha256": input_sha256,
            "measurement_revision": measurement_revision,
            "total_windows": total_windows,
            "total_frames": total_frames,
            "window_frames": window_frames,
            "hop_frames": hop_frames,
            "start_window": start_window,
            "end_window": end_window,
            "window_count": len(windows),
            "windows": windows,
        }
        raw = canonical_json_bytes(payload) + b"\n"
        digest = sha256_bytes(raw)
        path = self._chunk_path(key, index)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with tmp.open("wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise PipelineError("E_IO", f"cannot persist chunk {path}: {exc}") from exc
        with self._lock:
            try:
                self._connection.execute(
                    """
                    INSERT INTO chunks
                    (chunk_key, chunk_index, input_id, input_sha256,
                     measurement_revision, total_windows, start_window,
                     end_window, window_count, path, sha256)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chunk_key, chunk_index) DO UPDATE SET
                     input_id=excluded.input_id,
                     input_sha256=excluded.input_sha256,
                     measurement_revision=excluded.measurement_revision,
                     total_windows=excluded.total_windows,
                     start_window=excluded.start_window,
                     end_window=excluded.end_window,
                     window_count=excluded.window_count,
                     path=excluded.path,
                     sha256=excluded.sha256
                    """,
                    (
                        key, index, input_id, input_sha256, measurement_revision,
                        total_windows, start_window, end_window, len(windows),
                        str(path), digest,
                    ),
                )
                self._connection.commit()
            except sqlite3.Error as exc:
                raise PipelineError("E_IO", f"cannot index chunk {path}: {exc}") from exc
        return {
            "chunk_index": index,
            "path": str(path),
            "sha256": digest,
            "window_count": len(windows),
            "start_window": start_window,
            "end_window": end_window,
        }

    def write_interrupt_marker(self, marker: dict[str, Any]) -> Path:
        path = self.root / "interrupted.json"
        atomic_write_json(path, marker)
        return path

    def read_interrupt_marker(self) -> dict[str, Any] | None:
        path = self.root / "interrupted.json"
        if not path.exists():
            return None
        try:
            value = load_json(path, code="E_IO")
            return value if isinstance(value, dict) else None
        except PipelineError:
            return None

    def clear_interrupt_marker(self) -> None:
        try:
            (self.root / "interrupted.json").unlink(missing_ok=True)
        except OSError:
            pass

    def record_run(self, version: str, status: str, report_path: Path | None,
                   metadata: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO runs(version,status,report_path,metadata_json) "
                "VALUES (?,?,?,?)",
                (
                    version,
                    status,
                    str(report_path) if report_path else None,
                    canonical_json_bytes(metadata).decode("utf-8"),
                ),
            )
            self._connection.commit()
