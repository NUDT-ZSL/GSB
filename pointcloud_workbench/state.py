"""Atomic block persistence, digest validation, and recovery metadata."""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .errors import BusinessError, digest_error
from .spec import BlockSpec
from .util import (
    atomic_savez,
    atomic_write_json,
    canonical_json_bytes,
    ensure_writable_directory,
    read_json,
)


BLOCK_SCHEMA = 1
BLOCK_MAGIC = b"PCWB_BLOCK_V1\0"


def _dependency_digest(dependencies: np.ndarray) -> str:
    canonical = np.asarray(dependencies, dtype="<i8")
    return hashlib.sha256(canonical.tobytes(order="C")).hexdigest()


def _content_digest(meta_without_digest: dict[str, Any], gray: np.ndarray, depth: np.ndarray, deps: np.ndarray) -> str:
    header = canonical_json_bytes(meta_without_digest)
    digest = hashlib.sha256()
    digest.update(BLOCK_MAGIC)
    digest.update(struct.pack("<Q", len(header)))
    digest.update(header)
    digest.update(np.asarray(gray, dtype="<u1").tobytes(order="C"))
    digest.update(np.asarray(depth, dtype="<f4").tobytes(order="C"))
    digest.update(np.asarray(deps, dtype="<i8").tobytes(order="C"))
    return digest.hexdigest()


def make_block_meta(
    block: BlockSpec,
    *,
    version_id: str,
    dataset_digest: str,
    camera_digest: str,
    render_digest: str,
    task_id: str,
    point_count: int,
    provenance: str,
    dependencies: np.ndarray,
    gray: np.ndarray,
    depth: np.ndarray,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "schema": BLOCK_SCHEMA,
        "block_index": block.index,
        "block_id": block.block_id,
        "rect": [block.x0, block.y0, block.x1, block.y1],
        "version_id": version_id,
        "dataset_digest": dataset_digest,
        "camera_digest": camera_digest,
        "render_digest": render_digest,
        "task_id": task_id,
        "point_count": int(point_count),
        "provenance": provenance,
        "dependency_count": int(dependencies.size),
        "dependency_digest": _dependency_digest(dependencies),
        "declared_pixel_count": int(gray.size),
    }
    meta["content_digest"] = _content_digest(meta, gray, depth, dependencies)
    return meta


def write_block(
    path: Path,
    block: BlockSpec,
    gray: np.ndarray,
    depth: np.ndarray,
    dependencies: np.ndarray,
    *,
    version_id: str,
    dataset_digest: str,
    camera_digest: str,
    render_digest: str,
    task_id: str,
    point_count: int,
    provenance: str,
) -> dict[str, Any]:
    gray_array = np.asarray(gray, dtype=np.uint8)
    depth_array = np.asarray(depth, dtype="<f4")
    deps_array = np.asarray(dependencies, dtype="<i8")
    if gray_array.shape != (block.height, block.width):
        raise BusinessError(
            "E_RENDER",
            "rendered gray block has an unexpected shape",
            details={
                "block_index": block.index,
                "shape": list(gray_array.shape),
                "expected": [block.height, block.width],
            },
        )
    if depth_array.shape != gray_array.shape:
        raise BusinessError(
            "E_RENDER",
            "rendered depth block shape does not match grayscale block",
            details={"block_index": block.index},
        )
    if deps_array.ndim != 1 or np.any(deps_array < 0):
        raise BusinessError(
            "E_RENDER",
            "dependency indices must be a non-negative one-dimensional array",
            details={"block_index": block.index},
        )
    if deps_array.size > 1 and np.any(deps_array[1:] <= deps_array[:-1]):
        raise BusinessError(
            "E_RENDER",
            "dependency indices must be strictly increasing",
            details={"block_index": block.index},
        )
    meta = make_block_meta(
        block,
        version_id=version_id,
        dataset_digest=dataset_digest,
        camera_digest=camera_digest,
        render_digest=render_digest,
        task_id=task_id,
        point_count=point_count,
        provenance=provenance,
        dependencies=deps_array,
        gray=gray_array,
        depth=depth_array,
    )
    meta_bytes = canonical_json_bytes(meta)
    try:
        atomic_savez(
            path,
            {
                "gray": gray_array,
                "depth": depth_array,
                "dependencies": deps_array,
                "meta": np.frombuffer(meta_bytes, dtype=np.uint8),
            },
        )
    except BusinessError:
        raise
    except Exception as exc:
        raise BusinessError(
            "E_STATE",
            "failed to persist block atomically",
            details={"path": str(path), "os_error": str(exc)},
        ) from exc
    return meta


