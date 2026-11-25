"""Content-addressed artifacts and atomically replaced versioned snapshots."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes, sha256_bytes, sha256_value
from .errors import E_INCONSISTENT, WorkbenchError


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class ContentAddressedCache:
    SCHEMA = "ragdiag-cache-object/v1"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.objects_dir = self.root / "objects"
        self.snapshots_dir = self.root / "snapshots"
        self.active_path = self.root / "active_snapshot.json"
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    def _object_path(self, key: str) -> Path:
        if len(key) < 2:
            raise WorkbenchError(E_INCONSISTENT, "cache key is invalid", key=key)
        return self.objects_dir / key[:2] / f"{key}.json"

    def has(self, key: str) -> bool:
        return self.get(key) is not None

    def get(self, key: str) -> Any | None:
        path = self._object_path(key)
        if not path.exists():
            self.misses += 1
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkbenchError(
                E_INCONSISTENT,
                "cache object cannot be read",
                key=key,
                error=str(exc),
            ) from exc
        if record.get("schema") != self.SCHEMA or record.get("key") != key:
            raise WorkbenchError(
                E_INCONSISTENT, "cache object identity mismatch", key=key
            )
        payload = record.get("payload")
        if record.get("payload_hash") != sha256_value(payload):
            raise WorkbenchError(
                E_INCONSISTENT, "cache object payload hash mismatch", key=key
            )
        self.hits += 1
        return payload

    def put(self, key: str, payload: Any) -> bool:
        """Store a payload. Returns True when the key already existed."""

        path = self._object_path(key)
        if path.exists():
            existing = self.get(key)
            if existing != payload:
                raise WorkbenchError(
                    E_INCONSISTENT,
                    "content-addressed cache collision",
                    key=key,
                )
            return True
        record = {
            "schema": self.SCHEMA,
            "key": key,
            "payload": payload,
            "payload_hash": sha256_value(payload),
        }
        _atomic_write(path, canonical_bytes(record) + b"\n")
        return False

    def create_snapshot(
        self,
        manifest: dict[str, Any],
        base_snapshot_id: str | None,
        activate: bool = True,
    ) -> dict[str, Any]:
        snapshot_id = sha256_value(
            {"manifest": manifest, "base_snapshot_id": base_snapshot_id}
        )
        record = {
            "schema": "ragdiag-snapshot/v1",
            "snapshot_id": snapshot_id,
            "base_snapshot_id": base_snapshot_id,
            "manifest": manifest,
            "manifest_hash": sha256_value(manifest),
        }
        path = self.snapshots_dir / f"{snapshot_id}.json"
        if not path.exists():
            _atomic_write(path, canonical_bytes(record) + b"\n")
        if activate:
            self.activate_snapshot(snapshot_id)
        return record

    def load_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        if not snapshot_id or "/" in snapshot_id or "\\" in snapshot_id:
            raise WorkbenchError(
                E_INCONSISTENT, "snapshot id is invalid", snapshot_id=snapshot_id
            )
        path = self.snapshots_dir / f"{snapshot_id}.json"
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkbenchError(
                E_INCONSISTENT,
                "snapshot cannot be loaded",
                snapshot_id=snapshot_id,
                error=str(exc),
            ) from exc
        if record.get("schema") != "ragdiag-snapshot/v1":
            raise WorkbenchError(
                E_INCONSISTENT,
                "snapshot schema mismatch",
                snapshot_id=snapshot_id,
            )
        if record.get("snapshot_id") != snapshot_id:
            raise WorkbenchError(
                E_INCONSISTENT,
                "snapshot identity mismatch",
                snapshot_id=snapshot_id,
            )
        if record.get("manifest_hash") != sha256_value(record.get("manifest")):
            raise WorkbenchError(
                E_INCONSISTENT,
                "snapshot manifest hash mismatch",
                snapshot_id=snapshot_id,
            )
        return record

    def activate_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        record = self.load_snapshot(snapshot_id)
        pointer = {
            "schema": "ragdiag-active-snapshot/v1",
            "snapshot_id": snapshot_id,
        }
        _atomic_write(
            self.active_path, canonical_bytes(pointer) + b"\n"
        )
        return record

    def active_snapshot(self) -> dict[str, Any] | None:
        if not self.active_path.exists():
            return None
        try:
            pointer = json.loads(self.active_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkbenchError(
                E_INCONSISTENT,
                "active snapshot pointer cannot be read",
                error=str(exc),
            ) from exc
        if pointer.get("schema") != "ragdiag-active-snapshot/v1":
            raise WorkbenchError(
                E_INCONSISTENT, "active snapshot pointer schema mismatch"
            )
        return self.load_snapshot(pointer["snapshot_id"])

    def list_snapshots(self) -> list[str]:
        return sorted(path.stem for path in self.snapshots_dir.glob("*.json"))

    def stats(self) -> dict[str, Any]:
        objects = list(self.objects_dir.glob("*/*.json"))
        size = sum(path.stat().st_size for path in objects)
        return {
            "root": str(self.root),
            "objects": len(objects),
            "bytes": size,
            "hits": self.hits,
            "misses": self.misses,
            "snapshots": self.list_snapshots(),
        }


def cache_key_for_material(material: dict[str, Any]) -> str:
    return sha256_value(material)


def object_digest(payload: Any) -> str:
    return sha256_bytes(canonical_bytes(payload))

