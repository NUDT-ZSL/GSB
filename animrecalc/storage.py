from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .errors import DigestMismatchError
from .hashing import atomic_write_json, canonical_digest


class ChunkStore:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.chunks_dir = self.state_dir / "chunks"
        self.archive_dir = self.state_dir / "archive"
        self.marker_path = self.state_dir / "INTERRUPTED.json"
        self.version_path = self.state_dir / "version.json"
        self.catalog_path = self.state_dir / "catalog.sqlite"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.chunks_dir.mkdir(parents=True, exist_ok=True)
        self._init_catalog()

    def _init_catalog(self) -> None:
        with sqlite3.connect(self.catalog_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chunks (
                    version_id TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    frame_count INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    path TEXT NOT NULL,
                    PRIMARY KEY(version_id, chunk_index)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    report_path TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def chunk_path(self, chunk_index: int) -> Path:
        return self.chunks_dir / f"chunk_{chunk_index:06d}.json"

    @staticmethod
    def record_digest(record: dict[str, Any]) -> str:
        content = {key: value for key, value in record.items() if key != "digest"}
        return canonical_digest(content)

    def _archive_existing_version(self, path: Path, new_version: str) -> str | None:
        if not path.exists():
            return None
        try:
            old_payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt partial final file cannot be a completed chunk; replace it.
            return None
        old_version = str(old_payload.get("version_id", "unknown"))
        if old_version == new_version:
            return None
        safe_version = old_version.replace(":", "_").replace("/", "_")
        archive_path = self.archive_dir / safe_version / "chunks" / path.name
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        if archive_path.exists():
            try:
                old_digest = old_payload.get("digest")
                archived = json.loads(archive_path.read_text(encoding="utf-8"))
                if archived.get("digest") != old_digest:
                    stem = archive_path.stem
                    archive_path = archive_path.with_name(f"{stem}.{canonical_digest(old_payload)[:18]}.json")
            except (OSError, json.JSONDecodeError):
                pass
        path.replace(archive_path)
        return str(archive_path)

    def write_chunk(self, record: dict[str, Any]) -> Path:
        chunk_index = int(record["chunk_index"])
        path = self.chunk_path(chunk_index)
        archived_to = self._archive_existing_version(path, str(record["version_id"]))
        if archived_to:
            record = dict(record)
            record.setdefault("archive", {})
            # Do not include archive destination in digest; it is operational metadata.
        record["digest"] = self.record_digest(record)
        atomic_write_json(path, record)
        with sqlite3.connect(self.catalog_path) as conn:
            conn.execute(
                """
                INSERT INTO chunks(version_id, chunk_index, frame_count, digest, path)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(version_id, chunk_index) DO UPDATE SET
                    frame_count=excluded.frame_count,
                    digest=excluded.digest,
                    path=excluded.path
                """,
                (
                    record["version_id"],
                    chunk_index,
                    record["frame_count"],
                    record["digest"],
                    str(path),
                ),
            )
        return path

    def read_chunk_raw(self, chunk_index: int) -> dict[str, Any]:
        path = self.chunk_path(chunk_index)
        if not path.exists():
            raise DigestMismatchError("chunk is missing", {"chunk_index": chunk_index, "path": str(path)})
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DigestMismatchError(
                "chunk is unreadable or invalid JSON",
                {"chunk_index": chunk_index, "reason": str(exc)},
            ) from exc

    def verify_chunk(
        self,
        chunk_index: int,
        expected_version: str | None = None,
        expected_frame_count: int | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        try:
            record = self.read_chunk_raw(chunk_index)
        except DigestMismatchError as exc:
            return False, exc.message, None
        digest = record.get("digest")
        expected_digest = self.record_digest(record)
        if digest != expected_digest:
            return False, "content digest mismatch", record
        if expected_version is not None and record.get("version_id") != expected_version:
            return False, "version mismatch", record
        if expected_frame_count is not None and record.get("frame_count") != expected_frame_count:
            return False, "declared frame count mismatch", record
        pose = record.get("pose")
        if not isinstance(pose, list) or len(pose) != record.get("frame_count"):
            return False, "pose frame count does not match declaration", record
        return True, "ok", record

    def read_valid_chunk(
        self,
        chunk_index: int,
        expected_version: str,
        expected_frame_count: int,
    ) -> dict[str, Any]:
        ok, reason, record = self.verify_chunk(chunk_index, expected_version, expected_frame_count)
        if not ok or record is None:
            raise DigestMismatchError(
                reason,
                {"chunk_index": chunk_index, "expected_version": expected_version},
            )
        return record

    def write_interrupt_marker(self, payload: dict[str, Any]) -> None:
        atomic_write_json(self.marker_path, payload)

    def read_interrupt_marker(self) -> dict[str, Any] | None:
        if not self.marker_path.exists():
            return None
        try:
            return json.loads(self.marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"corrupt_marker": True}

    def clear_interrupt_marker(self) -> None:
        try:
            self.marker_path.unlink()
        except FileNotFoundError:
            pass

    def write_version(self, version_id: str, extra: dict[str, Any] | None = None) -> None:
        payload = {"version_id": version_id, **(extra or {})}
        atomic_write_json(self.version_path, payload)

    def read_version(self) -> str | None:
        if not self.version_path.exists():
            return None
        try:
            payload = json.loads(self.version_path.read_text(encoding="utf-8"))
            return payload.get("version_id")
        except (OSError, json.JSONDecodeError):
            return None

    def atomic_write_output(self, name: str, payload: Any) -> Path:
        path = self.state_dir / name
        atomic_write_json(path, payload)
        return path

    def record_run(self, run_id: str, status: str, report_path: str | None = None) -> None:
        with sqlite3.connect(self.catalog_path) as conn:
            conn.execute(
                """
                INSERT INTO runs(run_id, status, report_path)
                VALUES (?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status=excluded.status,
                    report_path=excluded.report_path,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (run_id, status, report_path),
            )
