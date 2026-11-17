from __future__ import annotations

import copy
import threading
from pathlib import Path
from typing import Any

from .cache import ARTIFACT_VERSIONS, artifact_payload_hash
from .errors import SnapshotMismatch
from .util import atomic_write_json, payload_hash, read_json, sha256_text
from .version import ENGINE_VERSION


SNAPSHOT_MANIFEST_VERSION = 1


class SnapshotStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "manifest.json"
        self.manifest: dict[str, Any] = {}
        self.artifacts: dict[str, dict[str, dict[str, Any]]] = {}
        self.global_version = 0
        self._artifact_lock = threading.Lock()

    def load(
        self,
        *,
        config_hash: str,
        page_width: int,
        page_height: int,
        allow_config_change: bool,
    ) -> None:
        if not self.manifest_path.exists():
            self.manifest = {}
            self.artifacts = {}
            self.global_version = 0
            return
        try:
            manifest = read_json(self.manifest_path)
        except (OSError, ValueError) as exc:
            raise SnapshotMismatch(
                f"snapshot manifest is unreadable: {exc}",
                details={"path": str(self.manifest_path)},
            ) from exc
        if not isinstance(manifest, dict):
            raise SnapshotMismatch("snapshot manifest is not a JSON object")
        if manifest.get("manifest_version") != SNAPSHOT_MANIFEST_VERSION:
            raise SnapshotMismatch(
                "snapshot manifest version is incompatible",
                details={
                    "found": manifest.get("manifest_version"),
                    "expected": SNAPSHOT_MANIFEST_VERSION,
                },
            )
        if manifest.get("engine_version") != ENGINE_VERSION:
            raise SnapshotMismatch(
                "snapshot engine version is incompatible",
                details={
                    "found": manifest.get("engine_version"),
                    "expected": ENGINE_VERSION,
                },
            )
        if (
            int(manifest.get("page_width", -1)) != page_width
            or int(manifest.get("page_height", -1)) != page_height
        ):
            raise SnapshotMismatch(
                "snapshot page dimensions differ from metadata",
                details={
                    "found": [manifest.get("page_width"), manifest.get("page_height")],
                    "expected": [page_width, page_height],
                },
            )
        found_hash = manifest.get("config_hash")
        if found_hash != config_hash and not allow_config_change:
            raise SnapshotMismatch(
                "snapshot configuration hash differs; refusing to reuse old artifacts",
                details={"found": found_hash, "expected": config_hash},
            )
        try:
            global_version = int(manifest.get("global_version", 0))
            artifacts = manifest.get("artifacts", {})
            if not isinstance(artifacts, dict):
                raise ValueError("artifacts must be an object")
        except (TypeError, ValueError) as exc:
            raise SnapshotMismatch(f"snapshot manifest is malformed: {exc}") from exc
        if global_version < 0:
            raise SnapshotMismatch("snapshot global_version must be non-negative")
        self.manifest = manifest
        self.artifacts = copy.deepcopy(artifacts)
        self.global_version = global_version

    def _page_directory(self, page: str) -> Path:
        return self.root / "pages" / sha256_text(page)[:20]

    def artifact_path(self, page: str, stage: str) -> Path:
        return self._page_directory(page) / f"{stage}.json"

    def read_artifact(
        self,
        page: str,
        stage: str,
        key: str,
    ) -> tuple[dict[str, Any] | None, str]:
        with self._artifact_lock:
            reference = copy.deepcopy(self.artifacts.get(page, {}).get(stage))
        if not reference:
            return None, "manifest_entry_missing"
        if reference.get("key") != key:
            return None, "cache_key_changed"
        path = self.artifact_path(page, stage)
        if not path.exists():
            return None, "artifact_file_missing"
        try:
            artifact = read_json(path)
        except (OSError, ValueError):
            return None, "artifact_unreadable"
        if not isinstance(artifact, dict):
            return None, "artifact_invalid"
        if artifact.get("stage") != stage or artifact.get("page") != page:
            return None, "artifact_identity_mismatch"
        if artifact.get("cache_key") != key:
            return None, "artifact_key_mismatch"
        version = int(artifact.get("artifact_version", -1))
        if version != ARTIFACT_VERSIONS[stage]:
            return None, "artifact_version_mismatch"
        payload = artifact.get("payload")
        if artifact.get("payload_hash") != payload_hash(payload):
            return None, "artifact_payload_hash_mismatch"
        return payload, "reused"

    def write_artifact(self, page: str, stage: str, key: str, payload: Any) -> dict[str, Any]:
        path = self.artifact_path(page, stage)
        artifact = {
            "artifact_version": ARTIFACT_VERSIONS[stage],
            "stage": stage,
            "page": page,
            "cache_key": key,
            "payload_hash": artifact_payload_hash(payload),
            "payload": payload,
        }
        atomic_write_json(path, artifact)
        reference = {
            "key": key,
            "version": ARTIFACT_VERSIONS[stage],
        }
        with self._artifact_lock:
            self.artifacts.setdefault(page, {})[stage] = reference
        return reference

    def commit_manifest(
        self,
        *,
        config_hash: str,
        parameter_hash: str,
        page_width: int,
        page_height: int,
        pages: dict[str, dict[str, dict[str, Any]]],
        completed_pages: list[str],
        failed_pages: list[str],
        pending_pages: list[str],
        total_pages: int,
        status: str,
        version: int | None = None,
    ) -> int:
        next_version = self.global_version + 1 if version is None else int(version)
        manifest = {
            "manifest_version": SNAPSHOT_MANIFEST_VERSION,
            "engine_version": ENGINE_VERSION,
            "global_version": next_version,
            "config_hash": config_hash,
            "parameter_hash": parameter_hash,
            "page_width": page_width,
            "page_height": page_height,
            "completed_pages": sorted(completed_pages),
            "failed_pages": sorted(failed_pages),
            "pending_pages": sorted(pending_pages),
            "total_pages": total_pages,
            "artifact_versions": dict(ARTIFACT_VERSIONS),
            "artifacts": copy.deepcopy(pages),
            "progress": {
                "status": status,
                "completed_count": len(completed_pages),
                "failed_count": len(failed_pages),
                "pending_count": len(pending_pages),
                "total_count": total_pages,
            },
        }
        atomic_write_json(self.manifest_path, manifest)
        self.manifest = manifest
        self.artifacts = copy.deepcopy(pages)
        self.global_version = next_version
        return next_version
