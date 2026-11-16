"""Stage snapshots: temp-file + atomic replace, crash-safe recovery."""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .cache import StageCache
from .determinism import (
    array_fingerprint,
    atomic_write_json,
    content_hash,
    read_json,
    short_hash,
)
from .errors import SnapshotError
from .materialize import MaterialState

SNAPSHOT_NAME = "snapshot.json"
SCHEMA_VERSION = 1


@dataclass
class SnapshotData:
    schema_version: int
    stage_name: str
    config_hash: str
    command_cursor: int
    completed_segments: list[str]
    product_version: int
    material_fingerprint: str
    stage_cache_path: str
    threshold_override: float | None
    cluster_overrides: list[dict[str, Any]]
    material: dict[str, Any]
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "stage_name": self.stage_name,
            "config_hash": self.config_hash,
            "command_cursor": self.command_cursor,
            "completed_segments": list(self.completed_segments),
            "product_version": self.product_version,
            "material_fingerprint": self.material_fingerprint,
            "stage_cache_path": self.stage_cache_path,
            "threshold_override": self.threshold_override,
            "cluster_overrides": list(self.cluster_overrides),
            "material": self.material,
            "extra": self.extra,
            "snapshot_hash": self.hash_payload(),
        }

    def hash_payload(self) -> str:
        payload = {key: value for key, value in self.__dict__.items()
                   if key != "extra"}
        return short_hash(payload)


class SnapshotStore:
    def __init__(self, directory: str) -> None:
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        self.path = os.path.join(directory, SNAPSHOT_NAME)
        self.cache_path = os.path.join(directory, "stage-cache.json")
        self.material_path = os.path.join(directory, "material.npz")
        self.ops_path = os.path.join(directory, "material-ops.json")

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def save(self, data: SnapshotData, cache: StageCache) -> None:
        cache.persist(self.cache_path)
        atomic_write_json(self.path, data.to_dict())

    def load(self) -> SnapshotData:
        if not self.exists():
            raise SnapshotError("snapshot does not exist")
        try:
            payload = read_json(self.path)
        except (OSError, ValueError) as exc:
            raise SnapshotError(f"unreadable snapshot: {exc}") from exc
        required = (
            "schema_version", "stage_name", "config_hash", "command_cursor",
            "completed_segments", "product_version", "material_fingerprint",
            "stage_cache_path", "material", "snapshot_hash",
        )
        if not all(key in payload for key in required):
            raise SnapshotError("snapshot missing required fields")
        expected = payload.get("snapshot_hash")
        actual = SnapshotData(
            schema_version=payload["schema_version"],
            stage_name=payload["stage_name"],
            config_hash=payload["config_hash"],
            command_cursor=payload["command_cursor"],
            completed_segments=list(payload["completed_segments"]),
            product_version=payload["product_version"],
            material_fingerprint=payload["material_fingerprint"],
            stage_cache_path=payload["stage_cache_path"],
            threshold_override=payload.get("threshold_override"),
            cluster_overrides=list(payload.get("cluster_overrides", [])),
            material=payload["material"],
            extra=dict(payload.get("extra", {})),
        ).hash_payload()
        if expected != actual:
            raise SnapshotError("snapshot hash mismatch; ignoring corrupt snapshot")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise SnapshotError("unsupported snapshot schema version")
        return _to_snapshot(payload)

    def load_cache(self) -> StageCache:
        return StageCache.restore(self.cache_path)


def _to_snapshot(payload: dict[str, Any]) -> SnapshotData:
    return SnapshotData(
        schema_version=payload["schema_version"],
        stage_name=payload["stage_name"],
        config_hash=payload["config_hash"],
        command_cursor=payload["command_cursor"],
        completed_segments=list(payload["completed_segments"]),
        product_version=payload["product_version"],
        material_fingerprint=payload["material_fingerprint"],
        stage_cache_path=payload["stage_cache_path"],
        threshold_override=payload.get("threshold_override"),
        cluster_overrides=list(payload.get("cluster_overrides", [])),
        material=payload["material"],
        extra=dict(payload.get("extra", {})),
    )


def material_to_snapshot(state: MaterialState) -> dict[str, Any]:
    return {
        name: {
            "samples_fingerprint": array_fingerprint(samples),
            "count": int(samples.shape[0]),
        }
        for name, samples in sorted(state.files.items())
    }


def persist_material_npz(store: SnapshotStore, state: MaterialState) -> None:
    """Atomically persist materialized audio as a compact npz bundle."""
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-material-", suffix=".part",
                                    dir=store.directory)
    os.close(fd)
    try:
        with open(tmp_path, "wb") as handle:
            np.savez(handle, **{
                name: np.asarray(samples, dtype=np.float64)
                for name, samples in sorted(state.files.items())
            })
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, store.material_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    atomic_write_json(store.ops_path, list(state.ops))


def load_material_npz(store: SnapshotStore,
                      ops: list[dict[str, Any]] | None = None) -> MaterialState:
    with np.load(store.material_path) as bundle:
        files = {name: np.asarray(bundle[name], dtype=np.float64)
                 for name in sorted(bundle.files)}
    if ops is None:
        ops = read_json(store.ops_path) if os.path.exists(store.ops_path) else []
    state = MaterialState(files=files, ops=list(ops), material_fingerprint="")
    state.material_fingerprint = state.fingerprint()
    return state


def snapshot_to_material(payload: dict[str, Any],
                         ops: list[dict[str, Any]]) -> MaterialState:
    raise RuntimeError("use SnapshotStore.load_material_npz for npz recovery")