@dataclass
class BlockInspection:
    path: Path
    exists: bool
    loaded: bool
    content_ok: bool
    meta: dict[str, Any] | None = None
    gray: np.ndarray | None = None
    depth: np.ndarray | None = None
    dependencies: np.ndarray | None = None
    reasons: list[dict[str, Any]] = field(default_factory=list)

    def reusable_for(self, target_version: str) -> bool:
        return (
            self.loaded
            and self.content_ok
            and self.meta is not None
            and self.meta.get("version_id") == target_version
        )

    def reason_codes(self) -> list[str]:
        return [str(reason.get("code")) for reason in self.reasons]


def _inspect_failure(
    path: Path,
    *,
    exists: bool,
    loaded: bool,
    code: str,
    message: str,
    **details: Any,
) -> BlockInspection:
    return BlockInspection(
        path=path,
        exists=exists,
        loaded=loaded,
        content_ok=False,
        reasons=[{"code": code, "message": message, "details": details}],
    )


def inspect_block(
    path: Path,
    block: BlockSpec,
    *,
    expected_version: str | None = None,
) -> BlockInspection:
    path = Path(path)
    if not path.is_file():
        return _inspect_failure(
            path,
            exists=False,
            loaded=False,
            code="MISSING",
            message="completed block file is missing",
        )
    try:
        with np.load(path, allow_pickle=False) as archive:
            required = {"gray", "depth", "dependencies", "meta"}
            if not required.issubset(set(archive.files)):
                return _inspect_failure(
                    path,
                    exists=True,
                    loaded=False,
                    code="CORRUPT",
                    message="block archive is missing required arrays",
                    arrays=sorted(archive.files),
                )
            gray_raw = archive["gray"]
            depth_raw = archive["depth"]
            deps_raw = archive["dependencies"]
            meta_raw = archive["meta"]
            if gray_raw.dtype != np.uint8 or depth_raw.dtype != np.float32 or deps_raw.dtype != np.int64:
                return _inspect_failure(
                    path,
                    exists=True,
                    loaded=False,
                    code="CORRUPT",
                    message="block arrays have unexpected dtypes",
                    dtypes={
                        "gray": str(gray_raw.dtype),
                        "depth": str(depth_raw.dtype),
                        "dependencies": str(deps_raw.dtype),
                    },
                )
            gray = np.array(gray_raw, copy=True, order="C")
            depth = np.array(depth_raw, dtype="<f4", copy=True, order="C")
            dependencies = np.array(deps_raw, dtype="<i8", copy=True, order="C")
            meta_bytes = np.asarray(meta_raw, dtype=np.uint8).tobytes()
            meta = json.loads(meta_bytes.decode("utf-8"))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return _inspect_failure(
            path,
            exists=True,
            loaded=False,
            code="CORRUPT",
            message="block archive cannot be decoded",
            os_error=str(exc),
        )

    reasons: list[dict[str, Any]] = []
    if not isinstance(meta, dict):
        return _inspect_failure(
            path,
            exists=True,
            loaded=True,
            code="CORRUPT",
            message="block metadata is not an object",
        )
    expected_shape = (block.height, block.width)
    if gray.shape != expected_shape or depth.shape != expected_shape:
        reasons.append(
            {
                "code": "PIXEL_COUNT_MISMATCH",
                "message": "block pixel shape does not match the block grid",
                "details": {
                    "gray_shape": list(gray.shape),
                    "depth_shape": list(depth.shape),
                    "expected_shape": list(expected_shape),
                },
            }
        )
    if int(meta.get("declared_pixel_count", -1)) != int(gray.size) or int(gray.size) != int(depth.size):
        reasons.append(
            {
                "code": "PIXEL_COUNT_MISMATCH",
                "message": "declared pixel count does not match stored arrays",
                "details": {
                    "declared": meta.get("declared_pixel_count"),
                    "gray_pixels": int(gray.size),
                    "depth_pixels": int(depth.size),
                },
            }
        )
    if meta.get("block_index") != block.index or meta.get("rect") != [
        block.x0,
        block.y0,
        block.x1,
        block.y1,
    ]:
        reasons.append(
            {
                "code": "CORRUPT",
                "message": "block metadata geometry does not match expected block",
            }
        )
    if dependencies.ndim != 1 or np.any(dependencies < 0):
        reasons.append(
            {
                "code": "CORRUPT",
                "message": "dependency indices are malformed",
            }
        )
    elif dependencies.size > 1 and np.any(dependencies[1:] <= dependencies[:-1]):
        reasons.append(
            {
                "code": "CORRUPT",
                "message": "dependency indices are not strictly increasing",
            }
        )
    point_count = int(meta.get("point_count", -1))
    if point_count < 0 or (dependencies.size and int(dependencies[-1]) >= point_count):
        reasons.append(
            {
                "code": "CORRUPT",
                "message": "dependency index falls outside the declared point count",
                "details": {"point_count": point_count},
            }
        )
    if _dependency_digest(dependencies) != meta.get("dependency_digest"):
        reasons.append(
            {
                "code": "DIGEST_MISMATCH",
                "message": "dependency digest does not match dependency array",
            }
        )
    if np.isnan(depth).any():
        reasons.append({"code": "CORRUPT", "message": "depth block contains NaN"})
    declared_content_digest = meta.get("content_digest")
    meta_without_digest = dict(meta)
    meta_without_digest.pop("content_digest", None)
    actual_content_digest = _content_digest(meta_without_digest, gray, depth, dependencies)
    if declared_content_digest != actual_content_digest:
        reasons.append(
            {
                "code": "DIGEST_MISMATCH",
                "message": "block content digest does not match stored content",
                "details": {
                    "declared": declared_content_digest,
                    "actual": actual_content_digest,
                },
            }
        )
    if expected_version is not None and meta.get("version_id") != expected_version:
        reasons.append(
            {
                "code": "VERSION_MISMATCH",
                "message": "block version id does not match current dataset/camera/render version",
                "details": {
                    "expected": expected_version,
                    "actual": meta.get("version_id"),
                },
            }
        )
    content_ok = not any(
        reason["code"] in {"CORRUPT", "DIGEST_MISMATCH", "PIXEL_COUNT_MISMATCH"} for reason in reasons
    )
    return BlockInspection(
        path=path,
        exists=True,
        loaded=True,
        content_ok=content_ok,
        meta=meta,
        gray=gray,
        depth=depth,
        dependencies=dependencies,
        reasons=reasons,
    )


