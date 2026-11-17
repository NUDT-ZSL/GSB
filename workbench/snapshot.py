"""Versioned snapshot with atomic whole-artifact replacement and recovery."""

from __future__ import annotations

import hashlib
import json
import os

from .cache import atomic_write_json
from .errors import E_SNAPSHOT_STALE, WorkbenchError

MANIFEST_NAME = "manifest.json"


def config_fingerprint(config):
    blob = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class SnapshotManager:
    def __init__(self, directory):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        self.manifest_path = os.path.join(directory, MANIFEST_NAME)

    def load_manifest(self):
        if not os.path.exists(self.manifest_path):
            return None
        with open(self.manifest_path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def write_manifest(self, manifest):
        atomic_write_json(self.manifest_path, manifest)

    def validate(self, snapshot_version, config_fp):
        """Return (status, manifest_or_none, reason_or_none).

        status: "fresh" (no manifest), "hit", "stale".
        """
        manifest = self.load_manifest()
        if manifest is None:
            return "fresh", None, None
        if manifest.get("global_version") != snapshot_version:
            return "stale", manifest, (
                f"manifest version {manifest.get('global_version')} != "
                f"configured {snapshot_version}"
            )
        if manifest.get("config_fingerprint") != config_fp:
            return "stale", manifest, "config fingerprint mismatch"
        return "hit", manifest, None

    def require_valid(self, snapshot_version, config_fp):
        status, manifest, reason = self.validate(snapshot_version, config_fp)
        if status == "stale":
            raise WorkbenchError(E_SNAPSHOT_STALE, reason)
        return manifest
