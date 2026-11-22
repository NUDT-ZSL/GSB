from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Mapping

from .errors import ErrorCode, fail

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FALLBACK_COUNTER = 0
_COUNTER_LOCK = threading.Lock()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def digest_hex(digest: str) -> str:
    if not is_digest(digest):
        fail(
            ErrorCode.SHARD_DIGEST_MISMATCH,
            f"非法的 sha256 摘要: {digest!r}",
            actual=digest,
        )
    return digest.split(":", 1)[1]


def is_digest(value: Any) -> bool:
    return isinstance(value, str) and bool(_DIGEST_RE.fullmatch(value))


def require_digest(value: Any, *, field: str = "digest", **details: Any) -> str:
    if not is_digest(value):
        fail(
            ErrorCode.INVALID_INPUT,
            f"{field} 必须为 sha256:<64 位小写十六进制>",
            actual=value,
            **details,
        )
    return str(value)


def load_json(path: str | os.PathLike[str]) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        fail(ErrorCode.NOT_FOUND, f"找不到 JSON 文件: {path}", path=str(path))
    except json.JSONDecodeError as exc:
        fail(
            ErrorCode.INVALID_INPUT,
            f"JSON 解析失败: {path}",
            path=str(path),
            line=exc.lineno,
            column=exc.colno,
        )


def atomic_write_bytes(
    path: str | os.PathLike[str],
    data: bytes,
    *,
    sync: bool = True,
) -> None:
    global _FALLBACK_COUNTER
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _COUNTER_LOCK:
        _FALLBACK_COUNTER += 1
        counter = _FALLBACK_COUNTER
    temp = target.with_name(
        f".{target.name}.tmp-{os.getpid()}-{threading.get_ident()}-{counter}"
    )
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            if sync:
                os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def read_bytes(path: str | os.PathLike[str], **details: Any) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except FileNotFoundError:
        fail(ErrorCode.MISSING_SHARD, f"找不到分片文件: {path}", path=str(path), **details)
    except OSError as exc:
        fail(
            ErrorCode.MISSING_SHARD,
            f"无法读取分片文件: {path}: {exc}",
            path=str(path),
            **details,
        )


def resolve_path(base_dir: str | os.PathLike[str], path: str | os.PathLike[str]) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return Path(base_dir) / candidate


def stable_sort_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    return {key: mapping[key] for key in sorted(mapping)}


def int_field(value: Any, name: str, *, minimum: int | None = None, **details: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        fail(ErrorCode.INVALID_INPUT, f"{name} 必须为整数", actual=value, **details)
    if minimum is not None and value < minimum:
        fail(
            ErrorCode.INVALID_INPUT,
            f"{name} 必须 >= {minimum}",
            actual=value,
            **details,
        )
    return value


def float_ratio(value: Any, name: str, **details: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(
            ErrorCode.INVALID_GRAY_RATIO,
            f"{name} 必须为数字",
            actual=value,
            **details,
        )
    result = float(value)
    if not 0.0 <= result <= 1.0:
        fail(
            ErrorCode.INVALID_GRAY_RATIO,
            f"{name} 必须位于 [0, 1]",
            actual=value,
            **details,
        )
    return result


class FileLock:
    """Portable advisory exclusive file lock.

    The lock is used for cross-process serialization of commit-critical work.
    It deliberately has no logical-time semantics.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._handle: BinaryIO | None = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        self._handle = handle
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None


def chunks(data: bytes, size: int) -> Iterator[bytes]:
    for offset in range(0, len(data), size):
        yield data[offset : offset + size]
