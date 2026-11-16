"""Versioned snapshot store with whole-directory atomic replacement."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

SNAPSHOT_GLOBAL_VERSION = 3
OVERLAY_VERSION = 1


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    return sha256_bytes(Path(path).read_bytes())


class SnapshotManager:
    """Manifest-versioned snapshot directory.

    Writes land in a temporary sibling directory first, then the snapshot is
    swapped as a whole (tmp -> target, old target kept as .old until the swap
    completes). Recovery only ever trusts a fully replaced manifest.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.manifest_path = self.path / "manifest.json"

    def load_manifest(self) -> dict | None:
        if not self.manifest_path.exists():
            return None
        try:
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def is_hit(self, global_version: int, config_hash: str,
               relations_hash: str) -> bool:
        m = self.load_manifest()
        if m is None:
            return False
        return (m.get("global_version") == global_version
                and m.get("config_hash") == config_hash
                and m.get("relations_hash") == relations_hash
                and m.get("status") == "complete")

    def write(self, manifest: dict, artifacts: dict[str, bytes]) -> None:
        tmp = self.path.parent / (self.path.name + ".tmp")
        old = self.path.parent / (self.path.name + ".old")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            for name, data in artifacts.items():
                dest = tmp / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            (tmp / "manifest.json").write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False),
                encoding="utf-8")
            shutil.rmtree(old, ignore_errors=True)
            if self.path.exists():
                os.replace(self.path, old)
            os.replace(tmp, self.path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.rmtree(old, ignore_errors=True)
