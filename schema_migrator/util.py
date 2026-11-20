from __future__ import annotations

import hashlib
import json
import os
import time
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any, Iterable, Optional, Union


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def pretty_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def digest_file(path: Union[str, Path]) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def decimal_text(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, float):
        dec = Decimal(str(value))
    elif isinstance(value, int):
        dec = Decimal(value)
    else:
        dec = Decimal(str(value))
    if not dec.is_finite():
        raise ValueError("non-finite decimal")
    text = format(dec, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        return "0"
    return text


def quantize_decimal(value: Any, scale: int, rounding: str = "ROUND_HALF_EVEN") -> Decimal:
    modes = {
        "ROUND_HALF_EVEN": ROUND_HALF_EVEN,
        "ROUND_HALF_UP": "ROUND_HALF_UP",
        "ROUND_DOWN": "ROUND_DOWN",
        "ROUND_UP": "ROUND_UP",
    }
    if rounding not in modes:
        raise ValueError(f"unsupported rounding mode: {rounding}")
    quant = Decimal(1).scaleb(-scale)
    return Decimal(str(value)).quantize(quant, rounding=modes[rounding])


def canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return {"$float": repr(value)}
        return {"$float": repr(value)}
    if isinstance(value, bytes):
        return {"$bytes": value.hex()}
    return str(value)


def canonical_rows(rows: Iterable[Iterable[Any]]) -> list:
    return [[canonical_value(value) for value in row] for row in rows]


def fsync_path(path: Path) -> None:
    try:
        with open(path, "rb") as handle:
            os.fsync(handle.fileno())
    except OSError:
        pass


def fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


class FileLock:
    """Small cross-platform advisory writer lock.

    Readers deliberately do not take this lock. They only consume an atomic
    catalog pointer to an immutable snapshot.
    """

    def __init__(self, path: Union[str, Path], timeout: float = 30.0) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.handle: Optional[Any] = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"\0")
            self.handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._try_lock()
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise TimeoutError("writer lock timeout")
                time.sleep(0.05)

    def _try_lock(self) -> None:
        assert self.handle is not None
        if os.name == "nt":
            import msvcrt

            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None

