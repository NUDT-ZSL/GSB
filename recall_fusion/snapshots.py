"""Versioned, checksummed snapshots with atomic whole-snapshot replacement.

Three snapshot kinds are managed:

* ``recall_stats``   - per-channel recall statistics
* ``norm_params``    - normalization parameters
* ``fusion_weights`` - channel fusion weights

Each submission is written to a new immutable version file. The current
manifest only ever points at one complete version per kind, so a swap is an
all-or-nothing replacement, never an in-place incremental patch. Older
versions can never overwrite newer ones (E_SNAPSHOT_ROLLBACK); a tampered file
or checksum mismatch raises E_SNAPSHOT_CORRUPT naming the snapshot and version.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BusinessError, E_SNAPSHOT_CORRUPT, E_SNAPSHOT_ROLLBACK

SNAPSHOT_KINDS = ("recall_stats", "norm_params", "fusion_weights")


def checksum_payload(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass
class Snapshot:
    name: str
    version: int
    checksum: str
    payload: dict[str, Any]


class SnapshotStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "manifest.json"
        self.manifest = self._load_manifest()

    def _load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {"current": {kind: None for kind in SNAPSHOT_KINDS}}
        with open(self.manifest_path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def _write_manifest(self) -> None:
        tmp = self.manifest_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(self.manifest, handle, sort_keys=True, indent=2)
        tmp.replace(self.manifest_path)

    def _version_path(self, name: str, version: int) -> Path:
        return self.root / f"{name}_v{version:06d}.json"

    def submit(self, name: str, version: int, payload: dict[str, Any]) -> Snapshot:
        if name not in SNAPSHOT_KINDS:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                f"unknown snapshot kind {name!r}",
                stage="snapshot",
                field=name,
            )
        current_version = self.manifest["current"].get(name)
        if current_version is not None and version <= current_version:
            raise BusinessError(
                E_SNAPSHOT_ROLLBACK,
                f"refusing version {version} for {name}: current version is "
                f"{current_version} and versions must be monotonically increasing",
                stage="snapshot",
                field=name,
            )
        checksum = checksum_payload(payload)
        record = {
            "name": name,
            "version": version,
            "checksum": checksum,
            "payload": payload,
        }
        path = self._version_path(name, version)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(record, handle, sort_keys=True, indent=2)
        tmp.replace(path)
        # Whole-snapshot replacement: only after the new file is durably stored
        # does the manifest atomically repoint the current version.
        self.manifest["current"][name] = version
        self._write_manifest()
        return Snapshot(name, version, checksum, payload)

    def load(self, name: str, version: int | None = None) -> Snapshot:
        target_version = version or self.manifest["current"].get(name)
        if target_version is None:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                f"no snapshot exists for {name}",
                stage="snapshot",
                field=name,
            )
        path = self._version_path(name, target_version)
        if not path.exists():
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                f"snapshot file missing for {name} v{target_version}",
                stage="snapshot",
                field=name,
            )
        try:
            with open(path, "r", encoding="utf-8") as handle:
                record = json.load(handle)
        except json.JSONDecodeError as exc:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                f"unreadable snapshot file: {exc}",
                stage="snapshot",
                field=f"{name}@v{target_version}",
            ) from exc
        actual = checksum_payload(record.get("payload", {}))
        if actual != record.get("checksum"):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                f"checksum mismatch: stored={record.get('checksum')} "
                f"actual={actual}",
                stage="snapshot",
                field=f"{name}@v{target_version}",
            )
        if record.get("version") != target_version or record.get("name") != name:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot header does not match manifest pointer",
                stage="snapshot",
                field=f"{name}@v{target_version}",
            )
        return Snapshot(name, target_version, actual, record["payload"])

    def current_version(self, name: str) -> int | None:
        return self.manifest["current"].get(name)

    def list_versions(self, name: str) -> list[int]:
        prefix = f"{name}_v"
        versions = []
        for path in self.root.glob(f"{name}_v*.json"):
            versions.append(int(path.stem[len(prefix):]))
        return sorted(versions)
