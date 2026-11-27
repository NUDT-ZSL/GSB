"""Small NumPy-native I/O helpers (PGM, JSON, hashes)."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import numpy as np

from .errors import BusinessError


def read_json(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    try:
        value = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BusinessError("E_METADATA_INVALID", f"文件不存在: {p}", details={"path": str(p)}) from exc
    except json.JSONDecodeError as exc:
        raise BusinessError(
            "E_METADATA_INVALID", f"JSON 解析失败: {exc}", details={"path": str(p)}
        ) from exc
    if not isinstance(value, dict):
        raise BusinessError("E_METADATA_INVALID", "JSON 根节点必须是对象", details={"path": str(p)})
    return value


def _json_default(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def write_json(path: str | Path, value: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    text = json.dumps(value, ensure_ascii=False, indent=2, default=_json_default, allow_nan=False)
    tmp.write_text(text + "\n", encoding="utf-8")
    tmp.replace(p)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_pgm(path: str | Path, image: np.ndarray) -> None:
    """Write an 8-bit binary PGM (P5) without external imaging libraries."""
    p = Path(path)
    array = np.asarray(image)
    if array.ndim != 2:
        raise ValueError("PGM image must be two-dimensional")
    if array.dtype != np.uint8:
        array = np.clip(np.rint(array), 0, 255).astype(np.uint8)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("wb") as handle:
        handle.write(b"P5\n")
        handle.write(f"# deterministic clock halftone lab\n{array.shape[1]} {array.shape[0]}\n255\n".encode("ascii"))
        handle.write(array.tobytes(order="C"))


def read_pgm(path: str | Path) -> np.ndarray:
    """Read a binary PGM useful for tests and external preview tools."""
    p = Path(path)
    with p.open("rb") as handle:
        tokens: list[bytes] = []
        while len(tokens) < 4:
            line = handle.readline()
            if not line:
                break
            stripped = line.strip()
            if not stripped or stripped.startswith(b"#"):
                continue
            tokens.extend(stripped.split())
        if len(tokens) < 4 or tokens[0] != b"P5":
            raise ValueError(f"unsupported PGM: {p}")
        width, height, maxval = int(tokens[1]), int(tokens[2]), int(tokens[3])
        if maxval != 255:
            raise ValueError("only 8-bit PGM is supported")
        payload = handle.read(width * height)
    return np.frombuffer(payload, dtype=np.uint8).reshape(height, width).copy()


def copy_tree(source: str | Path, destination: str | Path) -> None:
    dst = Path(destination)
    dst.mkdir(parents=True, exist_ok=True)
    for item in Path(source).iterdir():
        if item.is_file():
            shutil.copy2(item, dst / item.name)
