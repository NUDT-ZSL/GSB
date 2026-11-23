"""Versioned, checksummed, atomically switched pipeline snapshots."""

from __future__ import annotations

import base64
import json
import threading
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from .errors import BusinessError
from .io_utils import (
    atomic_write_bytes,
    canonical_json_bytes,
    sha256_bytes,
)
from .scoring import validate_scoring

SNAPSHOT_CATEGORIES = (
    "heatmap_observation",
    "keypoint_metadata",
    "scoring",
)

SNAPSHOT_FORMAT = "pose-snapshot-v1"


@dataclass(frozen=True)
class SnapshotRecord:
    category: str
    scene_id: str
    version: int
    checksum: str
    payload: dict[str, Any]

    def to_document(self) -> dict[str, Any]:
        return {
            "format": SNAPSHOT_FORMAT,
            "category": self.category,
            "scene_id": self.scene_id,
            "version": self.version,
            "checksum": self.checksum,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class SnapshotBundle:
    scene_id: str
    heatmap_record: SnapshotRecord
    metadata_record: SnapshotRecord
    scoring_record: SnapshotRecord

    @property
    def records(self) -> dict[str, SnapshotRecord]:
        return {
            "heatmap_observation": self.heatmap_record,
            "keypoint_metadata": self.metadata_record,
            "scoring": self.scoring_record,
        }

    @property
    def versions(self) -> dict[str, int]:
        return {
            category: record.version for category, record in self.records.items()
        }

    @property
    def checksums(self) -> dict[str, str]:
        return {
            category: record.checksum for category, record in self.records.items()
        }

    @property
    def heatmaps(self) -> np.ndarray:
        return decode_array_payload(self.heatmap_record.payload)

    @property
    def metadata(self) -> dict[str, Any]:
        return self.metadata_record.payload

    @property
    def scoring_config(self) -> dict[str, Any]:
        return self.scoring_record.payload

    def identity(self) -> dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "versions": self.versions,
            "checksums": self.checksums,
        }


