from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import BusinessError
from .util import (
    atomic_write_json,
    canonical_json_bytes,
    read_json,
    sanitize_component,
    sha256_bytes,
)
from .workspace import Workspace


VALID_KINDS = frozenset({"plan", "tolerance", "stats"})


class SnapshotStore:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.paths = workspace.paths
        self.paths.ensure()

    def _path(self, kind: str, key: str) -> Path:
        if kind not in VALID_KINDS:
            raise BusinessError(
                "E_POLICY_INVALID",
                f"snapshot kind must be one of {sorted(VALID_KINDS)}",
                field="snapshot.kind",
            )
        return self.paths.snapshots / kind / f"{sanitize_component(key)}.json"

    @staticmethod
    def content_checksum(content: Any) -> str:
        return sha256_bytes(canonical_json_bytes(content))

    @staticmethod
    def envelope_checksum(envelope: dict[str, Any]) -> str:
        material = {
            "schema": envelope["schema"],
            "kind": envelope["kind"],
            "key": envelope["key"],
            "version": envelope["version"],
            "content_checksum": envelope["content_checksum"],
            "previous_version": envelope["previous_version"],
            "replacement": envelope["replacement"],
        }
        return sha256_bytes(canonical_json_bytes(material))

    def read(self, kind: str, key: str) -> dict[str, Any] | None:
        path = self._path(kind, key)
        if not path.exists():
            return None
        envelope = read_json(path, code="E_CACHE_CORRUPT")
        if not isinstance(envelope, dict):
            raise BusinessError(
                "E_CACHE_CORRUPT", f"snapshot {path} is not an object", field=str(path)
            )
        required = {
            "schema",
            "kind",
            "key",
            "version",
            "content",
            "content_checksum",
            "previous_version",
            "replacement",
            "envelope_checksum",
        }
        if not required.issubset(envelope):
            raise BusinessError(
                "E_CACHE_CORRUPT",
                f"snapshot {path} lacks required fields",
                field=str(path),
                details={"missing": sorted(required - set(envelope))},
            )
        if (
            isinstance(envelope.get("version"), bool)
            or not isinstance(envelope.get("version"), int)
            or envelope["version"] < 1
        ):
            raise BusinessError(
                "E_CACHE_CORRUPT",
                "snapshot version is not a positive integer",
                field="snapshot.version",
                details={"path": str(path)},
            )
        try:
            actual_content = self.content_checksum(envelope["content"])
            actual_envelope = self.envelope_checksum(envelope)
        except (TypeError, ValueError) as exc:
            raise BusinessError(
                "E_CACHE_CORRUPT", f"snapshot {path} cannot be verified: {exc}", field=str(path)
            ) from exc
        if actual_content != envelope["content_checksum"]:
            raise BusinessError(
                "E_CACHE_CORRUPT",
                "snapshot content checksum mismatch",
                field="snapshot.content_checksum",
                details={"path": str(path)},
            )
        if actual_envelope != envelope["envelope_checksum"]:
            raise BusinessError(
                "E_CACHE_CORRUPT",
                "snapshot envelope checksum mismatch",
                field="snapshot.envelope_checksum",
                details={"path": str(path)},
            )
        return envelope

    def _write_whole(
        self,
        kind: str,
        key: str,
        version: int,
        content: Any,
        *,
        previous_version: int | None,
    ) -> dict[str, Any]:
        content_checksum = self.content_checksum(content)
        envelope: dict[str, Any] = {
            "schema": 1,
            "kind": kind,
            "key": key,
            "version": int(version),
            "content": content,
            "content_checksum": content_checksum,
            "previous_version": previous_version,
            "replacement": "atomic-whole",
        }
        envelope["envelope_checksum"] = self.envelope_checksum(envelope)
        atomic_write_json(self._path(kind, key), envelope, pretty=True)
        return envelope

    def put(self, kind: str, key: str, version: int, content: Any) -> dict[str, Any]:
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise BusinessError(
                "E_POLICY_INVALID",
                "snapshot version must be a positive integer",
                field="snapshot.version",
            )
        current = self.read(kind, key)
        content_checksum = self.content_checksum(content)
        if current is None:
            return self._write_whole(
                kind, key, version, content, previous_version=None
            )
        current_version = int(current["version"])
        if version < current_version:
            raise BusinessError(
                "E_SNAPSHOT_ROLLBACK",
                f"snapshot version {version} would roll back existing version {current_version}",
                field=f"snapshot.{kind}.{key}.version",
                details={"kind": kind, "key": key, "existing": current_version, "incoming": version},
            )
        if version == current_version:
            if content_checksum != current["content_checksum"]:
                raise BusinessError(
                    "E_SNAPSHOT_ROLLBACK",
                    "same snapshot version cannot replace different content",
                    field=f"snapshot.{kind}.{key}.version",
                    details={"kind": kind, "key": key, "version": version},
                )
            return current
        return self._write_whole(
            kind, key, version, content, previous_version=current_version
        )

    def write_next(self, kind: str, key: str, content: Any) -> dict[str, Any]:
        current = self.read(kind, key)
        if current is not None and current["content_checksum"] == self.content_checksum(content):
            return {**current, "changed": False}
        version = int(current["version"]) + 1 if current is not None else 1
        envelope = self.put(kind, key, version, content)
        return {**envelope, "changed": True}

    def list(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if not self.paths.snapshots.exists():
            return items
        for path in sorted(self.paths.snapshots.glob("*/*.json")):
            kind = path.parent.name
            key = path.stem
            envelope = self.read(kind, key)
            if envelope is None:
                continue
            items.append(
                {
                    "kind": envelope["kind"],
                    "key": envelope["key"],
                    "version": envelope["version"],
                    "content_checksum": envelope["content_checksum"],
                    "envelope_checksum": envelope["envelope_checksum"],
                    "previous_version": envelope["previous_version"],
                    "replacement": envelope["replacement"],
                    "path": str(path),
                }
            )
        return items

    def verify_all(self) -> list[dict[str, Any]]:
        return self.list()

