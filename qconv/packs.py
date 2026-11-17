"""Strict little-endian quantized tensor pack reader and writer."""

from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .errors import PackInvalidError, PackShapeMismatchError
from .graph import Graph, QuantNode
from .util import array_digest, atomic_write_bytes, finite_positive, safe_relative_path, sha256_file


MAGIC = b"QPK1"
FORMAT_VERSION = 1
ENDIAN_MARKER = 0x1234
HEADER = struct.Struct("<4sHHHHQ")
DTYPE_TO_CODE = {"int8": 1, "int16": 2, "int32": 3}
CODE_TO_DTYPE = {value: key for key, value in DTYPE_TO_CODE.items()}
DTYPE_SIZES = {"int8": 1, "int16": 2, "int32": 4}
DTYPE_TO_NUMPY = {"int8": "<i1", "int16": "<i2", "int32": "<i4"}


@dataclass(frozen=True)
class PackDescriptor:
    key: str
    path: Path
    shape: tuple[int, ...]
    dtype: str
    bit_width: int | None = None
    scale: float | None = None
    seed: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "path": str(self.path),
            "shape": list(self.shape),
            "dtype": self.dtype,
        }
        if self.bit_width is not None:
            result["bit_width"] = self.bit_width
        if self.scale is not None:
            result["scale"] = self.scale
        if self.seed is not None:
            result["seed"] = self.seed
        return result


@dataclass
class PackTensor:
    descriptor: PackDescriptor
    values: np.ndarray
    file_digest: str
    content_digest: str
    data_offset: int
    item_size: int


@dataclass
class PackRepository:
    manifest_path: Path
    input_tensor: PackTensor
    weights: dict[str, PackTensor]

    def weight_for(self, node: QuantNode) -> PackTensor:
        try:
            return self.weights[node.weight_pack]
        except KeyError as exc:
            raise PackInvalidError(
                f"weight pack is not present in manifest: {node.weight_pack}",
                node_id=node.id,
            ) from exc


def pack_header(shape: tuple[int, ...], dtype: str) -> bytes:
    if dtype not in DTYPE_TO_CODE:
        raise PackInvalidError(f"unsupported pack dtype: {dtype}")
    count = math.prod(shape)
    return HEADER.pack(
        MAGIC,
        FORMAT_VERSION,
        ENDIAN_MARKER,
        DTYPE_TO_CODE[dtype],
        len(shape),
        count,
    ) + struct.pack("<" + "I" * len(shape), *shape)


def read_pack(path: str | Path, *, key: str | None = None) -> tuple[PackTensor, tuple[int, ...], str, int, int]:
    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise PackInvalidError(f"cannot read quantized pack {source}: {exc}") from exc
    if len(raw) < HEADER.size:
        raise PackShapeMismatchError(f"pack header is truncated: {source}")
    magic, version, endian_marker, dtype_code, ndim, element_count = HEADER.unpack_from(raw, 0)
    if magic != MAGIC:
        raise PackInvalidError(f"invalid QPAK magic in {source}: {magic!r}")
    if version != FORMAT_VERSION:
        raise PackInvalidError(f"unsupported QPAK version {version} in {source}")
    if endian_marker != ENDIAN_MARKER:
        raise PackShapeMismatchError(
            f"pack {source} is not marked as little-endian (marker={endian_marker:#06x})"
        )
    if dtype_code not in CODE_TO_DTYPE:
        raise PackInvalidError(f"unsupported QPAK dtype code {dtype_code} in {source}")
    dtype = CODE_TO_DTYPE[dtype_code]
    if ndim <= 0 or ndim > 8:
        raise PackShapeMismatchError(f"invalid QPAK ndim {ndim} in {source}")
    shape_offset = HEADER.size
    shape_end = shape_offset + 4 * ndim
    if len(raw) < shape_end:
        raise PackShapeMismatchError(f"pack shape header is truncated: {source}")
    shape = tuple(struct.unpack_from("<" + "I" * ndim, raw, shape_offset))
    expected_count = math.prod(shape)
    if expected_count != element_count:
        raise PackShapeMismatchError(
            f"pack {source} header count {element_count} does not equal shape product {expected_count}"
        )
    item_size = DTYPE_SIZES[dtype]
    expected_length = shape_end + element_count * item_size
    if len(raw) != expected_length:
        raise PackShapeMismatchError(
            f"pack {source} byte length {len(raw)} does not equal expected {expected_length} "
            f"({element_count} x {dtype}); truncation or padding is not accepted"
        )
    np_dtype = np.dtype(DTYPE_TO_NUMPY[dtype])
    values = np.frombuffer(raw, dtype=np_dtype, count=element_count, offset=shape_end).astype(np.int64)
    values = values.reshape(shape)
    descriptor = PackDescriptor(
        key=key or source.stem,
        path=source.resolve(),
        shape=shape,
        dtype=dtype,
    )
    pack = PackTensor(
        descriptor=descriptor,
        values=values,
        file_digest=sha256_file(source),
        content_digest=array_digest(values),
        data_offset=shape_end,
        item_size=item_size,
    )
    return pack, shape, dtype, element_count, item_size