class SnapshotStore:
    """Atomic per-category snapshots with immutable history for replay."""

    _locks: ClassVar[dict[str, Any]] = {}
    _locks_guard = threading.Lock()

    def __init__(self, state_dir: str | Path, scene_id: str):
        self.state_dir = Path(state_dir)
        self.scene_id = scene_id
        self.scene_dir = self.state_dir / scene_id
        self.history_dir = self.scene_dir / "history"
        self.scene_dir.mkdir(parents=True, exist_ok=True)
        self.history_dir.mkdir(parents=True, exist_ok=True)
        with self._locks_guard:
            lock_key = str(self.scene_dir.resolve())
            self._lock = self._locks.setdefault(lock_key, threading.RLock())

    def path_for(self, category: str) -> Path:
        self._validate_category(category)
        return self.scene_dir / f"{category}.snapshot.json"

    def history_path(self, category: str, version: int) -> Path:
        self._validate_category(category)
        return self.history_dir / f"{category}.{version:08d}.snapshot.json"

    def exists(self) -> bool:
        return all(self.path_for(category).exists() for category in SNAPSHOT_CATEGORIES)

    def initialize(
        self,
        observation_heatmaps: np.ndarray,
        metadata: dict[str, Any],
        scoring: dict[str, Any],
        *,
        expected_channels: int,
        person_count: int,
    ) -> SnapshotBundle:
        with self._lock:
            self._validate_snapshot_payload(
                "heatmap_observation",
                encode_array_payload(observation_heatmaps),
                expected_channels=expected_channels,
            )
            self._validate_snapshot_payload(
                "keypoint_metadata",
                metadata,
                expected_channels=expected_channels,
                person_count=person_count,
            )
            self._validate_snapshot_payload("scoring", scoring)
            payloads = {
                "heatmap_observation": encode_array_payload(observation_heatmaps),
                "keypoint_metadata": dict(metadata),
                "scoring": dict(scoring),
            }
            for category in SNAPSHOT_CATEGORIES:
                if not self.path_for(category).exists():
                    self.commit(category, 1, payloads[category])
            return self.load_bundle()

    def commit(
        self,
        category: str,
        version: int,
        payload: dict[str, Any],
    ) -> SnapshotRecord:
        self._validate_category(category)
        if not isinstance(version, int) or version < 1:
            raise BusinessError(
                "E_SNAPSHOT_ROLLBACK",
                "snapshot version must be a positive integer",
                field="version",
                scene_id=self.scene_id,
                details={"category": category, "attempted_version": version},
            )
        with self._lock:
            current = self.load(category) if self.path_for(category).exists() else None
            if current is not None and version <= current.version:
                raise BusinessError(
                    "E_SNAPSHOT_ROLLBACK",
                    "snapshot versions must increase monotonically",
                    field="version",
                    scene_id=self.scene_id,
                    details={
                        "category": category,
                        "current_version": current.version,
                        "attempted_version": version,
                    },
                )
            self._validate_snapshot_payload(category, payload)
            checksum = self._checksum(category, version, payload)
            record = SnapshotRecord(
                category=category,
                scene_id=self.scene_id,
                version=version,
                checksum=checksum,
                payload=payload,
            )
            document_bytes = canonical_json_bytes(record.to_document())
            history_path = self.history_path(category, version)
            atomic_write_bytes(history_path, document_bytes)
            # The current pointer is the only mutable name. os.replace makes the
            # complete snapshot switch atomic on POSIX and Windows.
            atomic_write_bytes(self.path_for(category), document_bytes)
            loaded = self.load(category)
            if loaded.checksum != checksum or loaded.version != version:
                raise BusinessError(
                    "E_SNAPSHOT_CORRUPT",
                    "snapshot failed read-back verification after atomic replace",
                    field="checksum",
                    scene_id=self.scene_id,
                    details={"category": category, "version": version},
                )
            return loaded

    def load(
        self,
        category: str,
        *,
        expected_version: int | None = None,
        expected_checksum: str | None = None,
    ) -> SnapshotRecord:
        self._validate_category(category)
        path = self.path_for(category)
        if not path.exists():
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "snapshot category is missing",
                field="snapshot",
                scene_id=self.scene_id,
                details={"category": category, "version": None},
            )
        return self._read_snapshot(
            path,
            category,
            expected_version=expected_version,
            expected_checksum=expected_checksum,
        )

    def load_version(self, category: str, version: int) -> SnapshotRecord:
        self._validate_category(category)
        current_path = self.path_for(category)
        if current_path.exists():
            current = self.load(category)
            if current.version == version:
                return current
        history_path = self.history_path(category, version)
        if not history_path.exists():
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "requested historical snapshot version is unavailable",
                field="snapshot",
                scene_id=self.scene_id,
                details={"category": category, "version": version},
            )
        return self._read_snapshot(
            history_path,
            category,
            expected_version=version,
        )

    def load_bundle(self) -> SnapshotBundle:
        with self._lock:
            records = {
                category: self.load(category) for category in SNAPSHOT_CATEGORIES
            }
        return SnapshotBundle(
            scene_id=self.scene_id,
            heatmap_record=records["heatmap_observation"],
            metadata_record=records["keypoint_metadata"],
            scoring_record=records["scoring"],
        )

    def _read_snapshot(
        self,
        path: Path,
        category: str,
        *,
        expected_version: int | None = None,
        expected_checksum: str | None = None,
    ) -> SnapshotRecord:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "snapshot file is unreadable or malformed JSON",
                field="snapshot",
                scene_id=self.scene_id,
                details={"category": category, "version": None, "reason": str(exc)},
            ) from exc
        version = raw.get("version")
        if (
            raw.get("format") != SNAPSHOT_FORMAT
            or raw.get("category") != category
            or raw.get("scene_id") != self.scene_id
            or not isinstance(version, int)
            or version < 1
            or not isinstance(raw.get("payload"), dict)
            or not isinstance(raw.get("checksum"), str)
        ):
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "snapshot document does not match the declared schema",
                field="snapshot",
                scene_id=self.scene_id,
                details={"category": category, "version": version},
            )
        computed = self._checksum(category, version, raw["payload"])
        if computed != raw["checksum"]:
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "snapshot checksum verification failed",
                field="checksum",
                scene_id=self.scene_id,
                details={
                    "category": category,
                    "version": version,
                    "declared_checksum": raw["checksum"],
                    "computed_checksum": computed,
                },
            )
        if expected_version is not None and version != expected_version:
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "snapshot version does not match the recorded dependency",
                field="version",
                scene_id=self.scene_id,
                details={
                    "category": category,
                    "version": version,
                    "expected_version": expected_version,
                },
            )
        if expected_checksum is not None and raw["checksum"] != expected_checksum:
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "snapshot checksum does not match the recorded dependency",
                field="checksum",
                scene_id=self.scene_id,
                details={
                    "category": category,
                    "version": version,
                    "expected_checksum": expected_checksum,
                },
            )
        return SnapshotRecord(
            category=category,
            scene_id=self.scene_id,
            version=version,
            checksum=raw["checksum"],
            payload=raw["payload"],
        )

    def _checksum(
        self, category: str, version: int, payload: dict[str, Any]
    ) -> str:
        return sha256_bytes(
            canonical_json_bytes(
                {
                    "scene_id": self.scene_id,
                    "category": category,
                    "version": version,
                    "payload": payload,
                }
            )
        )

    def _validate_category(self, category: str) -> None:
        if category not in SNAPSHOT_CATEGORIES:
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "unknown snapshot category",
                field="category",
                scene_id=self.scene_id,
                details={"category": category},
            )

    def _validate_snapshot_payload(
        self,
        category: str,
        payload: dict[str, Any],
        *,
        expected_channels: int | None = None,
        person_count: int | None = None,
    ) -> None:
        if category == "heatmap_observation":
            array = decode_array_payload(payload)
            if array.ndim != 3:
                raise BusinessError(
                    "E_CHANNEL_MISMATCH",
                    "heatmap observation must be a 3-D tensor",
                    field="heatmap_observation",
                    scene_id=self.scene_id,
                    details={"shape": list(array.shape)},
                )
            if expected_channels is not None and array.shape[0] != expected_channels:
                raise BusinessError(
                    "E_CHANNEL_MISMATCH",
                    "heatmap channel count does not match num_keypoints",
                    field="num_keypoints",
                    scene_id=self.scene_id,
                    details={
                        "heatmap_channels": int(array.shape[0]),
                        "num_keypoints": expected_channels,
                    },
                )
        elif category == "keypoint_metadata":
            channels = payload.get("channels")
            if not isinstance(channels, list) or not channels:
                raise BusinessError(
                    "E_CONFIG_INVALID",
                    "metadata channels must be a non-empty list",
                    field="channels",
                    scene_id=self.scene_id,
                )
            if expected_channels is not None and len(channels) != expected_channels:
                raise BusinessError(
                    "E_CHANNEL_MISMATCH",
                    "metadata channel count does not match num_keypoints",
                    field="num_keypoints",
                    scene_id=self.scene_id,
                    details={
                        "metadata_channels": len(channels),
                        "num_keypoints": expected_channels,
                    },
                )
            persons = payload.get("persons")
            if not isinstance(persons, list):
                raise BusinessError(
                    "E_CONFIG_INVALID",
                    "metadata persons must be a list",
                    field="persons",
                    scene_id=self.scene_id,
                )
            if person_count is not None and len(persons) != person_count:
                raise BusinessError(
                    "E_PERSON_COUNT_MISMATCH",
                    "person_count does not match keypoint metadata person entries",
                    field="person_count",
                    scene_id=self.scene_id,
                    details={
                        "metadata_persons": len(persons),
                        "person_count": person_count,
                    },
                )
            for field_name in ("edges", "link_distance", "max_instance_diameter"):
                if field_name not in payload:
                    raise BusinessError(
                        "E_CONFIG_INVALID",
                        f"metadata is missing {field_name}",
                        field=field_name,
                        scene_id=self.scene_id,
                    )
        elif category == "scoring":
            validate_scoring(payload, scene_id=self.scene_id)


