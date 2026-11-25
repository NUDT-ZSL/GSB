"""Content-addressed frame snapshots and deterministic cache keys."""
from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

import numpy as np

from .atomicio import atomic_write_bytes, atomic_write_json
from .errors import ErrorCode, DiagnosticError
from .hashing import array_content_hash, canonical_hash


CACHE_KEY_NAMESPACE = "frame-cache-key/v1"


@dataclass(frozen=True)
class CachedFrame:
    cache_key: str
    content_hash: str
    frame: np.ndarray
    record: dict[str, Any]


def build_frame_cache_key(key_material: Mapping[str, Any]) -> str:
    clip_id = key_material.get("clip_id")
    frame_index = key_material.get("frame_index")
    upstream_hash = key_material.get("upstream_hash")
    required = (
        "clip_id",
        "noise_seed",
        "frame_range",
        "frame_index",
        "block_size",
        "propagation_steps",
        "upstream_hash",
    )
    missing = [name for name in required if key_material.get(name) in (None, "")]
    if missing:
        raise DiagnosticError(
            ErrorCode.CACHE_KEY_INVALID,
            "cache key is missing required dependency metadata",
            clip_id=clip_id if isinstance(clip_id, str) else None,
            field=",".join(missing),
            details={"missing": missing, "frame_index": frame_index},
        )
    frame_range = key_material["frame_range"]
    if (
        not isinstance(frame_range, (list, tuple))
        or len(frame_range) != 2
        or frame_range[0] != frame_index
        or frame_range[1] != frame_index + 1
    ):
        raise DiagnosticError(
            ErrorCode.CACHE_KEY_INVALID,
            "frame_range must be [frame_index, frame_index + 1]",
            clip_id=clip_id if isinstance(clip_id, str) else None,
            field="frame_range",
            details={"frame_range": frame_range, "frame_index": frame_index},
        )
    if not isinstance(upstream_hash, str) or len(upstream_hash) < 8:
        raise DiagnosticError(
            ErrorCode.CACHE_KEY_INVALID,
            "upstream frame content hash is missing or invalid",
            clip_id=clip_id if isinstance(clip_id, str) else None,
            field="upstream_hash",
            details={"frame_index": frame_index},
        )
    return canonical_hash(dict(key_material), namespace=CACHE_KEY_NAMESPACE)


