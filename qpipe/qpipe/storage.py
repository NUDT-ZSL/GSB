"""Persistent progress points, cache entries and a non-blocking stage lock.

All writes are atomic (temp file + ``os.replace``) and every persisted record
carries a SHA-256 content checksum, so a torn write or manual tampering is
detected on read as ``E_CACHE_CORRUPT`` instead of being silently trusted.
"""

from __future__ import annotations

import contextlib
import json
import os
from typing import Any, Dict, List, Optional

import numpy as np

from .deterministic import (
    canonical_json_bytes,
    checksum_json,
)
from .errors import (
    E_CACHE_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    PipelineError,
)

PROGRESS_DIR = "progress"
CACHE_DIR = "cache"
LOCK_DIR = "locks"
HEAD_NAME = "HEAD.json"
HISTORY_NAME = "history.jsonl"


def atomic_write_bytes(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def atomic_write_json(path: str, obj: Dict[str, Any]) -> None:
    atomic_write_bytes(path, canonical_json_bytes(obj))


def read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        raise PipelineError(
            E_CACHE_CORRUPT,
            f"cannot read JSON record {path}: {exc}",
        ) from exc


class FileLock:
    """Cross-platform non-blocking exclusive lock via atomic ``O_CREAT``.

    Acquisition never blocks: a second acquirer immediately raises
    :data:`E_STAGE_BUSY`.  Locks store the owner pid purely for diagnostics.
    """

    def __init__(self, work_dir: "WorkDir", name: str) -> None:
        self.path = work_dir.lock_path(name)
        self._held = False

    def acquire(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            owner = "?"
            with contextlib.suppress(OSError):
                with open(self.path, "r", encoding="utf-8") as handle:
                    owner = handle.read().strip()
            raise PipelineError(
                E_STAGE_BUSY,
                f"lock {os.path.basename(self.path)} held by pid {owner}",
            ) from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"pid": os.getpid()}, sort_keys=True))
        self._held = True

    def release(self) -> None:
        if self._held:
            with contextlib.suppress(FileNotFoundError):
                os.remove(self.path)
            self._held = False

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


