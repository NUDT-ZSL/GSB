from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import math
import os
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterator

from .errors import BusinessError, E_IO


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def require_nonneg_int(value: Any, field: str, code: str = "E_BAD_SAMPLE") -> int:
    if not is_int(value) or value < 0:
        raise BusinessError(code, f"{field} must be a non-negative integer", {"field": field, "value": value})
    return value


def require_int(value: Any, field: str, code: str = "E_BAD_SAMPLE") -> int:
    if not is_int(value):
        raise BusinessError(code, f"{field} must be an integer", {"field": field, "value": value})
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def rounded_number(value: Decimal | int | float | str, places: int = 6) -> float:
    quantum = Decimal(1).scaleb(-places)
    result = Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)
    return float(result)


def rounded_decimal(value: Decimal | int | float | str, places: int = 6) -> Decimal:
    quantum = Decimal(1).scaleb(-places)
    return Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)


def safe_ratio(numerator: Decimal | int | float | str, denominator: Decimal | int | float | str) -> float | None:
    den = Decimal(str(denominator))
    if den == 0:
        return None
    return rounded_number(Decimal(str(numerator)) / den)


def normalize_sqlite_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return {"$float": str(value)}
    return value


def quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def read_json_file(path: str | os.PathLike[str], code: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise BusinessError(code, "JSON file does not exist", {"path": str(path)}) from exc
    except (json.JSONDecodeError, OSError) as exc:
        raise BusinessError(code, "cannot parse JSON file", {"path": str(path), "reason": str(exc)}) from exc


def ensure_parent(path: str | os.PathLike[str]) -> None:
    parent = Path(path).resolve().parent
    parent.mkdir(parents=True, exist_ok=True)


class FileLock:
    """Process-level advisory lock used around every state mutation."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = str(path)
        self.handle = None

    def __enter__(self) -> "FileLock":
        ensure_parent(self.path)
        self.handle = open(self.path, "a+b")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


@contextlib.contextmanager
def transaction(conn: Any) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def fault_inject(point: str) -> None:
    """Hard-exit after a journal boundary; used to verify crash recovery."""

    if os.environ.get("SLOWPLAN_FAULT_POINT") == point:
        os._exit(91)
