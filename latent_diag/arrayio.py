from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from .hashing import canonical_json_bytes, hash_array


class BlobStore:
    """Content-addressed storage for arrays and canonical JSON payloads."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.blob_dir = self.root / "blobs"
        self.blob_dir.mkdir(parents=True, exist_ok=True)

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def absolute_path(self, relative_path: str) -> Path:
        path = (self.root / relative_path).resolve()
        root = self.root.resolve()
        if root != path and root not in path.parents:
            raise ValueError(f"artifact path escapes cache root: {relative_path}")
        return path

    def put_array(self, array: np.ndarray) -> tuple[str, str]:
        content_hash = hash_array(array)
        destination = self.blob_dir / f"{content_hash}.npy"
        if destination.exists():
            return content_hash, self._relative(destination)

        fd, temp_name = tempfile.mkstemp(prefix=".array-", dir=self.blob_dir)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            with temp_path.open("wb") as handle:
                np.save(handle, np.ascontiguousarray(array), allow_pickle=False)
            os.replace(temp_path, destination)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        return content_hash, self._relative(destination)

    def put_json(self, value: Any) -> tuple[str, str]:
        data = canonical_json_bytes(value)
        content_hash = hash_array_json_bytes(data)
        destination = self.blob_dir / f"{content_hash}.json"
        if destination.exists():
            return content_hash, self._relative(destination)

        fd, temp_name = tempfile.mkstemp(prefix=".json-", dir=self.blob_dir)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            with temp_path.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, destination)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        return content_hash, self._relative(destination)

    def load_array(self, relative_path: str, expected_hash: str | None = None) -> np.ndarray:
        path = self.absolute_path(relative_path)
        if not path.exists():
            raise FileNotFoundError(path)
        array = np.load(path, allow_pickle=False)
        if expected_hash is not None and hash_array(array) != expected_hash:
            raise ValueError(f"content hash mismatch for {relative_path}")
        return array

    def load_json(self, relative_path: str, expected_hash: str | None = None) -> Any:
        path = self.absolute_path(relative_path)
        if not path.exists():
            raise FileNotFoundError(path)
        data = path.read_bytes()
        if expected_hash is not None and hash_array_json_bytes(data) != expected_hash:
            raise ValueError(f"content hash mismatch for {relative_path}")
        import json

        return json.loads(data.decode("utf-8"))

    def validate_descriptor(self, descriptor: dict[str, Any]) -> bool:
        path = descriptor.get("artifact_path")
        expected_hash = descriptor.get("content_hash")
        kind = descriptor.get("storage", "array")
        if not path or not expected_hash:
            return False
        try:
            if kind == "json":
                self.load_json(path, expected_hash)
            else:
                self.load_array(path, expected_hash)
            return True
        except (FileNotFoundError, ValueError, OSError):
            return False


def hash_array_json_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(b"ldiag-json-v1\0" + data).hexdigest()

