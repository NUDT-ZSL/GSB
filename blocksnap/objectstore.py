"""Content-addressed object store for CoW blocks, bitmaps and the baseline."""
from __future__ import annotations

import os

from .digest import content_object_id, object_relpath, sha256_hex
from .errors import ChainError, IntegrityError


class ObjectStore:
    def __init__(self, store_dir: str):
        self.store_dir = store_dir
        os.makedirs(os.path.join(store_dir, "objects", "sha256"), exist_ok=True)

    def _path_for(self, object_id: str) -> str:
        return os.path.join(self.store_dir, object_relpath(object_id))

    def put(self, data: bytes) -> tuple[str, str]:
        digest = sha256_hex(data)
        object_id = content_object_id(digest)
        path = self._path_for(object_id)
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        return object_id, digest

    def exists(self, object_id: str) -> bool:
        return os.path.exists(self._path_for(object_id))

    def get(self, object_id: str) -> bytes:
        path = self._path_for(object_id)
        if not os.path.exists(path):
            raise ChainError(
                "snapshot chain object reference missing",
                location="objectstore.ObjectStore.get",
                missing_object=object_id,
                expected_path=path,
            )
        with open(path, "rb") as handle:
            return handle.read()

    def get_verified(self, object_id: str, expected_digest: str,
                     block_index: int) -> bytes:
        data = self.get(object_id)
        actual = sha256_hex(data)
        if actual != expected_digest:
            raise IntegrityError(
                "referenced block digest mismatch",
                location="objectstore.ObjectStore.get_verified",
                block_index=block_index,
                object_id=object_id,
                expected_digest=expected_digest,
                actual_digest=actual,
            )
        return data

    def verify_object(self, object_id: str, expected_digest: str) -> None:
        data = self.get(object_id)
        actual = sha256_hex(data)
        if actual != expected_digest:
            raise IntegrityError(
                "object digest mismatch",
                location="objectstore.ObjectStore.verify_object",
                object_id=object_id,
                expected_digest=expected_digest,
                actual_digest=actual,
            )
