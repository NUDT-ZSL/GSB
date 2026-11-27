"""Versioned, checksummed persistence for render artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: str | Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _safe_version(version: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", version).strip("._")
    if not cleaned:
        raise ValueError("project version does not contain a usable directory name")
    return cleaned


def build_artifact_manifest(directory: str | Path) -> dict[str, Any]:
    directory = Path(directory)
    files = []
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        relative = path.relative_to(directory).as_posix()
        if relative == "artifact_manifest.json":
            continue
        files.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return {
        "schema_version": "1.0",
        "file_count": len(files),
        "files": files,
    }


class VersionedArtifactStore:
    """Stores each project version under ``versions/<version>``.

    The staging directory is replaced only after all artifacts have been
    written, so a failed run cannot expose a partial version.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.versions_dir = self.root / "versions"
        self.staging_root = self.root / ".staging"
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)

    def begin(self, version: str) -> Path:
        safe = _safe_version(version)
        staging = self.staging_root / safe
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        return staging

    def commit(self, staging: str | Path, version: str) -> tuple[Path, dict[str, Any]]:
        staging = Path(staging)
        safe = _safe_version(version)
        if not staging.is_dir():
            raise ValueError("staging directory does not exist")
        manifest = build_artifact_manifest(staging)
        write_json(staging / "artifact_manifest.json", manifest)
        destination = self.versions_dir / safe
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(staging, destination)
        self._rebuild_index(latest=safe)
        return destination, manifest

    def _rebuild_index(self, latest: str) -> None:
        entries = []
        for directory in sorted(self.versions_dir.iterdir()):
            manifest_path = directory / "artifact_manifest.json"
            info_path = directory / "version_info.json"
            if not directory.is_dir() or not manifest_path.is_file():
                continue
            info = read_json(info_path) if info_path.is_file() else {}
            entries.append(
                {
                    "version": directory.name,
                    "project_id": info.get("project_id"),
                    "project_version": info.get("project_version"),
                    "project_sha256": info.get("project_sha256"),
                    "frame_count": info.get("frame_count"),
                    "artifact_manifest_sha256": sha256_file(manifest_path),
                }
            )
        index = {
            "schema_version": "1.0",
            "latest_version": latest,
            "versions": entries,
        }
        write_json(self.root / "index.json", index)
        write_json(self.root / "latest.json", {"schema_version": "1.0", "version": latest})

    def list_versions(self) -> list[dict[str, Any]]:
        index_path = self.root / "index.json"
        if not index_path.is_file():
            return []
        index = read_json(index_path)
        return list(index.get("versions", []))

    def resolve(self, version: str | None = None) -> Path:
        if version is None:
            latest_path = self.root / "latest.json"
            if not latest_path.is_file():
                raise FileNotFoundError("no committed artifact version exists")
            version = read_json(latest_path)["version"]
        destination = self.versions_dir / _safe_version(version)
        if not destination.is_dir():
            raise FileNotFoundError(f"artifact version {version!r} does not exist")
        return destination

    def load_manifest(self, version: str | None = None) -> dict[str, Any]:
        return read_json(self.resolve(version) / "artifact_manifest.json")

