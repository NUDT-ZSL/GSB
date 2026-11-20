"""Versioned stage snapshots with whole-snapshot replacement and commit locks."""
from __future__ import annotations

import fcntl
import json
import os
import shutil
from typing import Any, Dict, List

import numpy as np

from .errors import SnapshotCorruptError, StageBusyError
from .hashing import checksum_file, checksum_json

STAGES = ("ingest", "registration", "geometry", "texture")
MANIFEST_FILE = "snapshot.json"
MANIFEST_CHECKSUM_FILE = "snapshot.sha256"
COMMIT_LOCK_FILE = "commit.lock"


class CommitLock:
    """Process-wide exclusive commit lock backed by a non-blocking flock."""

    def __init__(self, store: "SnapshotStore"):
        self._store = store
        self._handle = None

    def acquire(self) -> "CommitLock":
        os.makedirs(self._store.root, exist_ok=True)
        path = os.path.join(self._store.root, COMMIT_LOCK_FILE)
        handle = open(path, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise StageBusyError(
                "another stage commit is currently in effect",
                stage="snapshot",
                details={"lock_file": path},
            ) from exc
        self._handle = handle
        return self

    def release(self) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None

    def __enter__(self) -> "CommitLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


class SnapshotStore:
    """On-disk layout: ``root/<scene_id>/<stage>/v<version>/...``."""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    # ------------------------------------------------------------------ paths
    def stage_dir(self, scene_id: str, stage: str) -> str:
        return os.path.join(self.root, scene_id, stage)

    def version_dir(self, scene_id: str, stage: str, version: int) -> str:
        return os.path.join(self.stage_dir(scene_id, stage), f"v{version}")

    def lock(self) -> CommitLock:
        return CommitLock(self)

    # --------------------------------------------------------------- versions
    def current_version(self, scene_id: str, stage: str) -> int:
        stage_dir = self.stage_dir(scene_id, stage)
        if not os.path.isdir(stage_dir):
            return 0
        versions = [
            int(name[1:]) for name in os.listdir(stage_dir)
            if name.startswith("v") and name[1:].isdigit()
        ]
        return max(versions, default=0)

    def list_scene_ids(self) -> List[str]:
        if not os.path.isdir(self.root):
            return []
        return sorted(
            name for name in os.listdir(self.root)
            if os.path.isdir(os.path.join(self.root, name))
        )

    def stage_scene_ids(self, stage: str) -> List[str]:
        scene_ids: List[str] = []
        for scene_id in self.list_scene_ids():
            if self.current_version(scene_id, stage) > 0:
                scene_ids.append(scene_id)
        return sorted(scene_ids)

    # ----------------------------------------------------------------- commit
    def save_array(self, version_dir: str, name: str, array: np.ndarray) -> str:
        path = os.path.join(version_dir, name)
        with open(path, "wb") as handle:
            np.save(handle, np.ascontiguousarray(array), allow_pickle=False)
        return checksum_file(path)

    def try_commit(self, scene_id: str, stage: str,
                   payload: Dict[str, Any],
                   arrays: Dict[str, np.ndarray] | None = None) -> Dict[str, Any]:
        """Atomically replace the stage snapshot with the next version.

        Returns the committed manifest.  Raises :class:`StageBusyError` if
        another commit holds the lock, and :class:`SnapshotCorruptError` on
        checksum failures during verification.
        """
        with self.lock():
            next_version = self.current_version(scene_id, stage) + 1
            target_dir = self.version_dir(scene_id, stage, next_version)
            temp_dir = target_dir + ".tmp"
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
            os.makedirs(temp_dir)

            array_checksums: Dict[str, str] = {}
            for name, array in (arrays or {}).items():
                array_checksums[name] = self.save_array(temp_dir, name, array)

            body_checksum = checksum_json(payload)
            manifest = {
                "scene_id": scene_id,
                "stage": stage,
                "version": next_version,
                "body_checksum": body_checksum,
                "array_checksums": array_checksums,
                "payload": payload,
            }
            manifest_path = os.path.join(temp_dir, MANIFEST_FILE)
            with open(manifest_path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(manifest, indent=2, sort_keys=True))
                handle.write("\n")
            checksum_path = os.path.join(temp_dir, MANIFEST_CHECKSUM_FILE)
            with open(checksum_path, "w", encoding="utf-8") as handle:
                handle.write(checksum_file(manifest_path) + "\n")

            # Verify the freshly written snapshot before publishing it.
            self._verify_version_dir(temp_dir)
            os.replace(temp_dir, target_dir)
            return manifest

    def load_manifest(self, scene_id: str, stage: str,
                      version: int | None = None) -> Dict[str, Any]:
        version = version or self.current_version(scene_id, stage)
        if version <= 0:
            raise SnapshotCorruptError(
                f"no snapshot for stage {stage}",
                scene_id=scene_id, stage=stage,
            )
        path = os.path.join(
            self.version_dir(scene_id, stage, version), MANIFEST_FILE
        )
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotCorruptError(
                f"snapshot manifest unreadable: {exc}",
                scene_id=scene_id, stage=stage,
            ) from exc

    def load_array(self, scene_id: str, stage: str, name: str,
                   version: int | None = None) -> np.ndarray:
        version = version or self.current_version(scene_id, stage)
        path = os.path.join(
            self.version_dir(scene_id, stage, version), name
        )
        try:
            with open(path, "rb") as handle:
                return np.load(handle, allow_pickle=False)
        except (OSError, ValueError) as exc:
            raise SnapshotCorruptError(
                f"array {name} unreadable: {exc}",
                scene_id=scene_id, stage=stage,
            ) from exc

    # --------------------------------------------------------------- verify
    def _verify_version_dir(self, version_dir: str) -> None:
        manifest_path = os.path.join(version_dir, MANIFEST_FILE)
        checksum_path = os.path.join(version_dir, MANIFEST_CHECKSUM_FILE)
        try:
            with open(manifest_path, "r", encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotCorruptError(
                f"manifest unreadable in {version_dir}: {exc}",
                scene_id=os.path.basename(os.path.dirname(version_dir)),
                stage=os.path.basename(os.path.dirname(os.path.dirname(version_dir))),
            ) from exc
        try:
            with open(checksum_path, "r", encoding="utf-8") as handle:
                expected_manifest_checksum = handle.read().strip()
        except OSError as exc:
            raise SnapshotCorruptError(
                "manifest checksum sidecar missing",
                scene_id=manifest.get("scene_id", "unknown"),
                stage=manifest.get("stage", "unknown"),
            ) from exc
        if checksum_file(manifest_path) != expected_manifest_checksum:
            raise SnapshotCorruptError(
                "snapshot manifest checksum mismatch",
                scene_id=manifest.get("scene_id", "unknown"),
                stage=manifest.get("stage", "unknown"),
            )

        payload = manifest.get("payload")
        if payload is None or checksum_json(payload) != manifest.get("body_checksum"):
            raise SnapshotCorruptError(
                "payload checksum mismatch",
                scene_id=manifest.get("scene_id", "unknown"),
                stage=manifest.get("stage", "unknown"),
            )
        for name, expected in manifest.get("array_checksums", {}).items():
            array_path = os.path.join(version_dir, name)
            if not os.path.exists(array_path) or checksum_file(array_path) != expected:
                raise SnapshotCorruptError(
                    f"array checksum mismatch for {name}",
                    scene_id=manifest.get("scene_id", "unknown"),
                    stage=manifest.get("stage", "unknown"),
                )

    def verify(self, scene_id: str, stage: str,
               version: int | None = None) -> Dict[str, Any]:
        version = version or self.current_version(scene_id, stage)
        if version <= 0:
            raise SnapshotCorruptError(
                f"missing snapshot for stage {stage}",
                scene_id=scene_id, stage=stage,
            )
        version_dir = self.version_dir(scene_id, stage, version)
        self._verify_version_dir(version_dir)
        return self.load_manifest(scene_id, stage, version)

