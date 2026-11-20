"""Four-stage snapshot store with checksums, versions and a non-blocking lock.

On-disk layout (all writes are atomic via temp files + ``os.replace``)::

    snapshot_dir/
      _store.lock                 # O_CREAT|O_EXCL advisory lock
      framing/v00001.json
      framing/v00001.npz
      framing/latest.json
      pooling/...
      training/v00001.json ...    # one progress snapshot per committed epoch
      training/complete.json
      summary/...

Every snapshot is an envelope carrying a monotonically increasing version,
the configuration checksum it was built from, the schema version and a SHA-256
content checksum covering both the JSON payload and the ``.npz`` bytes.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os

import numpy as np

from ..errors import (E_SNAPSHOT_CORRUPT, E_STAGE_BUSY, EngineError)

STAGES = ("framing", "pooling", "training", "summary")


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_bytes(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8")


class SnapshotStore:
    def __init__(self, root_dir: str, config_checksum: str,
                 schema_version: int = 1):
        self.root_dir = root_dir
        self.config_checksum = config_checksum
        self.schema_version = schema_version
        os.makedirs(root_dir, exist_ok=True)
        self.lock_path = os.path.join(root_dir, "_store.lock")

    # ------------------------------------------------------------------ #
    # Non-blocking commit lock (requirement 7)
    # ------------------------------------------------------------------ #
    @contextlib.contextmanager
    def commit_lock(self):
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise EngineError(E_STAGE_BUSY,
                              "another stage commit is in progress",
                              item=self.lock_path)
        try:
            os.write(fd, str(os.getpid()).encode("utf-8"))
            yield
        finally:
            os.close(fd)
            with contextlib.suppress(FileNotFoundError):
                os.remove(self.lock_path)

    def _stage_dir(self, stage: str) -> str:
        if stage not in STAGES:
            raise EngineError(E_SNAPSHOT_CORRUPT,
                              f"unknown stage {stage}", item=stage)
        path = os.path.join(self.root_dir, stage)
        os.makedirs(path, exist_ok=True)
        return path

    # ------------------------------------------------------------------ #
    # Version management
    # ------------------------------------------------------------------ #
    def latest_version(self, stage: str) -> int:
        stage_dir = self._stage_dir(stage)
        versions = [int(name[1:6]) for name in os.listdir(stage_dir)
                    if name.startswith("v") and name.endswith(".json")
                    and name[1:6].isdigit()]
        return max(versions) if versions else 0

    def reset_stage(self, stage: str) -> None:
        """Whole-directory replacement on schema/config version changes."""
        stage_dir = self._stage_dir(stage)
        for name in os.listdir(stage_dir):
            os.remove(os.path.join(stage_dir, name))

    def reset_all(self) -> None:
        for stage in STAGES:
            if os.path.isdir(os.path.join(self.root_dir, stage)):
                self.reset_stage(stage)

    # ------------------------------------------------------------------ #
    # Atomic, checksummed writes
    # ------------------------------------------------------------------ #
    def save(self, stage: str, payload: dict,
             arrays: dict | None = None) -> dict:
        stage_dir = self._stage_dir(stage)
        version = self.latest_version(stage) + 1
        base = f"v{version:05d}"
        json_tmp = os.path.join(stage_dir, f".{base}.json.tmp")
        npz_tmp_base = os.path.join(stage_dir, f".{base}")
        npz_path = os.path.join(stage_dir, f"{base}.npz")
        json_path = os.path.join(stage_dir, f"{base}.json")

        npz_hash = ""
        if arrays:
            np.savez(npz_tmp_base, **{key: np.asarray(value)
                                      for key, value in arrays.items()})
            os.replace(f"{npz_tmp_base}.npz", npz_path)
            npz_hash = sha256_file(npz_path)

        envelope = {
            "stage": stage,
            "version": version,
            "schema_version": self.schema_version,
            "config_checksum": self.config_checksum,
            "npz_sha256": npz_hash,
            "payload": payload,
        }
        envelope["payload_sha256"] = hashlib.sha256(
            canonical_bytes(payload)).hexdigest()
        envelope["checksum"] = hashlib.sha256(
            canonical_bytes(envelope) + npz_hash.encode("ascii")).hexdigest()

        with open(json_tmp, "w", encoding="utf-8") as handle:
            json.dump(envelope, handle, sort_keys=True, indent=2)
        os.replace(json_tmp, json_path)

        latest_tmp = os.path.join(stage_dir, ".latest.json.tmp")
        with open(latest_tmp, "w", encoding="utf-8") as handle:
            json.dump({"latest_version": version,
                       "file": f"{base}.json"}, handle, sort_keys=True)
        os.replace(latest_tmp, os.path.join(stage_dir, "latest.json"))
        return envelope

    def mark_complete(self, stage: str, version: int) -> None:
        stage_dir = self._stage_dir(stage)
        marker_tmp = os.path.join(stage_dir, ".complete.json.tmp")
        with open(marker_tmp, "w", encoding="utf-8") as handle:
            json.dump({"complete_version": version}, handle, sort_keys=True)
        os.replace(marker_tmp, os.path.join(stage_dir, "complete.json"))

    def is_complete(self, stage: str) -> bool:
        return os.path.exists(os.path.join(self._stage_dir(stage),
                                           "complete.json"))

    # ------------------------------------------------------------------ #
    # Verified reads
    # ------------------------------------------------------------------ #
    def load_latest(self, stage: str) -> tuple[dict, dict | None]:
        stage_dir = self._stage_dir(stage)
        pointer = os.path.join(stage_dir, "latest.json")
        if not os.path.exists(pointer):
            raise EngineError(E_SNAPSHOT_CORRUPT,
                              f"no snapshot pointer for stage {stage}",
                              item=stage)
        with open(pointer, "r", encoding="utf-8") as handle:
            latest = json.load(handle)
        return self.load_version(stage, latest["latest_version"])

    def load_version(self, stage: str, version: int) -> tuple[dict, dict | None]:
        stage_dir = self._stage_dir(stage)
        json_path = os.path.join(stage_dir, f"v{version:05d}.json")
        if not os.path.exists(json_path):
            raise EngineError(E_SNAPSHOT_CORRUPT,
                              f"snapshot v{version:05d} missing for {stage}",
                              item=stage)
        with open(json_path, "r", encoding="utf-8") as handle:
            envelope = json.load(handle)
        stored_checksum = envelope.pop("checksum")
        npz_hash = envelope.get("npz_sha256", "")
        recomputed = hashlib.sha256(
            canonical_bytes(envelope) + npz_hash.encode("ascii")).hexdigest()
        if recomputed != stored_checksum:
            raise EngineError(E_SNAPSHOT_CORRUPT,
                              f"envelope checksum mismatch in {stage} "
                              f"v{version:05d}", item=stage)
        payload = envelope["payload"]
        if hashlib.sha256(canonical_bytes(payload)).hexdigest() \
                != envelope["payload_sha256"]:
            raise EngineError(E_SNAPSHOT_CORRUPT,
                              f"payload checksum mismatch in {stage} "
                              f"v{version:05d}", item=stage)
        if envelope["schema_version"] != self.schema_version or \
                envelope["config_checksum"] != self.config_checksum:
            # Version change: caller must replace, never patch incrementally.
            raise EngineError(E_SNAPSHOT_CORRUPT,
                              f"snapshot lineage mismatch in {stage} "
                              f"(schema/config version drift); full "
                              f"replacement required", item=stage)
        arrays = None
        if npz_hash:
            npz_path = os.path.join(stage_dir, f"v{version:05d}.npz")
            if not os.path.exists(npz_path) or \
                    sha256_file(npz_path) != npz_hash:
                raise EngineError(E_SNAPSHOT_CORRUPT,
                                  f"npz checksum mismatch in {stage} "
                                  f"v{version:05d}", item=stage)
            with np.load(npz_path) as loaded:
                arrays = {key: loaded[key].copy() for key in loaded.files}
        return envelope, {"payload": payload, "arrays": arrays,
                          "version": envelope["version"]}

    def load_all_versions(self, stage: str) -> list[tuple[dict, dict | None]]:
        return [self.load_version(stage, version)
                for version in range(1, self.latest_version(stage) + 1)]
