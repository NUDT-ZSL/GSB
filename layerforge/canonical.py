from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def canonical_digest(value: Any, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm)
    digest.update(canonical_bytes(value))
    return f"{algorithm}:{digest.hexdigest()}"


def pretty_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def byte_digest(data: bytes, algorithm: str = "sha256") -> str:
    return f"{algorithm}:{hashlib.new(algorithm, data).hexdigest()}"


def file_digest(path: os.PathLike[str] | str, algorithm: str = "sha256") -> str:
    h = hashlib.new(algorithm)
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return f"{algorithm}:{h.hexdigest()}"


def atomic_write_bytes(path: Path, data: bytes, *, fsync: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        if fsync:
            _fsync_directory(path.parent)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(directory, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json_atomic(path: Path, value: Any) -> None:
    atomic_write_bytes(path, pretty_json_bytes(value))


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def stable_bucket(digest: str, bucket_count: int) -> int:
    material = digest.split(":", 1)[-1].encode("ascii")
    return int(hashlib.sha256(b"bucket|" + material).hexdigest()[:16], 16) % bucket_count


def safe_slug(value: str) -> str:
    out = []
    for char in value:
        if char.isascii() and (char.isalnum() or char in "._-"):
            out.append(char)
        else:
            out.append("_")
    return "".join(out)


def sorted_dict(value: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value[key] for key in sorted(value)}
