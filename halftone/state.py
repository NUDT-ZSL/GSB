from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import shutil
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np

from .atomicio import atomic_write_json
from .config import ValidatedJob, canonical_json_bytes, sha256_hex
from .errors import BusinessError

TILE_FORMAT = "halftone-tile-v1"
MANIFEST_FORMAT = "halftone-state-manifest-v1"


def digest_tile_document(document: Dict[str, Any]) -> str:
    body = {key: value for key, value in document.items() if key != "content_sha256"}
    return sha256_hex(canonical_json_bytes(body))


def encode_tile_pixels(pixels: np.ndarray) -> Tuple[str, str]:
    array = np.ascontiguousarray(pixels, dtype=np.uint8)
    raw = array.tobytes()
    return base64.b64encode(raw).decode("ascii"), hashlib.sha256(raw).hexdigest()


def decode_and_verify_tile(
    document: Dict[str, Any],
    *,
    expected_job: Optional[ValidatedJob] = None,
    expected_version: Optional[str] = None,
) -> np.ndarray:
    def corrupt(reason: str, details: Optional[Dict[str, Any]] = None) -> BusinessError:
        payload = {"reason": reason}
        if details:
            payload.update(details)
        return BusinessError("E_DIGEST", "persisted tile digest verification failed", payload)

    if not isinstance(document, dict):
        raise corrupt("document_not_object")
    claimed_digest = document.get("content_sha256")
    if not isinstance(claimed_digest, str):
        raise corrupt("missing_content_sha256", {"index": document.get("index")})
    expected_digest = digest_tile_document(document)
    if not hmac.compare_digest(claimed_digest, expected_digest):
        raise corrupt(
            "content_digest_mismatch",
            {
                "index": document.get("index"),
                "expected": expected_digest,
                "actual": claimed_digest,
            },
        )

    try:
        raw = base64.b64decode(document.get("data_b64", ""), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise corrupt("invalid_base64", {"index": document.get("index")}) from exc
    pixel_digest = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(pixel_digest, str(document.get("pixel_sha256", ""))):
        raise corrupt("pixel_digest_mismatch", {"index": document.get("index")})

    shape = document.get("shape")
    dtype = document.get("dtype")
    if dtype != "uint8" or not isinstance(shape, list) or len(shape) != 2:
        raise corrupt("unsupported_tile_encoding", {"index": document.get("index")})
    try:
        pixels = np.frombuffer(raw, dtype=np.uint8).reshape(int(shape[0]), int(shape[1])).copy()
    except (ValueError, TypeError) as exc:
        raise corrupt("shape_mismatch", {"index": document.get("index")}) from exc

    if expected_version is not None and document.get("version_id") != expected_version:
        raise corrupt(
            "version_mismatch",
            {
                "index": document.get("index"),
                "expected_version": expected_version,
                "actual_version": document.get("version_id"),
            },
        )
    if expected_job is not None:
        checks = {
            "job_id": expected_job.job_id,
            "full_width": expected_job.output_width,
            "full_height": expected_job.output_height,
            "tile_width": expected_job.tile_width,
            "tile_height": expected_job.tile_height,
        }
        for key, expected in checks.items():
            if document.get(key) != expected:
                raise corrupt(
                    "metadata_mismatch",
                    {"index": document.get("index"), "field": key, "expected": expected,
                     "actual": document.get(key)},
                )
        index = int(document.get("index", -1))
        tile_y, tile_x = divmod(index, expected_job.tile_cols)
        offset_x = tile_x * expected_job.tile_width
        offset_y = tile_y * expected_job.tile_height
        if document.get("tile_x") != tile_x or document.get("tile_y") != tile_y:
            raise corrupt("tile_coordinate_mismatch", {"index": index})
        if document.get("offset_x") != offset_x or document.get("offset_y") != offset_y:
            raise corrupt("tile_offset_mismatch", {"index": index})
        if document.get("width") != expected_job.tile_width:
            raise corrupt("tile_width_mismatch", {"index": index})
        if document.get("height") != expected_job.tile_height:
            raise corrupt("tile_height_mismatch", {"index": index})
    return pixels


class StateStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    def manifest_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "manifest.json"

    def tiles_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "tiles"

    def tile_path(self, job_id: str, index: int) -> Path:
        return self.tiles_dir(job_id) / f"tile_{index:08d}.json"

    def read_manifest(self, job_id: str) -> Optional[Dict[str, Any]]:
        path = self.manifest_path(job_id)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BusinessError(
                "E_STATE", f"state manifest is unreadable for {job_id}"
            ) from exc
        if not isinstance(value, dict):
            raise BusinessError("E_STATE", f"state manifest is invalid for {job_id}")
        return value

    def prepare_version(self, job: ValidatedJob, version_id: str) -> Tuple[set[int], Optional[str]]:
        job_dir = self.job_dir(job.job_id)
        tiles_dir = self.tiles_dir(job.job_id)
        manifest = self.read_manifest(job.job_id)
        archived_from_version: Optional[str] = None

        if manifest is not None and manifest.get("version_id") == version_id:
            tiles_dir.mkdir(parents=True, exist_ok=True)
            reusable = self.verify_all_tiles(job, version_id)
            return reusable, None

        if manifest is not None:
            old_version = str(manifest.get("version_id", "unknown"))
            archived_from_version = self.archive_current(job.job_id, old_version)
        elif tiles_dir.exists() and any(tiles_dir.glob("tile_*.json")):
            archived_from_version = self.archive_current(job.job_id, "orphan")

        tiles_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            self.manifest_path(job.job_id),
            {
                "format": MANIFEST_FORMAT,
                "job_id": job.job_id,
                "version_id": version_id,
                "tile_count": job.tile_cols * job.tile_rows,
                "tile_cols": job.tile_cols,
                "tile_rows": job.tile_rows,
                "tile_width": job.tile_width,
                "tile_height": job.tile_height,
            },
        )
        return set(), archived_from_version

    def archive_current(self, job_id: str, old_version: str) -> str:
        job_dir = self.job_dir(job_id)
        archive_root = job_dir / "archive"
        base = archive_root / old_version
        destination = base
        suffix = 1
        while destination.exists():
            destination = archive_root / f"{old_version}-r{suffix:03d}"
            suffix += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.mkdir()

        tiles_dir = self.tiles_dir(job_id)
        manifest = self.manifest_path(job_id)
        if tiles_dir.exists():
            shutil.move(str(tiles_dir), str(destination / "tiles"))
        if manifest.exists():
            shutil.move(str(manifest), str(destination / "manifest.json"))
        return str(destination.relative_to(self.root))

    def verify_all_tiles(self, job: ValidatedJob, version_id: str) -> set[int]:
        reusable: set[int] = set()
        tiles_dir = self.tiles_dir(job.job_id)
        expected_count = job.tile_cols * job.tile_rows
        if not tiles_dir.exists():
            return reusable
        for path in sorted(tiles_dir.glob("tile_*.json")):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                pixels = decode_and_verify_tile(
                    document, expected_job=job, expected_version=version_id
                )
                index = int(document["index"])
                if pixels.shape != (job.tile_height, job.tile_width):
                    raise BusinessError("E_DIGEST", "tile shape does not match job")
                if not 0 <= index < expected_count:
                    raise BusinessError("E_DIGEST", "tile index outside grid")
                reusable.add(index)
            except BusinessError:
                raise
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise BusinessError(
                    "E_DIGEST",
                    "persisted tile is unreadable or incomplete",
                    {"path": str(path)},
                ) from exc
        return reusable

    def write_tile(
        self,
        job: ValidatedJob,
        version_id: str,
        index: int,
        pixels: np.ndarray,
    ) -> Path:
        tile_y, tile_x = divmod(index, job.tile_cols)
        offset_x = tile_x * job.tile_width
        offset_y = tile_y * job.tile_height
        data_b64, pixel_digest = encode_tile_pixels(pixels)
        document: Dict[str, Any] = {
            "format": TILE_FORMAT,
            "job_id": job.job_id,
            "version_id": version_id,
            "index": index,
            "tile_x": tile_x,
            "tile_y": tile_y,
            "offset_x": offset_x,
            "offset_y": offset_y,
            "width": job.tile_width,
            "height": job.tile_height,
            "tile_width": job.tile_width,
            "tile_height": job.tile_height,
            "full_width": job.output_width,
            "full_height": job.output_height,
            "dtype": "uint8",
            "shape": [int(pixels.shape[0]), int(pixels.shape[1])],
            "encoding": "base64-raw-sha256",
            "pixel_sha256": pixel_digest,
            "data_b64": data_b64,
        }
        document["content_sha256"] = digest_tile_document(document)
        path = self.tile_path(job.job_id, index)
        atomic_write_json(path, document)
        return path

    def read_tile_pixels(self, job: ValidatedJob, version_id: str, index: int) -> np.ndarray:
        path = self.tile_path(job.job_id, index)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BusinessError(
                "E_DIGEST", f"persisted tile cannot be read: {path}"
            ) from exc
        return decode_and_verify_tile(
            document, expected_job=job, expected_version=version_id
        )

    def corrupt_digest_for_injection(self, job_id: str, index: int = 0) -> Path:
        path = self.tile_path(job_id, index)
        document = json.loads(path.read_text(encoding="utf-8"))
        digest = str(document["content_sha256"])
        replacement = "0" if not digest.startswith("0") else "1"
        document["content_sha256"] = replacement + digest[1:]
        atomic_write_json(path, document)
        return path