def encode_array_payload(array: np.ndarray) -> dict[str, Any]:
    contiguous = np.ascontiguousarray(array, dtype=np.float32)
    compressed = zlib.compress(contiguous.tobytes(order="C"), level=9)
    return {
        "format": "npy-zlib-v1",
        "dtype": "float32",
        "shape": [int(value) for value in contiguous.shape],
        "data_base64": base64.b64encode(compressed).decode("ascii"),
    }


def decode_array_payload(payload: dict[str, Any]) -> np.ndarray:
    if (
        payload.get("format") != "npy-zlib-v1"
        or payload.get("dtype") != "float32"
        or not isinstance(payload.get("shape"), list)
        or not isinstance(payload.get("data_base64"), str)
    ):
        raise BusinessError(
            "E_SNAPSHOT_CORRUPT",
            "heatmap snapshot payload is malformed",
            field="heatmap_observation",
        )
    try:
        compressed = base64.b64decode(payload["data_base64"], validate=True)
        raw = zlib.decompress(compressed)
        shape = tuple(int(value) for value in payload["shape"])
        array = np.frombuffer(raw, dtype=np.float32).reshape(shape).copy()
    except (ValueError, TypeError, zlib.error) as exc:
        raise BusinessError(
            "E_SNAPSHOT_CORRUPT",
            "heatmap snapshot payload cannot be decoded",
            field="heatmap_observation",
            details={"reason": str(exc)},
        ) from exc
    return array

