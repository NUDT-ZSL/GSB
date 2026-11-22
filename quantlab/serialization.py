from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .errors import BusinessError, E_IO


DTYPE_TO_NAME = {
    np.dtype("float64"): "float64",
    np.dtype("int64"): "int64",
    np.dtype("int32"): "int32",
    np.dtype("uint8"): "uint8",
    np.dtype("bool"): "bool",
}
NAME_TO_DTYPE = {v: k for k, v in DTYPE_TO_NAME.items()}


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_text(value: Any, *, indent: int | None = None) -> str:
    if indent is None:
        return canonical_json_bytes(value).decode("utf-8")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ": "),
        ensure_ascii=False,
        allow_nan=False,
        indent=indent,
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_checksum(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def _little_endian_view(array: np.ndarray) -> np.ndarray:
    array = np.ascontiguousarray(array)
    if array.dtype.byteorder == ">":
        array = array.byteswap().view(array.dtype.newbyteorder("<"))
    return array


def encode_array(array: np.ndarray) -> dict[str, Any]:
    arr = _little_endian_view(np.asarray(array))
    dtype_name = DTYPE_TO_NAME.get(arr.dtype)
    if dtype_name is None:
        if np.issubdtype(arr.dtype, np.floating):
            arr = arr.astype("<f8", copy=False)
            dtype_name = "float64"
        elif np.issubdtype(arr.dtype, np.integer):
            arr = arr.astype("<i8", copy=False)
            dtype_name = "int64"
        else:
            raise BusinessError(
                E_IO,
                f"不支持序列化的数组类型: {arr.dtype}",
                details={"dtype": str(arr.dtype)},
            )
    return {
        "dtype": dtype_name,
        "shape": list(arr.shape),
        "data": base64.b64encode(arr.tobytes(order="C")).decode("ascii"),
    }


def decode_array(payload: dict[str, Any]) -> np.ndarray:
    dtype_name = payload.get("dtype")
    if dtype_name not in NAME_TO_DTYPE:
        raise BusinessError(
            E_IO,
            f"数组序列化 dtype 非法: {dtype_name!r}",
            details={"dtype": dtype_name},
        )
    dtype = NAME_TO_DTYPE[dtype_name].newbyteorder("<")
    try:
        raw = base64.b64decode(payload["data"], validate=True)
        shape = tuple(int(x) for x in payload["shape"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BusinessError(E_IO, "数组序列化载荷损坏", details={"reason": str(exc)}) from exc
    expected = int(np.prod(shape, dtype=np.int64)) * dtype.itemsize
    if len(raw) != expected:
        raise BusinessError(
            E_IO,
            "数组序列化长度与 shape 不一致",
            details={"expected": expected, "actual": len(raw), "shape": list(shape)},
        )
    return np.frombuffer(raw, dtype=dtype).reshape(shape).astype(dtype.newbyteorder("="), copy=True)


def array_checksum(array: np.ndarray) -> str:
    arr = _little_endian_view(np.asarray(array))
    header = canonical_json_bytes({"dtype": str(arr.dtype), "shape": list(arr.shape)})
    return sha256_bytes(header + b"|" + arr.tobytes(order="C"))


def array_equal_bytes(left: np.ndarray, right: np.ndarray) -> bool:
    a = np.ascontiguousarray(left)
    b = np.ascontiguousarray(right)
    return a.shape == b.shape and a.dtype == b.dtype and a.tobytes() == b.tobytes()


def arrays_equal_bytes(pairs: Iterable[tuple[np.ndarray, np.ndarray]]) -> bool:
    return all(array_equal_bytes(a, b) for a, b in pairs)


def atomic_write(path: Path, data: bytes, *, temp_dir: Path) -> None:
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=temp_dir)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        try:
            parent_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            parent_fd = None
        if parent_fd is not None:
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Any, *, temp_dir: Path, indent: int | None = None) -> None:
    text = canonical_json_text(value, indent=indent) + "\n"
    atomic_write(path, text.encode("utf-8"), temp_dir=temp_dir)


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError(
            E_IO,
            f"读取 JSON 失败: {path}",
            details={"path": str(path), "reason": str(exc)},
        ) from exc
