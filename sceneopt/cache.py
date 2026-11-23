"""Content-addressed stage cache and non-blocking commit gate."""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from .errors import CacheCorrupt, StageBusy
from .serialization import (
    canonical_bytes,
    content_checksum,
    from_jsonable,
    sha256_bytes,
)

CACHE_FORMAT = "sceneopt-cache-entry-v1"


def build_stage_cache_key(
    stage: str,
    *,
    scene_content_hash: str,
    camera_pose_digest: str,
    geometry_mode: str,
    target_face_budget: int,
    upstream_artifact_hash: str,
    config_digest: str,
    effective_face_budget: int | None = None,
) -> str:
    """Build the required content key.

    The key intentionally includes every scene/configuration field named by the
    task, even for upstream stages where one field may be a logical no-op.
    """

    material = {
        "stage": str(stage),
        "scene_content_hash": str(scene_content_hash),
        "camera_pose_digest": str(camera_pose_digest),
        "geometry_mode": str(geometry_mode),
        "target_face_budget": int(target_face_budget),
        "upstream_artifact_hash": str(upstream_artifact_hash),
        "config_digest": str(config_digest),
        "effective_face_budget": (
            int(target_face_budget)
            if effective_face_budget is None
            else int(effective_face_budget)
        ),
    }
    return content_checksum(material)


def _atomic_write(path: str, data: bytes) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


class ArtifactCache:
    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    def _path(self, stage: str, key: str) -> str:
        if not stage or any(character not in "abcdefghijklmnopqrstuvwxyz_0123456789" for character in stage):
            raise ValueError(f"invalid stage name: {stage!r}")
        if len(key) != 64 or any(character not in "0123456789abcdef" for character in key):
            raise ValueError(f"invalid cache key: {key!r}")
        return os.path.join(self.root, stage, f"{key}.json")

    def put(self, stage: str, key: str, payload: dict[str, Any]) -> str:
        payload_bytes = canonical_bytes(payload)
        checksum = sha256_bytes(payload_bytes)
        entry = {
            "format": CACHE_FORMAT,
            "stage": stage,
            "key": key,
            "checksum": checksum,
            "payload": payload,
        }
        _atomic_write(self._path(stage, key), canonical_bytes(entry))
        return checksum

    def read_entry(self, stage: str, key: str) -> tuple[dict[str, Any], dict[str, Any]]:
        path = self._path(stage, key)
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
            document = from_jsonable(json.loads(raw.decode("utf-8")))
        except FileNotFoundError:
            raise
        except Exception as exc:
            raise CacheCorrupt(stage, key, f"unreadable JSON: {exc}") from exc
        if not isinstance(document, dict):
            raise CacheCorrupt(stage, key, "cache entry is not an object")
        if document.get("format") != CACHE_FORMAT:
            raise CacheCorrupt(stage, key, "unknown cache format")
        if document.get("stage") != stage or document.get("key") != key:
            raise CacheCorrupt(stage, key, "cache locator does not match file name")
        payload = document.get("payload")
        if not isinstance(payload, dict):
            raise CacheCorrupt(stage, key, "payload is not an object")
        expected = document.get("checksum")
        actual = content_checksum(payload)
        if expected != actual:
            raise CacheCorrupt(stage, key, "payload checksum mismatch")
        return document, payload

    def get(self, stage: str, key: str) -> dict[str, Any] | None:
        try:
            _entry, payload = self.read_entry(stage, key)
            return payload
        except FileNotFoundError:
            return None

    def exists(self, stage: str, key: str) -> bool:
        return os.path.isfile(self._path(stage, key))


class CommitGate:
    """Process-wide and cross-process non-blocking exclusive lock."""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, stage: str = "incremental_commit") -> None:
        if self._fd is not None:
            raise StageBusy(stage=stage, reason="gate is already held")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as exc:
            raise StageBusy(stage=stage, lock_path=self.path) from exc
        self._fd = descriptor
        try:
            os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            self._fd = None
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
            raise

    def release(self) -> None:
        if self._fd is None:
            return
        descriptor = self._fd
        self._fd = None
        try:
            os.close(descriptor)
        finally:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass

    def __enter__(self) -> "CommitGate":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()

