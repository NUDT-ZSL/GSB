"""Strict parser for the deterministic point-cloud binary format."""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .errors import BusinessError, validation_error
from .util import sha256_bytes


POINT_MAGIC = b"PCWBPTS1"
POINT_VERSION = 1
POINT_RECORD_FLOATS = 8
POINT_RECORD_SIZE = POINT_RECORD_FLOATS * 4
POINT_HEADER = struct.Struct("<8sIIII")


@dataclass
class PointCloud:
    raw: np.ndarray
    points_world: np.ndarray
    gray: np.ndarray
    normals: np.ndarray
    radius_world: np.ndarray
    source_digest: str
    world_scale: float
    origin_offset: np.ndarray
    source_name: str

    @property
    def point_count(self) -> int:
        return int(self.raw.shape[0])

    def region_digest(self, start: int, count: int) -> str:
        chunk = np.asarray(self.raw[start : start + count], dtype="<f4")
        return sha256_bytes(chunk.tobytes(order="C"))


def _first_bad(mask: np.ndarray, field: str, code: str = "E_POINT_DATA") -> None:
    if mask.any():
        index = int(np.flatnonzero(mask)[0])
        raise validation_error(
            code,
            f"point {index} has invalid {field}",
            point_index=index,
            field=field,
        )


def parse_pointcloud_bytes(
    data: bytes,
    source_name: str,
    world_scale: float,
    origin_offset: np.ndarray,
) -> PointCloud:
    if len(data) < POINT_HEADER.size:
        raise validation_error(
            "E_POINTCLOUD_FORMAT",
            "point-cloud file is shorter than its header",
            path=source_name,
            actual_bytes=len(data),
        )
    magic, version, count, record_size, flags = POINT_HEADER.unpack_from(data, 0)
    if magic != POINT_MAGIC:
        raise validation_error(
            "E_POINTCLOUD_FORMAT",
            "point-cloud magic does not match",
            path=source_name,
            magic=magic.hex(),
        )
    if version != POINT_VERSION:
        raise validation_error(
            "E_POINTCLOUD_FORMAT",
            "unsupported point-cloud version",
            path=source_name,
            version=version,
        )
    if record_size != POINT_RECORD_SIZE:
        raise validation_error(
            "E_POINTCLOUD_FORMAT",
            "unexpected point record size",
            path=source_name,
            record_size=record_size,
            expected=POINT_RECORD_SIZE,
        )
    if flags != 0:
        raise validation_error(
            "E_POINTCLOUD_FORMAT",
            "unsupported point-cloud flags",
            path=source_name,
            flags=flags,
        )
    expected_bytes = POINT_HEADER.size + int(count) * POINT_RECORD_SIZE
    if len(data) != expected_bytes:
        raise validation_error(
            "E_POINTCLOUD_FORMAT",
            "point-cloud file size does not match declared point count",
            path=source_name,
            declared_count=count,
            expected_bytes=expected_bytes,
            actual_bytes=len(data),
        )
    try:
        raw = np.frombuffer(data, dtype="<f4", count=count * POINT_RECORD_FLOATS, offset=POINT_HEADER.size)
        raw = raw.reshape((count, POINT_RECORD_FLOATS)).astype(np.float64)
    except (ValueError, TypeError) as exc:
        raise validation_error(
            "E_POINTCLOUD_FORMAT",
            "cannot decode point records",
            path=source_name,
            os_error=str(exc),
        ) from exc

    if count:
        coords = raw[:, 0:3]
        gray = raw[:, 3]
        normals = raw[:, 4:7]
        radius = raw[:, 7]
        _first_bad(~np.isfinite(coords).all(axis=1), "coordinates")
        _first_bad(~np.isfinite(gray), "grayscale")
        _first_bad((gray < 0.0) | (gray > 1.0), "grayscale range")
        _first_bad(~np.isfinite(normals).all(axis=1), "normal")
        normal_scales = np.max(np.abs(normals), axis=1)
        nonzero_normals = np.isfinite(normal_scales) & (normal_scales > 0.0)
        _first_bad(~nonzero_normals, "normal length")
        safe_scales = np.where(nonzero_normals, normal_scales, 1.0)
        scaled_normals = normals / safe_scales[:, None]
        scaled_lengths = np.linalg.norm(scaled_normals, axis=1)
        _first_bad((~np.isfinite(scaled_lengths)) | (scaled_lengths <= 0.0), "normal length")
        _first_bad(~np.isfinite(radius), "radius")
        _first_bad(radius < 0.0, "radius range")
        normals = scaled_normals / scaled_lengths[:, None]
        points_world = coords * float(world_scale) + np.asarray(origin_offset, dtype=np.float64)[None, :]
        radius_world = radius * float(world_scale)
        _first_bad(~np.isfinite(points_world).all(axis=1), "world coordinates")
        _first_bad(~np.isfinite(radius_world), "world radius")
    else:
        gray = np.empty(0, dtype=np.float64)
        normals = np.empty((0, 3), dtype=np.float64)
        points_world = np.empty((0, 3), dtype=np.float64)
        radius_world = np.empty(0, dtype=np.float64)

    cloud = PointCloud(
        raw=raw,
        points_world=points_world,
        gray=gray,
        normals=normals,
        radius_world=radius_world,
        source_digest=sha256_bytes(data),
        world_scale=float(world_scale),
        origin_offset=np.asarray(origin_offset, dtype=np.float64).copy(),
        source_name=source_name,
    )
    for array in (
        cloud.raw,
        cloud.points_world,
        cloud.gray,
        cloud.normals,
        cloud.radius_world,
        cloud.origin_offset,
    ):
        array.setflags(write=False)
    return cloud


def load_pointcloud(
    path: Path,
    world_scale: float,
    origin_offset: np.ndarray,
) -> PointCloud:
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise BusinessError(
            code="E_POINTCLOUD_IO",
            message="cannot read point-cloud file",
            details={"path": str(path), "os_error": str(exc)},
        ) from exc
    return parse_pointcloud_bytes(data, str(path), world_scale, origin_offset)


def encode_pointcloud(raw: np.ndarray) -> bytes:
    array = np.asarray(raw, dtype="<f4")
    if array.ndim != 2 or array.shape[1] != POINT_RECORD_FLOATS:
        raise ValueError("point records must have shape (N, 8)")
    header = POINT_HEADER.pack(POINT_MAGIC, POINT_VERSION, array.shape[0], POINT_RECORD_SIZE, 0)
    return header + array.tobytes(order="C")


def dataset_digest(cloud: PointCloud) -> str:
    canonical = (
        "PCWB_DATASET_V1|"
        + cloud.source_digest
        + "|"
        + format(cloud.world_scale, ".17g")
        + "|"
        + ",".join(format(float(value), ".17g") for value in cloud.origin_offset)
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()

