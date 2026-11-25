"""Versioned atomic snapshots: v1 JSON and v2 shards plus SQLite."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .canonical import (
    atomic_write_json,
    canonical_dumps,
    read_json,
    sha256_bytes,
    sha256_file,
    sha256_json,
)
from .errors import E_SNAPSHOT_ROLLBACK, make_error


def content_checksum(payload: dict[str, Any]) -> str:
    return sha256_json(payload)


class SnapshotManager:
    def __init__(self, workdir: str | Path):
        self.root = Path(workdir) / "snapshots"
        self.v1_root = self.root / "v1"
        self.v2_root = self.root / "v2"
        self.v1_root.mkdir(parents=True, exist_ok=True)
        self.v2_root.mkdir(parents=True, exist_ok=True)

    def current_v1(self) -> dict[str, Any] | None:
        path = self.v1_root / "snapshot.json"
        return read_json(path) if path.is_file() else None

    def publish_v1(self, payload: dict[str, Any], now_ms: int) -> dict[str, Any]:
        checksum = content_checksum(payload)
        current = self.current_v1()
        if current and current.get("content_checksum") == checksum:
            return {
                "generation": "v1",
                "version": current["version"],
                "created_at_ms": current.get("created_at_ms"),
                "content_checksum": checksum,
                "changed": False,
                "replacement_mode": "unchanged_keep_current",
                "path": str(self.v1_root / "snapshot.json"),
            }

        version = int(current["version"]) + 1 if current else 1
        manifest = {
            "snapshot_format": "v1",
            "format_version": 1,
            "version": version,
            "created_at_ms": int(now_ms),
            "content_checksum": checksum,
            "payload": payload,
        }
        atomic_write_json(self.v1_root / "snapshot.json", manifest)
        atomic_write_json(self.v1_root / "versions" / f"v{version}.json", manifest)
        return {
            "generation": "v1",
            "version": version,
            "created_at_ms": int(now_ms),
            "content_checksum": checksum,
            "changed": True,
            "replacement_mode": "atomic_full_replace",
            "trigger": "checksum_changed" if current else "initial_snapshot",
            "path": str(self.v1_root / "snapshot.json"),
        }

    def current_v2_manifest(self) -> dict[str, Any] | None:
        current_link = self.v2_root / "current"
        manifest = current_link / "manifest.json"
        if manifest.is_file():
            return read_json(manifest)
        pointer = self.v2_root / "current.json"
        if pointer.is_file():
            info = read_json(pointer)
            manifest_path = self.v2_root / info["release_path"] / "manifest.json"
            return read_json(manifest_path) if manifest_path.is_file() else None
        return None

    def _write_shard(self, release_dir: Path, relative: str, value: Any) -> str:
        path = release_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        text = canonical_dumps(value, indent=2) + "\n"
        path.write_text(text, encoding="utf-8", newline="\n")
        return sha256_bytes(text.encode("utf-8"))

    def _write_trace_shards(self, release_dir: Path, traces_by_batch: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
        checksums = {}
        for batch_id, traces in sorted(traces_by_batch.items()):
            relative = f"traces/{batch_id.replace('/', '_')}.jsonl"
            path = release_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            text = "".join(canonical_dumps(row) + "\n" for row in traces)
            path.write_text(text, encoding="utf-8", newline="\n")
            checksums[relative] = sha256_bytes(text.encode("utf-8"))
        return checksums

    def _build_sqlite(
        self,
        db_path: Path,
        payload: dict[str, Any],
        cache_refs: list[dict[str, Any]],
    ) -> str:
        if db_path.exists():
            db_path.unlink()
        connection = sqlite3.connect(str(db_path))
        try:
            cursor = connection.cursor()
            cursor.executescript(
                """
                CREATE TABLE manifest (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE batches (
                    batch_id TEXT PRIMARY KEY,
                    cycle_id TEXT NOT NULL,
                    record_count INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    cache_key TEXT
                );
                CREATE TABLE calibration (
                    batch_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    batch_id TEXT,
                    segment_index INTEGER,
                    target TEXT,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
                CREATE TABLE cache_refs (
                    ref_name TEXT PRIMARY KEY,
                    cache_key TEXT NOT NULL,
                    event_seq INTEGER NOT NULL
                );
                CREATE TABLE traces (
                    trace_id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    cycle_id TEXT,
                    entity_id TEXT,
                    category TEXT,
                    payload_json TEXT NOT NULL
                );
                """
            )
            cursor.execute(
                "INSERT INTO manifest(key,value) VALUES(?,?)",
                ("content_checksum", payload["content_checksum"]),
            )
            for batch in payload.get("batch_summaries", []):
                cursor.execute(
                    "INSERT OR REPLACE INTO batches VALUES (?,?,?,?,?,?)",
                    (
                        batch["batch_id"],
                        batch.get("cycle_id", ""),
                        int(batch.get("record_count", 0)),
                        batch.get("fingerprint", ""),
                        batch.get("content_hash", ""),
                        batch.get("cache_key"),
                    ),
                )
            for calibration in payload.get("calibration_results", []):
                checksum = sha256_json(calibration)
                cursor.execute(
                    "INSERT OR REPLACE INTO calibration VALUES (?,?,?,?)",
                    (
                        calibration["batch_id"],
                        calibration.get("status", "unknown"),
                        checksum,
                        canonical_dumps(calibration),
                    ),
                )
            for error in payload.get("errors", []):
                details = {key: value for key, value in error.items() if key not in {"code", "batch_id", "segment_index", "target", "message"}}
                cursor.execute(
                    "INSERT INTO errors(code,batch_id,segment_index,target,message,details_json) VALUES (?,?,?,?,?,?)",
                    (
                        error.get("code"),
                        error.get("batch_id"),
                        error.get("segment_index"),
                        error.get("target"),
                        error.get("message", ""),
                        canonical_dumps(details),
                    ),
                )
            for ref in cache_refs:
                cursor.execute(
                    "INSERT OR REPLACE INTO cache_refs VALUES (?,?,?)",
                    (ref["ref_name"], ref["cache_key"], int(ref.get("event_seq", 0))),
                )
            for trace in payload.get("trace_rows", []):
                cursor.execute(
                    "INSERT OR REPLACE INTO traces VALUES (?,?,?,?,?,?)",
                    (
                        trace["trace_id"],
                        trace["batch_id"],
                        trace.get("cycle_id"),
                        trace.get("entity_id"),
                        trace.get("category"),
                        canonical_dumps(trace),
                    ),
                )
            connection.commit()
        finally:
            connection.close()
        return sha256_file(db_path)

    def publish_v2(
        self,
        payload: dict[str, Any],
        now_ms: int,
        *,
        cache_refs: list[dict[str, Any]] | None = None,
        traces_by_batch: dict[str, list[dict[str, Any]]] | None = None,
        trace_rows: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        checksum = content_checksum(payload)
        current = self.current_v2_manifest()
        if current and current.get("content_checksum") == checksum:
            return {
                "generation": "v2",
                "version": current["version"],
                "created_at_ms": current.get("created_at_ms"),
                "content_checksum": checksum,
                "changed": False,
                "replacement_mode": "unchanged_keep_current_release",
                "release_path": str(self.v2_root / "current"),
            }

        version = int(current["version"]) + 1 if current else 1
        releases = self.v2_root / "releases"
        releases.mkdir(parents=True, exist_ok=True)
        tmp_release = Path(tempfile.mkdtemp(prefix=f".v{version}.", dir=releases))
        shard_checksums: dict[str, str] = {}
        try:
            shard_checksums["config.json"] = self._write_shard(tmp_release, "config.json", payload.get("config", {}))
            for name in ("reference_statistics", "full_statistics"):
                relative = f"{name}.json"
                shard_checksums[relative] = self._write_shard(tmp_release, relative, payload.get(name, {}))
            for batch in payload.get("batch_summaries", []):
                batch_id = batch["batch_id"].replace("/", "_")
                relative = f"batches/{batch_id}.json"
                shard_checksums[relative] = self._write_shard(tmp_release, relative, batch)
            for calibration in payload.get("calibration_results", []):
                batch_id = calibration["batch_id"].replace("/", "_")
                relative = f"calibration/{batch_id}.json"
                shard_checksums[relative] = self._write_shard(tmp_release, relative, calibration)
            for index, diagnosis in enumerate(payload.get("boundary_diagnostics", [])):
                target_name = str(diagnosis.get("target") or index).replace("/", "_")
                relative = f"boundary/{index:02d}-{target_name}.json"
                shard_checksums[relative] = self._write_shard(tmp_release, relative, diagnosis)
            shard_checksums.update(self._write_trace_shards(tmp_release, traces_by_batch or {}))

            sqlite_checksum = self._build_sqlite(
                tmp_release / "index.sqlite",
                {**payload, "content_checksum": checksum, "trace_rows": trace_rows or []},
                cache_refs or [],
            )
            shard_checksums["index.sqlite"] = sqlite_checksum
            manifest_without_hash = {
                "snapshot_format": "v2",
                "format_version": 2,
                "version": version,
                "created_at_ms": int(now_ms),
                "content_checksum": checksum,
                "release": f"releases/v{version}",
                "shard_checksums": dict(sorted(shard_checksums.items())),
            }
            manifest_checksum = sha256_json(manifest_without_hash)
            manifest = {**manifest_without_hash, "manifest_checksum": manifest_checksum}
            (tmp_release / "manifest.json").write_text(
                canonical_dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
            )
            final_release = releases / f"v{version}"
            os.replace(tmp_release, final_release)
        except Exception:
            import shutil

            if tmp_release.exists():
                shutil.rmtree(tmp_release, ignore_errors=True)
            raise

        self._atomically_switch_current(version)
        atomic_write_json(
            self.v2_root / "current.json",
            {"version": version, "release_path": f"releases/v{version}", "manifest_checksum": manifest_checksum},
        )
        return {
            "generation": "v2",
            "version": version,
            "created_at_ms": int(now_ms),
            "content_checksum": checksum,
            "manifest_checksum": manifest_checksum,
            "changed": True,
            "replacement_mode": "atomic_full_release_replace",
            "trigger": "checksum_changed" if current else "initial_snapshot",
            "release_path": str(final_release),
        }

    def _atomically_switch_current(self, version: int) -> None:
        link_path = self.v2_root / "current"
        tmp_link = self.v2_root / f".current-{os.getpid()}-{version}.tmp"
        target = f"releases/v{version}"
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink()
        os.symlink(target, tmp_link)
        os.replace(tmp_link, link_path)
        directory_fd = os.open(self.v2_root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def open_current_v2(self) -> tuple[Path, dict[str, Any]]:
        current_link = self.v2_root / "current"
        if not current_link.is_symlink() and not current_link.exists():
            raise FileNotFoundError("v2 snapshot is not published")
        release_root = current_link.resolve()
        manifest = read_json(release_root / "manifest.json")
        return release_root, manifest

    def verify_current_v2(self) -> dict[str, Any]:
        release_root, manifest = self.open_current_v2()
        failures = []
        for relative, expected in manifest.get("shard_checksums", {}).items():
            path = release_root / relative
            if not path.is_file():
                failures.append({"relative": relative, "reason": "missing"})
                continue
            actual = sha256_file(path)
            if actual != expected:
                failures.append({"relative": relative, "reason": "checksum_mismatch"})
        return {
            "valid": not failures,
            "release_root": str(release_root),
            "version": manifest.get("version"),
            "failures": failures,
        }

    def attempt_rollback(self, target_version: int) -> dict[str, Any]:
        current_versions = []
        v1 = self.current_v1()
        v2 = self.current_v2_manifest()
        if v1:
            current_versions.append(int(v1["version"]))
        if v2:
            current_versions.append(int(v2["version"]))
        current_version = max(current_versions, default=1)
        if int(target_version) < current_version:
            return {
                "allowed": False,
                "current_version": current_version,
                "target_version": int(target_version),
                "error": make_error(
                    E_SNAPSHOT_ROLLBACK,
                    "old snapshot version rollback is rejected",
                    current_version=current_version,
                    target_version=int(target_version),
                ),
            }
        return {
            "allowed": True,
            "current_version": current_version,
            "target_version": int(target_version),
            "message": "target is not older than current; no rollback required",
        }


def read_snapshot_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)