class WorkDir:
    """Layout and path helpers for the on-disk working directory."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        self.progress = os.path.join(self.root, PROGRESS_DIR)
        self.cache = os.path.join(self.root, CACHE_DIR)
        self.locks = os.path.join(self.root, LOCK_DIR)
        for directory in (self.progress, self.cache, self.locks):
            os.makedirs(directory, exist_ok=True)

    def lock_path(self, name: str) -> str:
        return os.path.join(self.locks, f"{name}.lock")

    def point_path(self, progress_version: int) -> str:
        return os.path.join(
            self.progress, f"point_v{progress_version:06d}.json"
        )

    def head_path(self) -> str:
        return os.path.join(self.progress, HEAD_NAME)

    def history_path(self) -> str:
        return os.path.join(self.progress, HISTORY_NAME)

    def cache_entry_path(self, stage: str, layer_index: int) -> str:
        sub = os.path.join(self.cache, stage)
        return os.path.join(sub, f"layer_{layer_index:04d}.json")

    def cache_array_path(self, stage: str, layer_index: int, tag: str) -> str:
        sub = os.path.join(self.cache, stage)
        return os.path.join(sub, f"layer_{layer_index:04d}_{tag}.npy")


class ProgressStore:
    """Loads and atomically commits monotonic progress points."""

    def __init__(self, work_dir: WorkDir) -> None:
        self.work_dir = work_dir

    def head(self) -> Optional[Dict[str, Any]]:
        path = self.work_dir.head_path()
        if not os.path.exists(path):
            return None
        head = read_json(path)
        return self._load_verified_point(head["progress_version"])

    def _load_verified_point(self, progress_version: int) -> Dict[str, Any]:
        path = self.work_dir.point_path(progress_version)
        point = read_json(path)
        stored = point.pop("checksum", None)
        if stored != checksum_json(point):
            raise PipelineError(
                E_CACHE_CORRUPT,
                f"progress point v{progress_version} checksum mismatch",
                detail={"progress_version": progress_version},
            )
        point["checksum"] = stored
        return point

    def latest(self) -> Optional[Dict[str, Any]]:
        return self.head()

    def commit(self, point: Dict[str, Any]) -> Dict[str, Any]:
        """Atomically persist a point and swing HEAD to it.

        Callers must hold the stage lock.  The point's version must be strictly
        greater than the current HEAD (rollback protection lives in the
        pipeline; this class still guards the monotonic invariant).
        """
        current = self.head()
        if current is not None:
            if point["progress_version"] < current["progress_version"]:
                raise PipelineError(
                    E_SNAPSHOT_ROLLBACK,
                    (
                        f"progress version {point['progress_version']} older "
                        f"than HEAD {current['progress_version']}"
                    ),
                    stage=point["stage"],
                )
        record = dict(point)
        record.pop("checksum", None)
        record["checksum"] = checksum_json(record)
        version = record["progress_version"]
        atomic_write_json(self.work_dir.point_path(version), record)

        history = self.work_dir.history_path()
        with open(history, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(
                    {
                        "progress_version": version,
                        "stage": record["stage"],
                        "checksum": record["checksum"],
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        atomic_write_json(
            self.work_dir.head_path(),
            {"progress_version": version, "checksum": record["checksum"]},
        )
        return record

    def history(self) -> List[Dict[str, Any]]:
        path = self.work_dir.history_path()
        if not os.path.exists(path):
            return []
        records: List[Dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records


class CacheStore:
    """Stage/layer keyed cache with checksummed JSON metadata and ``.npy``."""

    def __init__(self, work_dir: WorkDir) -> None:
        self.work_dir = work_dir

    def write_entry(
        self,
        stage: str,
        layer_index: int,
        payload: Dict[str, Any],
        arrays: Optional[Dict[str, np.ndarray]] = None,
    ) -> None:
        arrays = arrays or {}
        array_refs: Dict[str, str] = {}
        for tag, arr in arrays.items():
            array_path = self.work_dir.cache_array_path(
                stage, layer_index, tag
            )
            os.makedirs(os.path.dirname(array_path), exist_ok=True)
            tmp = array_path + ".tmp"
            with open(tmp, "wb") as handle:
                np.save(handle, np.ascontiguousarray(arr), allow_pickle=False)
            os.replace(tmp, array_path)
            array_refs[tag] = os.path.basename(array_path)
        record = {
            "stage": stage,
            "layer_index": layer_index,
            "payload": payload,
            "arrays": array_refs,
        }
        record["checksum"] = checksum_json(
            {k: v for k, v in record.items() if k != "checksum"}
        )
        atomic_write_json(
            self.work_dir.cache_entry_path(stage, layer_index), record
        )

    def read_entry(
        self, stage: str, layer_index: int
    ) -> Optional[Dict[str, Any]]:
        path = self.work_dir.cache_entry_path(stage, layer_index)
        if not os.path.exists(path):
            return None
        record = read_json(path)
        stored = record.get("checksum")
        recomputed = checksum_json(
            {
                "stage": record.get("stage"),
                "layer_index": record.get("layer_index"),
                "payload": record.get("payload"),
                "arrays": record.get("arrays"),
            }
        )
        if stored != recomputed:
            raise PipelineError(
                E_CACHE_CORRUPT,
                f"cache entry {stage}/layer{layer_index} checksum mismatch",
                stage=stage,
                layer_index=layer_index,
            )
        return record

    def read_array(
        self, stage: str, layer_index: int, tag: str
    ) -> np.ndarray:
        record = self.read_entry(stage, layer_index)
        if record is None or tag not in record["arrays"]:
            raise PipelineError(
                E_CACHE_CORRUPT,
                f"missing array {tag} in {stage}/layer{layer_index}",
                stage=stage,
                layer_index=layer_index,
            )
        array_path = os.path.join(
            self.work_dir.cache, stage, record["arrays"][tag]
        )
        if not os.path.exists(array_path):
            raise PipelineError(
                E_CACHE_CORRUPT,
                f"array file missing for {stage}/layer{layer_index}/{tag}",
                stage=stage,
                layer_index=layer_index,
            )
        return np.ascontiguousarray(np.load(array_path, allow_pickle=False))

    def has_array(
        self, stage: str, layer_index: int, tag: str
    ) -> bool:
        """Return whether a checksummed entry and its array both exist."""
        record = self.read_entry(stage, layer_index)
        if record is None or tag not in record["arrays"]:
            return False
        array_path = os.path.join(
            self.work_dir.cache, stage, record["arrays"][tag]
        )
        return os.path.exists(array_path)

    def remove_layer(self, stage: str, layer_index: int) -> None:
        """Wholesale removal of one layer's cache (used by replacement)."""
        entry = self.work_dir.cache_entry_path(stage, layer_index)
        if os.path.exists(entry):
            os.remove(entry)
        stage_dir = os.path.join(self.work_dir.cache, stage)
        if not os.path.isdir(stage_dir):
            return
        prefix = f"layer_{layer_index:04d}_"
        for name in os.listdir(stage_dir):
            if name.startswith(prefix) and name.endswith(".npy"):
                os.remove(os.path.join(stage_dir, name))

    def count_entries(self) -> int:
        total = 0
        if not os.path.isdir(self.work_dir.cache):
            return 0
        for stage_dir in os.listdir(self.work_dir.cache):
            full = os.path.join(self.work_dir.cache, stage_dir)
            if os.path.isdir(full):
                total += sum(
                    1 for n in os.listdir(full) if n.endswith(".json")
                )
        return total

    def list_layers(self, stage: str) -> List[int]:
        stage_dir = os.path.join(self.work_dir.cache, stage)
        if not os.path.isdir(stage_dir):
            return []
        layers = []
        for name in os.listdir(stage_dir):
            if name.startswith("layer_") and name.endswith(".json"):
                layers.append(int(name[len("layer_") : -len(".json")]))
        return sorted(layers)