def write_pack(path: str | Path, values: np.ndarray, dtype: str | None = None) -> None:
    array = np.asarray(values)
    if dtype is None:
        dtype = str(array.dtype)
    if dtype not in DTYPE_TO_CODE:
        raise PackInvalidError(f"unsupported output pack dtype: {dtype}")
    np_dtype = np.dtype(DTYPE_TO_NUMPY[dtype])
    canonical = np.ascontiguousarray(array.astype(np_dtype, copy=False))
    payload = pack_header(tuple(canonical.shape), dtype) + canonical.tobytes(order="C")
    atomic_write_bytes(path, payload)


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackInvalidError(f"cannot load pack manifest {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PackInvalidError("pack manifest root must be an object")
    if not isinstance(data.get("input"), dict):
        raise PackInvalidError("pack manifest requires an input descriptor")
    if not isinstance(data.get("weights"), dict):
        raise PackInvalidError("pack manifest requires a weights descriptor map")
    return data


def _descriptor(
    key: str,
    raw: dict[str, Any],
    base_dir: Path,
) -> PackDescriptor:
    if not isinstance(raw, dict):
        raise PackInvalidError(f"pack descriptor {key} must be an object")
    path_value = raw.get("path")
    if not isinstance(path_value, str) or not path_value:
        raise PackInvalidError(f"pack descriptor {key} requires path")
    shape_raw = raw.get("shape")
    if not isinstance(shape_raw, list) or not shape_raw:
        raise PackShapeMismatchError(f"pack descriptor {key} requires a non-empty shape")
    try:
        shape = tuple(int(value) for value in shape_raw)
    except (TypeError, ValueError) as exc:
        raise PackShapeMismatchError(f"pack descriptor {key} has a non-integer shape") from exc
    if any(value <= 0 for value in shape):
        raise PackShapeMismatchError(f"pack descriptor {key} shape values must be positive")
    dtype = raw.get("dtype", "int8")
    if dtype not in DTYPE_TO_CODE:
        raise PackInvalidError(f"pack descriptor {key} has unsupported dtype {dtype!r}")
    bit_width = raw.get("bit_width")
    if bit_width is not None:
        bit_width = int(bit_width)
        if bit_width < 2 or bit_width > 16:
            raise PackInvalidError(f"pack descriptor {key} bit_width must be in [2, 16]")
    scale = raw.get("scale")
    if scale is not None:
        scale = float(scale)
        if not finite_positive(scale):
            raise PackInvalidError(f"pack descriptor {key} scale must be finite and positive")
    seed = raw.get("seed")
    if seed is not None:
        seed = int(seed)
    return PackDescriptor(
        key=key,
        path=safe_relative_path(base_dir, path_value),
        shape=shape,
        dtype=dtype,
        bit_width=bit_width,
        scale=scale,
        seed=seed,
    )


def _bind_descriptor(
    key: str,
    descriptor: PackDescriptor,
    expected_shape: tuple[int, ...],
    expected_dtype: str | None = None,
) -> PackTensor:
    pack, shape, dtype, _, _ = read_pack(descriptor.path, key=key)
    if shape != tuple(descriptor.shape):
        raise PackShapeMismatchError(
            f"pack {key} header shape {list(shape)} does not match descriptor {list(descriptor.shape)}"
        )
    if tuple(shape) != tuple(expected_shape):
        raise PackShapeMismatchError(
            f"pack {key} shape {list(shape)} does not match expected graph shape {list(expected_shape)}"
        )
    if expected_dtype is not None and dtype != expected_dtype:
        raise PackShapeMismatchError(
            f"pack {key} dtype {dtype} does not match expected {expected_dtype}"
        )
    if descriptor.bit_width is not None:
        lower = -(1 << (descriptor.bit_width - 1))
        upper = (1 << (descriptor.bit_width - 1)) - 1
        if int(pack.values.min()) < lower or int(pack.values.max()) > upper:
            raise PackShapeMismatchError(
                f"pack {key} contains values outside declared {descriptor.bit_width}-bit signed range"
            )
    pack.descriptor = descriptor
    return pack


def load_pack_repository(manifest_path: str | Path, graph: Graph) -> PackRepository:
    path = Path(manifest_path)
    data = _load_manifest(path)
    base_dir = path.parent
    input_raw = data["input"]
    input_descriptor = _descriptor("input", input_raw, base_dir)
    input_pack = _bind_descriptor(
        "input",
        input_descriptor,
        graph.input.shape,
        graph.input.dtype if input_raw.get("dtype") is not None else None,
    )
    weights: dict[str, PackTensor] = {}
    for node in graph.nodes.values():
        if node.weight_pack in weights:
            continue
        raw = data["weights"].get(node.weight_pack)
        if raw is None:
            raise PackInvalidError(
                f"weight pack {node.weight_pack!r} referenced by node {node.id} is absent from manifest",
                node_id=node.id,
            )
        descriptor = _descriptor(node.weight_pack, raw, base_dir)
        weights[node.weight_pack] = _bind_descriptor(
            node.weight_pack,
            descriptor,
            tuple(node.weight_shape),
            descriptor.dtype,
        )
    return PackRepository(
        manifest_path=path.resolve(),
        input_tensor=input_pack,
        weights=weights,
    )
