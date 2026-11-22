"""Whole-file, content-addressed, monotonically versioned snapshots."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigInvalid, SnapshotCorrupt, SnapshotRollback
from .utils import hash_payload, to_jsonable

SNAPSHOT_FORMAT = "ts-precision-snapshot/v1"


def _snapshot_path(root: Path, name: str) -> Path:
    if not name or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in name):
        raise ConfigInvalid(
            "snapshot.name",
            "snapshot name must contain only lowercase letters, digits and underscore",
        )
    return root / f"{name}.json"


class SnapshotStore:
    """A tiny local store where every commit replaces the entire snapshot file."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def path_for(self, name: str) -> Path:
        return _snapshot_path(self.directory, name)

    def load(self, name: str) -> dict[str, Any] | None:
        path = self.path_for(name)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                envelope = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotCorrupt(
                name,
                None,
                f"snapshot {name!r} cannot be read: {exc}",
            ) from exc
        if not isinstance(envelope, Mapping):
            raise SnapshotCorrupt(name, None, "snapshot envelope must be an object")
        version_raw = envelope.get("version")
        version = version_raw if isinstance(version_raw, int) and not isinstance(version_raw, bool) else None
        if (
            envelope.get("format") != SNAPSHOT_FORMAT
            or envelope.get("snapshot_name") != name
            or version is None
            or version < 1
            or not isinstance(envelope.get("payload"), Mapping)
            or not isinstance(envelope.get("checksum"), str)
        ):
            raise SnapshotCorrupt(
                name,
                version,
                f"snapshot {name!r} has an invalid envelope",
            )
        expected = hash_payload(envelope["payload"])
        if expected != envelope["checksum"]:
            raise SnapshotCorrupt(
                name,
                version,
                (
                    f"checksum mismatch for snapshot {name!r} version {version}: "
                    f"expected {expected}, found {envelope['checksum']}"
                ),
            )
        return {
            "format": SNAPSHOT_FORMAT,
            "snapshot_name": name,
            "version": int(version),
            "checksum": str(envelope["checksum"]),
            "payload": dict(envelope["payload"]),
        }

    def next_version(self, name: str) -> int:
        existing = self.load(name)
        return 1 if existing is None else int(existing["version"]) + 1

    def commit(
        self,
        name: str,
        payload: Mapping[str, Any],
        *,
        version: int | None = None,
    ) -> dict[str, Any]:
        if version is not None and (
            isinstance(version, bool) or not isinstance(version, int) or version < 1
        ):
            raise ConfigInvalid("snapshot.version", "snapshot version must be >= 1")
        existing = self.load(name)
        checksum = hash_payload(payload)
        if existing is None:
            target_version = 1 if version is None else version
        else:
            if version is None:
                target_version = int(existing["version"]) + 1
            else:
                target_version = int(version)
            if target_version < int(existing["version"]):
                raise SnapshotRollback(
                    name,
                    target_version,
                    (
                        f"cannot replace snapshot {name!r} version "
                        f"{existing['version']} with older version {target_version}"
                    ),
                )
            if target_version == int(existing["version"]):
                if checksum == existing["checksum"]:
                    return existing
                raise SnapshotRollback(
                    name,
                    target_version,
                    (
                        f"cannot overwrite snapshot {name!r} version {target_version} "
                        "with different content"
                    ),
                )
        envelope = {
            "format": SNAPSHOT_FORMAT,
            "snapshot_name": name,
            "version": target_version,
            "checksum": checksum,
            "payload": to_jsonable(payload),
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(name)
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self.directory,
                prefix=f".{name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = handle.name
                json.dump(
                    envelope,
                    handle,
                    ensure_ascii=False,
                    allow_nan=False,
                    indent=2,
                    sort_keys=True,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass
        loaded = self.load(name)
        if loaded is None:
            raise SnapshotCorrupt(name, target_version, "snapshot disappeared after commit")
        return loaded

    def commit_bundle(
        self,
        payloads: Mapping[str, Mapping[str, Any]],
        versions: Mapping[str, int] | None = None,
    ) -> dict[str, dict[str, Any]]:
        versions = versions or {}
        return {
            name: self.commit(
                name,
                payload,
                version=versions.get(name),
            )
            for name, payload in payloads.items()
        }

    def sync(
        self,
        payloads: Mapping[str, Mapping[str, Any]],
        versions: Mapping[str, int] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Load-or-replace snapshots after restart.

        Identical content is retained even at the same version; changed content
        receives the explicit version or the next monotonic version.
        """

        versions = versions or {}
        result: dict[str, dict[str, Any]] = {}
        for name, payload in payloads.items():
            checksum = hash_payload(payload)
            existing = self.load(name)
            if (
                existing is not None
                and existing["checksum"] == checksum
                and (
                    name not in versions
                    or int(versions[name]) == int(existing["version"])
                )
            ):
                result[name] = existing
                continue
            result[name] = self.commit(
                name,
                payload,
                version=versions.get(name),
            )
        return result

    def load_all(self, names: list[str]) -> dict[str, dict[str, Any] | None]:
        return {name: self.load(name) for name in names}
