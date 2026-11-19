from __future__ import annotations

import fcntl
import json
import re
import threading
from pathlib import Path
from typing import Any

from .cache import CacheStore
from .canonical import (
    atomic_write,
    atomic_write_json,
    canonical_bytes,
    hash_value,
    readable_json_bytes,
    read_json,
    sha256_bytes,
    sha256_file,
    without_field,
)
from .errors import E_CACHE_CORRUPT, E_STAGE_BUSY, E_STALE_VERSION, WorkbenchError

_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def _process_lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.Lock())


class StageLock:
    """Non-blocking exclusive writer lock for one stage."""

    def __init__(self, stage_root: Path | str) -> None:
        self.stage_root = Path(stage_root)
        self.stage_root.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.stage_root / "stage.lock"
        self._thread_lock = _process_lock_for(self.lock_path)
        self._file: Any = None

    def __enter__(self) -> "StageLock":
        if not self._thread_lock.acquire(blocking=False):
            raise WorkbenchError(
                E_STAGE_BUSY,
                "another incremental commit is active in this process",
                {"stage": str(self.stage_root)},
            )
        self._file = self.lock_path.open("a+b")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._file.close()
            self._file = None
            self._thread_lock.release()
            raise WorkbenchError(
                E_STAGE_BUSY,
                "another incremental commit is active",
                {"stage": str(self.stage_root)},
            ) from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._file is not None:
            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            finally:
                self._file.close()
                self._file = None
        self._thread_lock.release()


