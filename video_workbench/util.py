from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def array_fingerprint(array: np.ndarray) -> str:
    contiguous = np.ascontiguousarray(array)
    digest = hashlib.sha256()
    digest.update(contiguous.dtype.str.encode("ascii"))
    digest.update(canonical_json(list(contiguous.shape)).encode("ascii"))
    digest.update(contiguous.tobytes(order="C"))
    return digest.hexdigest()


def payload_fingerprint(metadata: Mapping[str, Any], arrays: Mapping[str, np.ndarray]) -> str:
    digest = hashlib.sha256()
    digest.update(canonical_json(metadata).encode("utf-8"))
    for key in sorted(arrays):
        array = np.ascontiguousarray(arrays[key])
        digest.update(key.encode("utf-8"))
        digest.update(array.dtype.str.encode("ascii"))
        digest.update(canonical_json(list(array.shape)).encode("ascii"))
        digest.update(array.tobytes(order="C"))
    return digest.hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(path: str | Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: str | Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(json_safe(value), indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def atomic_write_npy(path: str | Path, array: np.ndarray) -> None:
    buffer = io.BytesIO()
    np.save(buffer, np.ascontiguousarray(array), allow_pickle=False)
    atomic_write_bytes(path, buffer.getvalue())


def read_json(path: str | Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def read_json_bytes(data: bytes) -> Any:
    return json.loads(data.decode("utf-8"))


def encode_npz(metadata: Mapping[str, Any], arrays: Mapping[str, np.ndarray]) -> bytes:
    """Create a deterministic NPZ container with fixed ZIP timestamps."""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        info = zipfile.ZipInfo("metadata.json", date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o600 << 16
        archive.writestr(info, canonical_json(json_safe(metadata)).encode("utf-8"))
        for key in sorted(arrays):
            info = zipfile.ZipInfo(f"arrays/{key}.npy", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            array_buffer = io.BytesIO()
            np.save(array_buffer, np.ascontiguousarray(arrays[key]), allow_pickle=False)
            archive.writestr(info, array_buffer.getvalue())
    return buffer.getvalue()


def decode_npz(data: bytes) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
        names = set(archive.namelist())
        if "metadata.json" not in names:
            raise ValueError("artifact metadata.json is missing")
        metadata = read_json_bytes(archive.read("metadata.json"))
        arrays: dict[str, np.ndarray] = {}
        for name in sorted(names):
            if not name.startswith("arrays/") or not name.endswith(".npy"):
                continue
            key = name[len("arrays/") : -len(".npy")]
            arrays[key] = np.load(io.BytesIO(archive.read(name)), allow_pickle=False)
    if not isinstance(metadata, dict):
        raise ValueError("artifact metadata must be an object")
    return metadata, arrays


def atomic_write_npz(path: str | Path, metadata: Mapping[str, Any], arrays: Mapping[str, np.ndarray]) -> bytes:
    data = encode_npz(metadata, arrays)
    atomic_write_bytes(path, data)
    return data


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def merge_mapping(base: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(json_safe(base)))
    for key, value in update.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = merge_mapping(merged[key], value)
        else:
            merged[key] = json_safe(value)
    return merged


def stable_sort_key(stage: str, step: int | None, frame: int | None) -> list[object]:
    return [stage, -1 if step is None else int(step), -1 if frame is None else int(frame)]


def key_tuple(key: list[object]) -> tuple[object, ...]:
    return tuple(key)
