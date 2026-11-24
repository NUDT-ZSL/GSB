from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import numpy as np

from .hashing import sha256_bytes


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
    tmp.replace(path)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    atomic_write_bytes(path, data)


class BlockStore:
    def __init__(self, state_dir: str | Path, image_id: str) -> None:
        self.image_id = image_id
        self.image_dir = Path(state_dir) / "images" / image_id
        self.blocks_dir = self.image_dir / "blocks"
        self.version_path = self.image_dir / "version.json"

    def load_version(self) -> str | None:
        if not self.version_path.exists():
            return None
        try:
            data = json.loads(self.version_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        version = data.get("version_id")
        return version if isinstance(version, str) else None

    def write_version(self, version_id: str) -> None:
        atomic_write_json(
            self.version_path,
            {"image_id": self.image_id, "version_id": version_id, "schema": "formbench-block-v1"},
        )

    def reset(self) -> None:
        if self.image_dir.exists():
            shutil.rmtree(self.image_dir)
        self.blocks_dir.mkdir(parents=True, exist_ok=True)

    def _json_path(self, index: int) -> Path:
        return self.blocks_dir / f"block-{index:04d}.json"

    def _bitmap_path(self, index: int) -> Path:
        return self.blocks_dir / f"block-{index:04d}.normalized.bin"

    def save_block(
        self,
        *,
        index: int,
        version_id: str,
        bitmap: np.ndarray,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.blocks_dir.mkdir(parents=True, exist_ok=True)
        if bitmap.dtype != np.uint8 or bitmap.ndim != 2 or not bitmap.flags["C_CONTIGUOUS"]:
            bitmap = np.ascontiguousarray(bitmap, dtype=np.uint8)
        bitmap_bytes = bitmap.tobytes()
        normalized_sha = sha256_bytes(bitmap_bytes)
        bitmap_path = self._bitmap_path(index)
        atomic_write_bytes(bitmap_path, bitmap_bytes)

        record = dict(payload)
        record.update(
            {
                "status": "complete",
                "image_id": self.image_id,
                "block_index": index,
                "version_id": version_id,
                "normalized_file": bitmap_path.name,
                "normalized_sha256": normalized_sha,
                "normalized_shape": list(bitmap.shape),
                "normalized_dtype": "uint8",
            }
        )
        atomic_write_json(self._json_path(index), record)
        return record

    def load_block(self, index: int, version_id: str) -> dict[str, Any] | None:
        json_path = self._json_path(index)
        if not json_path.exists():
            return None
        try:
            record = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if record.get("status") != "complete" or record.get("version_id") != version_id:
            return None
        bitmap_path = self._bitmap_path(index)
        if not bitmap_path.exists():
            return None
        data = bitmap_path.read_bytes()
        if sha256_bytes(data) != record.get("normalized_sha256"):
            return None
        shape = record.get("normalized_shape")
        if not isinstance(shape, list) or len(shape) != 2:
            return None
        try:
            bitmap = np.frombuffer(data, dtype=np.uint8).reshape(int(shape[0]), int(shape[1])).copy()
        except (ValueError, TypeError):
            return None
        record["bitmap"] = bitmap
        return record

    def completed_indices(self, version_id: str) -> set[int]:
        result: set[int] = set()
        if not self.blocks_dir.exists():
            return result
        for path in sorted(self.blocks_dir.glob("block-*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            index = record.get("block_index")
            if (
                record.get("status") == "complete"
                and record.get("version_id") == version_id
                and isinstance(index, int)
            ):
                result.add(index)
        return result

    def cleanup_temporary_files(self) -> None:
        if not self.blocks_dir.exists():
            return
        for path in self.blocks_dir.glob("*.tmp"):
            try:
                path.unlink()
            except OSError:
                pass


def write_interrupt_marker(state_dir: str | Path, payload: dict[str, Any]) -> Path:
    path = Path(state_dir) / "INTERRUPTED.json"
    atomic_write_json(path, payload)
    return path


def clear_interrupt_marker(state_dir: str | Path) -> None:
    path = Path(state_dir) / "INTERRUPTED.json"
    if path.exists():
        path.unlink()