class Stage:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "manifests").mkdir(parents=True, exist_ok=True)
        (self.root / "cache" / "indexes").mkdir(parents=True, exist_ok=True)
        (self.root / "datasets").mkdir(parents=True, exist_ok=True)
        self.cache = CacheStore(self.root)
        self._errors_guard = _process_lock_for(self.root / "errors.lock")

    @property
    def current_path(self) -> Path:
        return self.root / "current.json"

    def manifest_path(self, version: int) -> Path:
        return self.root / "manifests" / f"v{version:06d}.json"

    def dataset_path(self, version: int) -> Path:
        return self.root / "datasets" / f"dataset-v{version:06d}.json"

    def dataset_content_path(self, dataset_checksum: str) -> Path:
        digest = dataset_checksum.removeprefix("sha256:")
        return self.root / "datasets" / "content" / f"{digest}.jsonl"

    @staticmethod
    def validate_manifest(manifest: dict[str, Any]) -> None:
        recorded = manifest.get("manifest_checksum")
        computed = hash_value(without_field(manifest, "manifest_checksum"))
        if recorded != computed:
            raise WorkbenchError(
                E_CACHE_CORRUPT,
                "manifest checksum mismatch",
                {
                    "category": "MANIFEST_CHECKSUM_MISMATCH",
                    "recorded_checksum": recorded,
                    "computed_checksum": computed,
                },
                node="manifest",
                version=manifest.get("version") if isinstance(manifest.get("version"), int) else None,
            )

    def current_manifest(self) -> dict[str, Any] | None:
        if not self.current_path.exists():
            return None
        raw = self.current_path.read_text(encoding="utf-8")
        try:
            manifest = json.loads(raw)
        except json.JSONDecodeError as exc:
            match = re.search(r'"version"\s*:\s*(\d+)', raw)
            version = int(match.group(1)) if match else None
            raise WorkbenchError(
                E_CACHE_CORRUPT,
                "current manifest is not valid JSON",
                {"category": "MANIFEST_JSON_CORRUPT", "path": str(self.current_path)},
                node="manifest",
                version=version,
            ) from exc
        if not isinstance(manifest, dict):
            raise WorkbenchError(
                E_CACHE_CORRUPT,
                "current manifest root must be an object",
                node="manifest",
            )
        self.validate_manifest(manifest)
        version = manifest["version"]
        snapshot_path = self.manifest_path(version)
        if snapshot_path.exists():
            snapshot = read_json(snapshot_path)
            self.validate_manifest(snapshot)
            if snapshot.get("manifest_checksum") != manifest.get("manifest_checksum"):
                raise WorkbenchError(
                    E_CACHE_CORRUPT,
                    "current manifest and version snapshot disagree",
                    {"category": "MANIFEST_SNAPSHOT_MISMATCH"},
                    node="manifest",
                    version=version,
                )
        return manifest

    def load_manifest(self, version: int) -> dict[str, Any]:
        path = self.manifest_path(version)
        if not path.exists():
            raise WorkbenchError(
                E_CACHE_CORRUPT,
                "manifest version not found",
                {"path": str(path)},
                node="manifest",
                version=version,
            )
        manifest = read_json(path)
        self.validate_manifest(manifest)
        return manifest

    def commit(
        self,
        manifest: dict[str, Any],
        dataset_records: list[dict[str, Any]],
        *,
        expected_base_version: int | None = None,
    ) -> dict[str, Any]:
        current = self.current_manifest()
        current_version = int(current["version"]) if current is not None else 0
        if expected_base_version is not None and expected_base_version != current_version:
            raise WorkbenchError(
                E_STALE_VERSION,
                "expected base version does not match the active snapshot",
                {
                    "expected_base_version": expected_base_version,
                    "current_version": current_version,
                },
                node="manifest",
                version=current_version,
            )
        version = current_version + 1
        if manifest.get("version") != version:
            raise WorkbenchError(
                E_STALE_VERSION,
                "candidate manifest version is not the next monotonic version",
                {
                    "candidate_version": manifest.get("version"),
                    "required_version": version,
                },
                node="manifest",
                version=current_version,
            )

        dataset = {
            "schema_version": 1,
            "version": version,
            "records": dataset_records,
        }
        dataset["dataset_checksum"] = hash_value(dataset["records"])
        dataset_path = self.dataset_path(version)
        atomic_write(dataset_path, canonical_bytes(dataset))
        content = b"\n".join(
            canonical_bytes(record)
            for record in sorted(dataset_records, key=lambda item: item["pair_id"])
        )
        if content:
            content += b"\n"
        content_path = self.dataset_content_path(dataset["dataset_checksum"])
        atomic_write(content_path, content)

        manifest["dataset_checksum"] = dataset["dataset_checksum"]
        manifest["dataset_path"] = str(dataset_path.relative_to(self.root))
        manifest["dataset_records_path"] = str(content_path.relative_to(self.root))
        manifest["dataset_records_checksum"] = sha256_bytes(content)
        manifest["manifest_checksum"] = hash_value(manifest)
        serialized = canonical_bytes(manifest)
        atomic_write(self.manifest_path(version), serialized)
        atomic_write(self.current_path, serialized)
        return manifest

    def dataset(self, manifest: dict[str, Any]) -> dict[str, Any]:
        path = self.root / manifest["dataset_path"]
        try:
            return read_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkbenchError(
                E_CACHE_CORRUPT,
                "dataset snapshot is missing or corrupt",
                {"category": "DATASET_JSON_CORRUPT", "path": str(path)},
                node="manifest",
                version=manifest.get("version"),
            ) from exc

    def commit_cache_index(
        self,
        *,
        version: int,
        entries: dict[str, dict[str, dict[str, Any]]],
    ) -> dict[str, Any]:
        index = {
            "schema_version": 1,
            "version": version,
            "entries": entries,
        }
        index["index_checksum"] = hash_value(index)
        path = self.root / "cache" / "indexes" / f"v{version:06d}.json"
        atomic_write(path, canonical_bytes(index))
        return index

    def load_cache_index(self, version: int) -> dict[str, Any]:
        path = self.root / "cache" / "indexes" / f"v{version:06d}.json"
        if not path.exists():
            raise WorkbenchError(
                E_CACHE_CORRUPT,
                "cache index version not found",
                {"path": str(path)},
                node="cache_index",
                version=version,
            )
        index = read_json(path)
        recorded = index.get("index_checksum")
        computed = hash_value(without_field(index, "index_checksum"))
        if recorded != computed:
            raise WorkbenchError(
                E_CACHE_CORRUPT,
                "cache index checksum mismatch",
                {"category": "CACHE_INDEX_CHECKSUM_MISMATCH"},
                node="cache_index",
                version=version,
            )
        return index

    def record_error(self, error: WorkbenchError) -> None:
        if not self._errors_guard.acquire(blocking=False):
            return
        path = self.root / "errors.json"
        try:
            history = read_json(path) if path.exists() else {"schema_version": 1, "sequence": 0, "errors": []}
            history["sequence"] = int(history.get("sequence", 0)) + 1
            row = {
                "sequence": history["sequence"],
                **error.to_dict(),
            }
            history["errors"] = (history.get("errors", []) + [row])[-30:]
            atomic_write_json(path, history)
        except (OSError, ValueError, TypeError):
            pass
        finally:
            self._errors_guard.release()

    def recent_errors(self) -> list[dict[str, Any]]:
        path = self.root / "errors.json"
        if not path.exists():
            return []
        try:
            history = read_json(path)
            return list(history.get("errors", []))[-10:]
        except (OSError, json.JSONDecodeError, TypeError):
            return []

    def write_auxiliary_json(self, name: str, value: Any) -> Path:
        path = self.root / name
        atomic_write_json(path, value, readable=True)
        return path

    def dataset_file_checksum(self, manifest: dict[str, Any]) -> str:
        path = self.root / manifest["dataset_path"]
        if not path.exists():
            raise WorkbenchError(
                E_CACHE_CORRUPT,
                "dataset snapshot file is missing",
                {"path": str(path)},
                node="manifest",
                version=manifest.get("version"),
            )
        return sha256_file(path)
