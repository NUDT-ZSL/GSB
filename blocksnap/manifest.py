"""Versioned snapshot manifest with content digest and whole-replace semantics.

Manifest layout (canonical JSON, sorted keys)::

    {
      "format_version": 1,           # integer manifest format version
      "sequence": 3,                 # snapshot sequence / snapshot version no.
      "block_size": 4096,
      "block_count": 64,
      "baseline_object": "sha256-....",
      "snapshots": [ SnapshotEntry, ... ],
      "content_digest": "sha256:...."
    }

``content_digest`` covers everything except the digest field itself, so any
single-bit change to an entry invalidates the stored digest.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

from .digest import sha256_hex
from .errors import SnapshotError

MANIFEST_FILENAME = "manifest.json"
MANIFEST_BACKUP_SUFFIX = ".bak"


@dataclass(frozen=True)
class BlockRef:
    """Reference to one CoW data block held in the object store."""

    block_index: int
    object_id: str
    digest: str
    length: int


@dataclass
class SnapshotEntry:
    snapshot_id: str
    name: str
    sequence: int                     # snapshot version number (1-based)
    created_volume_version: int      # volume_version after creation
    bitmap_object: str               # object id of the packed period bitmap
    bitmap_length: int               # packed bitmap byte length
    changed_count: int
    blocks: list[BlockRef] = field(default_factory=list)
    state_digest: str = ""           # whole-volume sha256 at snapshot time
    parent_snapshot_id: str | None = None
    # Full block-reference map captured at snapshot creation. Content
    # addressing dedupes unchanged blocks against baseline/prior objects, so
    # this costs one new object only for genuinely new block content.
    state_blocks: list[BlockRef] = field(default_factory=list)

    def block_ref_map(self) -> dict[int, BlockRef]:
        return {ref.block_index: ref for ref in self.blocks}

    def state_ref_map(self) -> dict[int, BlockRef]:
        return {ref.block_index: ref for ref in self.state_blocks}

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["blocks"] = [asdict(ref) for ref in self.blocks]
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> "SnapshotEntry":
        blocks = [BlockRef(**ref) for ref in payload.get("blocks", [])]
        state_blocks = [BlockRef(**ref) for ref in payload.get("state_blocks", [])]
        data = dict(payload)
        data["blocks"] = blocks
        data["state_blocks"] = state_blocks
        return cls(**data)


@dataclass
class Manifest:
    format_version: int
    sequence: int
    block_size: int
    block_count: int
    baseline_object: str
    snapshots: list[SnapshotEntry] = field(default_factory=list)
    content_digest: str = ""

    def names(self) -> list[str]:
        return [entry.name for entry in self.snapshots]

    def get_by_name(self, name: str) -> SnapshotEntry:
        for entry in self.snapshots:
            if entry.name == name:
                return entry
        raise SnapshotError(
            "snapshot name not found",
            location="manifest.Manifest.get_by_name",
            name=name,
            known_names=self.names(),
        )

    def get_by_id(self, snapshot_id: str) -> SnapshotEntry:
        for entry in self.snapshots:
            if entry.snapshot_id == snapshot_id:
                return entry
        raise SnapshotError(
            "snapshot id not found",
            location="manifest.Manifest.get_by_id",
            snapshot_id=snapshot_id,
        )

    def canonical_payload(self) -> dict:
        return {
            "format_version": int(self.format_version),
            "sequence": int(self.sequence),
            "block_size": int(self.block_size),
            "block_count": int(self.block_count),
            "baseline_object": self.baseline_object,
            "snapshots": [entry.to_dict() for entry in self.snapshots],
        }

    def compute_content_digest(self) -> str:
        canonical = json.dumps(
            self.canonical_payload(), sort_keys=True, separators=(",", ":")
        )
        return "sha256:" + sha256_hex(canonical.encode("utf-8"))

    def to_dict(self) -> dict:
        payload = self.canonical_payload()
        payload["content_digest"] = self.content_digest or self.compute_content_digest()
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> "Manifest":
        required = {
            "format_version",
            "sequence",
            "block_size",
            "block_count",
            "baseline_object",
            "snapshots",
            "content_digest",
        }
        missing = required - set(payload)
        if missing:
            raise SnapshotError(
                "manifest missing required fields",
                location="manifest.Manifest.from_dict",
                missing=sorted(missing),
            )
        entries = [SnapshotEntry.from_dict(item) for item in payload["snapshots"]]
        manifest = cls(
            format_version=int(payload["format_version"]),
            sequence=int(payload["sequence"]),
            block_size=int(payload["block_size"]),
            block_count=int(payload["block_count"]),
            baseline_object=str(payload["baseline_object"]),
            snapshots=entries,
            content_digest=str(payload["content_digest"]),
        )
        return manifest


def save_manifest(store_dir: str, manifest: Manifest) -> str:
    """Persist the manifest, refreshing its content digest atomically."""
    manifest.content_digest = manifest.compute_content_digest()
    path = os.path.join(store_dir, MANIFEST_FILENAME)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(manifest.to_dict(), handle, sort_keys=True, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return manifest.content_digest


def load_manifest(store_dir: str, verify_digest: bool = True) -> Manifest:
    path = os.path.join(store_dir, MANIFEST_FILENAME)
    if not os.path.exists(path):
        raise SnapshotError(
            "manifest does not exist",
            location="manifest.load_manifest",
            path=os.path.abspath(path),
        )
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    manifest = Manifest.from_dict(payload)
    if verify_digest:
        actual = manifest.compute_content_digest()
        if actual != manifest.content_digest:
            raise SnapshotError(
                "manifest content digest mismatch",
                location="manifest.load_manifest",
                expected_digest=manifest.content_digest,
                actual_digest=actual,
            )
    return manifest
