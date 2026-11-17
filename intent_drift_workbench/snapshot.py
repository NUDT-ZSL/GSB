"""Versioned, atomically published snapshots and recovery selection."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical import (
    atomic_write_json,
    canonical_json,
    read_json,
    sha256_file,
    sha256_json,
)

VERSION_PATTERN = re.compile(r"^v(\d{8,})$")
STAGE_FILES = {
    "features": "turns.json",
    "windows": "windows.json",
    "scoring": "scoring.json",
    "failures": "failures.json",
    "state": "state.json",
}


class SnapshotError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class SnapshotManager:
    def __init__(self, root: Path):
        self.root = root
        self.versions_dir = root / "versions"
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        self.latest_path = root / "latest.json"

    def _write_file(self, directory: Path, name: str, value: Any) -> None:
        path = directory / name
        atomic_write_json(path, value)

    def commit(
        self,
        *,
        global_version: int,
        features: list[dict[str, Any]],
        windows: list[dict[str, Any]],
        scoring: list[dict[str, Any]],
        failures: list[dict[str, Any]],
        state: dict[str, Any],
        completed_batches: list[str],
        config_hash: str,
        base_vocab_hash: str,
        vocab_hash: str,
        weights_hash: str,
    ) -> dict[str, Any]:
        final_directory = self.versions_dir / f"v{global_version:08d}"
        if final_directory.exists():
            raise SnapshotError(
                "E_SNAPSHOT_EXISTS",
                f"snapshot version already exists: {final_directory}",
            )
        temporary = Path(
            tempfile.mkdtemp(prefix=f".v{global_version:08d}.", dir=self.versions_dir)
        )
        try:
            payload = {
                "features": features,
                "windows": windows,
                "scoring": scoring,
                "failures": failures,
                "state": state,
            }
            stage_files: dict[str, Any] = {}
            for stage, filename in STAGE_FILES.items():
                self._write_file(temporary, filename, payload[stage])
                stage_path = temporary / filename
                stage_files[stage] = {
                    "path": filename,
                    "sha256": sha256_file(stage_path),
                    "version": global_version,
                    "bytes": stage_path.stat().st_size,
                }
            manifest = {
                "schema_version": 1,
                "status": "complete",
                "global_version": global_version,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "config_hash": config_hash,
                "base_vocab_hash": base_vocab_hash,
                "vocab_hash": vocab_hash,
                "weights_hash": weights_hash,
                "stage_versions": {
                    stage: global_version for stage in STAGE_FILES
                },
                "stage_files": stage_files,
                "completed_batches": list(completed_batches),
                "completed_batch_count": len(completed_batches),
            }
            manifest["manifest_hash"] = sha256_json(
                {key: value for key, value in manifest.items() if key != "manifest_hash"}
            )
            self._write_file(temporary, "manifest.json", manifest)
            _fsync_directory(temporary)
            os.replace(temporary, final_directory)
            _fsync_directory(self.versions_dir)
            manifest["_directory"] = str(final_directory)
            atomic_write_json(
                self.latest_path,
                {
                    "global_version": global_version,
                    "path": str(final_directory),
                    "manifest_hash": manifest["manifest_hash"],
                },
            )
            return manifest
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def _read_manifest(self, directory: Path) -> dict[str, Any] | None:
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            return None
        try:
            manifest = read_json(manifest_path)
        except (OSError, ValueError):
            return None
        if not isinstance(manifest, dict) or manifest.get("status") != "complete":
            return None
        expected_hash = manifest.get("manifest_hash")
        actual_hash = sha256_json(
            {key: value for key, value in manifest.items() if key != "manifest_hash"}
        )
        if expected_hash != actual_hash:
            return None
        stage_files = manifest.get("stage_files")
        if not isinstance(stage_files, dict):
            return None
        for stage in STAGE_FILES:
            entry = stage_files.get(stage)
            if not isinstance(entry, dict):
                return None
            path = directory / str(entry.get("path"))
            if not path.is_file():
                return None
            if sha256_file(path) != entry.get("sha256"):
                return None
        return manifest

    def list_complete_manifests(self) -> list[dict[str, Any]]:
        manifests = []
        if not self.versions_dir.is_dir():
            return manifests
        for directory in self.versions_dir.iterdir():
            if not directory.is_dir() or not VERSION_PATTERN.match(directory.name):
                continue
            manifest = self._read_manifest(directory)
            if manifest is not None:
                manifest["_directory"] = str(directory)
                manifests.append(manifest)
        manifests.sort(key=lambda item: int(item["global_version"]))
        return manifests

    def load_latest(self) -> tuple[dict[str, Any], dict[str, Any]] | None:
        manifests = self.list_complete_manifests()
        if not manifests:
            return None
        manifest = manifests[-1]
        directory = Path(manifest["_directory"])
        payload = {}
        for stage, filename in STAGE_FILES.items():
            payload[stage] = read_json(directory / filename)
        return manifest, payload

    def latest_pointer(self) -> dict[str, Any] | None:
        if not self.latest_path.is_file():
            return None
        try:
            return read_json(self.latest_path)
        except (OSError, ValueError):
            return None

    def recovery_info(self, manifest: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "RECOVERED_COMPLETE_SNAPSHOT",
            "global_version": manifest["global_version"],
            "completed_batches": list(manifest.get("completed_batches", [])),
            "stage_versions": dict(manifest.get("stage_versions", {})),
            "snapshot_path": manifest.get("_directory"),
            "created_at": manifest.get("created_at"),
            "note": "Only a fully replaced snapshot with a valid manifest is used; no exit report is assumed.",
        }

    def canonical_manifest(self, manifest: dict[str, Any]) -> str:
        return canonical_json(
            {key: value for key, value in manifest.items() if key != "created_at"}
        )