class FrameCache:
    def __init__(self, state_dir: str | Path):
        self.root = Path(state_dir) / "cache"
        self.key_dir = self.root / "keys"
        self.frame_dir = self.root / "frames"
        self.key_dir.mkdir(parents=True, exist_ok=True)
        self.frame_dir.mkdir(parents=True, exist_ok=True)

    def key_path(self, cache_key: str) -> Path:
        return self.key_dir / f"{cache_key}.json"

    def content_path(self, content_hash: str) -> Path:
        return self.frame_dir / f"{content_hash}.npy"

    def put(self, key_material: Mapping[str, Any], frame: np.ndarray) -> dict[str, Any]:
        cache_key = build_frame_cache_key(key_material)
        array = np.ascontiguousarray(frame)
        content_hash = array_content_hash(array)
        buffer = io.BytesIO()
        np.save(buffer, array, allow_pickle=False)
        atomic_write_bytes(self.content_path(content_hash), buffer.getvalue())
        record = {
            "schema": "frame-cache-record/v1",
            "cache_key": cache_key,
            "key_material": dict(key_material),
            "content_sha256": content_hash,
            "frame_shape": list(array.shape),
            "frame_dtype": array.dtype.str,
            "artifact": f"cache/frames/{content_hash}.npy",
        }
        atomic_write_json(self.key_path(cache_key), record)
        return record

    def load_verified(self, cache_key: str) -> CachedFrame | None:
        path = self.key_path(cache_key)
        if not path.exists():
            return None
        record = self._read_record_file(path)
        self.verify_record(record)
        content_hash = record["content_sha256"]
        array = np.load(self.content_path(content_hash), allow_pickle=False)
        return CachedFrame(
            cache_key=cache_key,
            content_hash=content_hash,
            frame=array,
            record=record,
        )

    def verify_record(self, record: Mapping[str, Any]) -> None:
        material = record.get("key_material")
        cache_key = record.get("cache_key")
        frame_index = None
        clip_id = None
        if isinstance(material, Mapping):
            frame_index = material.get("frame_index")
            clip_id = material.get("clip_id")
        if not isinstance(material, Mapping) or not isinstance(cache_key, str):
            raise DiagnosticError(
                ErrorCode.STATE_INCONSISTENT,
                "cache record lacks key material or cache_key",
                clip_id=clip_id,
                field="cache_record",
                details={"first_frame": frame_index},
            )
        try:
            expected_key = build_frame_cache_key(material)
        except DiagnosticError as error:
            raise DiagnosticError(
                ErrorCode.STATE_INCONSISTENT,
                "cache key material is invalid",
                clip_id=clip_id,
                field=error.field,
                details={"first_frame": frame_index, "source_error": error.to_dict()},
            ) from error
        if cache_key != expected_key:
            raise DiagnosticError(
                ErrorCode.STATE_INCONSISTENT,
                "stored cache_key does not match key material",
                clip_id=clip_id,
                field="cache_key",
                details={
                    "first_frame": frame_index,
                    "stored_key": cache_key,
                    "expected_key": expected_key,
                },
            )
        content_hash = record.get("content_sha256")
        content_path = self.content_path(str(content_hash))
        if not content_path.exists():
            raise DiagnosticError(
                ErrorCode.STATE_INCONSISTENT,
                "cached frame artifact is missing",
                clip_id=clip_id,
                field="content_sha256",
                details={"first_frame": frame_index, "content_hash": content_hash},
            )
        array = np.load(content_path, allow_pickle=False)
        actual_hash = array_content_hash(array)
        if actual_hash != content_hash:
            raise DiagnosticError(
                ErrorCode.STATE_INCONSISTENT,
                "cached frame content hash mismatch",
                clip_id=clip_id,
                field="content_sha256",
                details={
                    "first_frame": frame_index,
                    "stored_hash": content_hash,
                    "actual_hash": actual_hash,
                },
            )

    def iter_records(self) -> Iterator[dict[str, Any]]:
        for path in sorted(self.key_dir.glob("*.json")):
            yield self._read_record_file(path)

    def discard_clip_frames_from(
        self, clip_id: str, start_frame: int
    ) -> list[dict[str, Any]]:
        discarded: list[dict[str, Any]] = []
        for path in sorted(self.key_dir.glob("*.json")):
            record = self._read_record_file(path)
            material = record.get("key_material") or {}
            if material.get("clip_id") != clip_id:
                continue
            frame_index = material.get("frame_index")
            if not isinstance(frame_index, int) or frame_index < start_frame:
                continue
            path.unlink(missing_ok=True)
            discarded.append(
                {
                    "frame": frame_index,
                    "cache_key": record.get("cache_key"),
                    "content_hash": record.get("content_sha256"),
                    "propagation_steps": material.get("propagation_steps"),
                }
            )
        return discarded

    def _read_record_file(self, path: Path) -> dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as handle:
                record = json.load(handle)
        except (json.JSONDecodeError, OSError) as error:
            raise DiagnosticError(
                ErrorCode.STATE_INCONSISTENT,
                "cache record is unreadable",
                field="cache_key",
                details={"path": str(path), "error": str(error), "first_frame": None},
            ) from error
        if not isinstance(record, dict):
            raise DiagnosticError(
                ErrorCode.STATE_INCONSISTENT,
                "cache record must be a JSON object",
                field="cache_key",
                details={"path": str(path), "first_frame": None},
            )
        return record
