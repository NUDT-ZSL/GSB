"""Versioned, checksummed snapshots with atomic whole-snapshot replacement."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from .canonical import canonical_dumps, content_checksum
from .errors import VersionRollback
from .index import InvertedIndex, PartitionStats
from .model import BridgeResource


def attach_checksum(bridge: BridgeResource) -> BridgeResource:
    payload = {
        "directions": [list(pair) for pair in sorted(bridge.directions)],
        "entries": bridge.to_dict()["entries"],
    }
    bridge.checksum = content_checksum(payload)
    return bridge


def index_checksum(index: InvertedIndex) -> str:
    payload = {
        lang: {
            "doc_count": part.doc_count,
            "total_len": part.total_len,
            "avg_len": part.avg_len,
            "df": part.df,
            "postings": {
                term: [[p.doc_id, p.tf] for p in postings]
                for term, postings in sorted(part.postings.items())
            },
        }
        for lang, part in sorted(index.partitions.items())
    }
    return content_checksum(payload)


def partition_checksum(partition: PartitionStats) -> str:
    payload = {
        "doc_count": partition.doc_count,
        "total_len": partition.total_len,
        "avg_len": partition.avg_len,
        "df": partition.df,
        "postings": {
            term: [[p.doc_id, p.tf] for p in postings]
            for term, postings in sorted(partition.postings.items())
        },
    }
    return content_checksum(payload)


def atomic_write(path: Path, obj: Any) -> None:
    """Write atomically: temp file in the same dir, fsync, then os.replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_dumps(obj).encode("utf-8") if not isinstance(obj, (bytes, bytearray)) else bytes(obj)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


@dataclass
class SnapshotRef:
    resource: str
    version: int
    checksum: str
    path: str

    def to_dict(self) -> dict:
        return {
            "resource": self.resource,
            "version": self.version,
            "checksum": self.checksum,
            "path": self.path,
        }


class SnapshotStore:
    """Owns the on-disk snapshot layout and the monotonic version ledger."""

    BRIDGE = "bridge"
    DIRECTIONS = "directions"
    INDEX = "index"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.root / "ledger.json"

    # ------------------------------------------------------------------
    def _load_ledger(self) -> Dict[str, SnapshotRef]:
        if not self.ledger_path.exists():
            return {}
        with open(self.ledger_path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        return {key: SnapshotRef(**value) for key, value in raw.items()}

    def _write_ledger(self, ledger: Dict[str, SnapshotRef]) -> None:
        payload = {key: value.to_dict() for key, value in sorted(ledger.items())}
        atomic_write(self.ledger_path, payload)

    def _enforce_monotonic(self, resource: str, new_version: int) -> None:
        ledger = self._load_ledger()
        current = ledger.get(resource)
        if current is not None and new_version <= current.version:
            raise VersionRollback(resource, current.version, new_version)

    # ------------------------------------------------------------------
    def publish_bridge(self, bridge: BridgeResource) -> SnapshotRef:
        attach_checksum(bridge)
        self._enforce_monotonic(self.BRIDGE, bridge.version)
        path = self.root / f"bridge_v{bridge.version}.json"
        atomic_write(path, bridge.to_dict())
        return self._update_ref(self.BRIDGE, bridge.version, bridge.checksum, path)

    def publish_directions(self, version: int, directions: List) -> SnapshotRef:
        payload = {"directions": [list(pair) for pair in sorted(directions)]}
        checksum = content_checksum(payload)
        self._enforce_monotonic(self.DIRECTIONS, version)
        path = self.root / f"directions_v{version}.json"
        atomic_write(path, {"version": version, "checksum": checksum, **payload})
        return self._update_ref(self.DIRECTIONS, version, checksum, path)

    def publish_index(self, index: InvertedIndex) -> SnapshotRef:
        for partition in index.partitions.values():
            partition.version = index.version
            partition.checksum = partition_checksum(partition)
        checksum = index_checksum(index)
        self._enforce_monotonic(self.INDEX, index.version)
        path = self.root / f"index_v{index.version}.json"
        atomic_write(
            path,
            {
                "version": index.version,
                "checksum": checksum,
                "partitions": {
                    lang: partition.to_dict()
                    for lang, partition in sorted(index.partitions.items())
                },
            },
        )
        return self._update_ref(self.INDEX, index.version, checksum, path)

    def _update_ref(self, resource: str, version: int, checksum: str, path: Path) -> SnapshotRef:
        ledger = self._load_ledger()
        ref = SnapshotRef(resource, version, checksum, str(path))
        ledger[resource] = ref
        self._write_ledger(ledger)
        return ref

    # ------------------------------------------------------------------
    def current(self, resource: str):
        return self._load_ledger().get(resource)

    def all_current(self) -> Dict[str, SnapshotRef]:
        return self._load_ledger()

    def verify_checksum(self, resource: str, obj: Any, expected: str) -> bool:
        if resource == self.BRIDGE:
            attach_checksum(obj)
            return obj.checksum == expected
        if resource == self.INDEX:
            return index_checksum(obj) == expected
        raise KeyError(resource)