class StateStore:
    def __init__(self, root: Path, cache_key: str) -> None:
        self.root = Path(root).resolve()
        self.cache_key = cache_key
        self.cache_dir = self.root / cache_key
        self.blocks_root = self.cache_dir / "blocks"
        self.interrupt_path = self.cache_dir / "INTERRUPTED.json"
        try:
            ensure_writable_directory(self.cache_dir, "E_STATE")
            ensure_writable_directory(self.blocks_root, "E_STATE")
        except BusinessError:
            raise
        except OSError as exc:
            raise BusinessError(
                "E_STATE",
                "state directory is not writable",
                details={"path": str(self.cache_dir), "os_error": str(exc)},
            ) from exc

    def camera_dir(self, camera_id: str) -> Path:
        path = self.cache_dir / camera_id
        ensure_writable_directory(path, "E_STATE")
        return path

    def block_path(self, camera_id: str, block_index: int) -> Path:
        return self.camera_dir(camera_id) / "blocks" / f"b{block_index:04d}.npz"

    def committed_path(self, camera_id: str) -> Path:
        return self.camera_dir(camera_id) / "committed.json"

    def read_committed(self, camera_id: str) -> dict[str, Any] | None:
        path = self.committed_path(camera_id)
        if not path.is_file():
            return None
        try:
            value = read_json(path, "E_STATE")
        except BusinessError:
            return None
        return value if isinstance(value, dict) else None

    def commit(self, camera_id: str, payload: dict[str, Any]) -> None:
        try:
            atomic_write_json(self.committed_path(camera_id), payload)
        except OSError as exc:
            raise BusinessError(
                "E_STATE",
                "cannot commit camera state",
                details={"path": str(self.committed_path(camera_id)), "os_error": str(exc)},
            ) from exc

    def write_interrupt_marker(self, payload: dict[str, Any]) -> None:
        try:
            atomic_write_json(self.interrupt_path, payload)
        except OSError as exc:
            raise BusinessError(
                "E_STATE",
                "cannot write interruption marker",
                details={"path": str(self.interrupt_path), "os_error": str(exc)},
            ) from exc

    def clear_interrupt_marker(self) -> None:
        try:
            self.interrupt_path.unlink(missing_ok=True)
        except OSError as exc:
            raise BusinessError(
                "E_STATE",
                "cannot clear interruption marker",
                details={"path": str(self.interrupt_path), "os_error": str(exc)},
            ) from exc

    def promote(
        self,
        camera_id: str,
        block: BlockSpec,
        inspection: BlockInspection,
        *,
        version_id: str,
        dataset_digest: str,
        camera_digest: str,
        render_digest: str,
        task_id: str,
        point_count: int,
    ) -> dict[str, Any]:
        if inspection.gray is None or inspection.depth is None or inspection.dependencies is None:
            raise digest_error("cannot promote an unloaded block", block_index=block.index)
        return write_block(
            self.block_path(camera_id, block.index),
            block,
            inspection.gray,
            inspection.depth,
            inspection.dependencies,
            version_id=version_id,
            dataset_digest=dataset_digest,
            camera_digest=camera_digest,
            render_digest=render_digest,
            task_id=task_id,
            point_count=point_count,
            provenance="promoted",
        )

