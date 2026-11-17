"""Stage snapshots with atomic replacement and resume-consistency checks."""

import os

from . import GLOBAL_VERSION
from .io_utils import atomic_write_json, load_json


class ConfigMismatch(Exception):
    pass


class SnapshotManager:
    def __init__(self, workdir):
        self.snapshot_dir = os.path.join(workdir, "snapshots")
        os.makedirs(self.snapshot_dir, exist_ok=True)
        self.manifest_path = os.path.join(self.snapshot_dir, "manifest.json")

    def load_manifest(self):
        if not os.path.exists(self.manifest_path):
            return None
        return load_json(self.manifest_path)

    def begin(self, config_hash, injection_hash, samples_hash):
        """Return "fresh", "stale" or "resume".

        "stale" means config/injection hashes match but sample content
        fingerprints changed: snapshots are invalidated while the content
        cache stays authoritative for reuse.  Raise ConfigMismatch when the
        global version, config hash or injection hash does not match.
        """
        manifest = self.load_manifest()
        if manifest is None:
            return "fresh"
        if (manifest.get("global_version") != GLOBAL_VERSION
                or manifest.get("config_hash") != config_hash
                or manifest.get("injection_hash") != injection_hash):
            raise ConfigMismatch()
        if manifest.get("samples_hash") != samples_hash:
            return "stale"
        return "resume"

    def reset(self):
        if os.path.isdir(self.snapshot_dir):
            for name in os.listdir(self.snapshot_dir):
                os.unlink(os.path.join(self.snapshot_dir, name))

    def stage_completed(self, stage):
        manifest = self.load_manifest()
        return bool(manifest) and stage in manifest.get("completed_stages", [])

    def _stage_path(self, stage):
        return os.path.join(self.snapshot_dir, "stage-%s.json" % stage)

    def load_stage(self, stage):
        return load_json(self._stage_path(stage))["payload"]

    def complete_stage(self, stage, payload, config_hash, injection_hash,
                       samples_hash, schemes_done, samples_done):
        atomic_write_json(self._stage_path(stage), {
            "stage": stage,
            "global_version": GLOBAL_VERSION,
            "payload": payload,
        })
        manifest = self.load_manifest() or {}
        completed = list(manifest.get("completed_stages", []))
        if stage not in completed:
            completed.append(stage)
        atomic_write_json(self.manifest_path, {
            "global_version": GLOBAL_VERSION,
            "config_hash": config_hash,
            "injection_hash": injection_hash,
            "samples_hash": samples_hash,
            "completed_stages": completed,
            "completed_schemes": sorted(set(
                manifest.get("completed_schemes", [])) | set(schemes_done)),
            "completed_samples": sorted(set(
                manifest.get("completed_samples", [])) | set(samples_done)),
        })
