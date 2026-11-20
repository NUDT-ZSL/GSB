from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from .errors import GSBIoError


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle, object_pairs_hook=_reject_duplicate_keys)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise GSBIoError(
            f"cannot parse JSON file {path}",
            {"path": str(path), "cause": str(exc)},
        ) from exc


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def stable_json(value: Any, *, indent: int = 2) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=True,
        sort_keys=True,
        indent=indent,
    ) + "\n"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def normalize_digest(value: str) -> str:
    text = value.strip().lower()
    for prefix in ("sha256:", "sha-256:", "sha256="):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise GSBIoError(
            f"cannot atomically write {path}",
            {"path": str(path), "cause": str(exc)},
        ) from exc


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_finite_number(value: Any) -> bool:
    return is_number(value) and math.isfinite(float(value))


def number_token(value: int | float) -> str:
    try:
        decimal = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"invalid numeric key: {value!r}") from exc
    if decimal == 0:
        return "n:0"
    return "n:" + format(decimal.normalize(), "f")


def group_key_token(value: Any) -> str:
    if isinstance(value, bool):
        return "b:1" if value else "b:0"
    if is_number(value):
        if not math.isfinite(float(value)):
            raise ValueError("NaN and infinity are not valid group keys")
        return number_token(value)
    if isinstance(value, str):
        return "s:" + canonical_json(value)
    raise ValueError(f"unsupported group key type: {type(value).__name__}")


def group_key_sort_key(value: Any) -> tuple[Any, ...]:
    if isinstance(value, bool):
        return (0, 1 if value else 0)
    if is_number(value):
        return (1, Decimal(str(value)))
    if isinstance(value, str):
        return (2, value)
    return (3, repr(value))


def sort_key_token(value: Any) -> str:
    rank, comparable = group_key_sort_key(value)
    if rank == 1:
        return f"1:{format(comparable.normalize(), 'f')}"
    if rank == 2:
        return "2:" + canonical_json(comparable)
    return f"{rank}:{canonical_json(comparable)}"


def json_type_tag(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return type(value).__name__


def same_json_bitwise(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            if isinstance(left, bool) or isinstance(right, bool):
                return False
            return left == right
        return False
    if isinstance(left, float):
        if math.isnan(left) or math.isnan(right):
            return math.isnan(left) and math.isnan(right)
        return left.hex() == right.hex()
    if isinstance(left, dict):
        if set(left) != set(right):
            return False
        return all(same_json_bitwise(left[key], right[key]) for key in left)
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(
            same_json_bitwise(a, b) for a, b in zip(left, right)
        )
    return left == right


def first_difference(left: Any, right: Any, path: str = "$") -> dict[str, Any] | None:
    if type(left) is not type(right):
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            if isinstance(left, bool) or isinstance(right, bool):
                return {"path": path, "left": left, "right": right}
            if same_json_bitwise(left, right):
                return None
        return {"path": path, "left": left, "right": right}
    if isinstance(left, float):
        if same_json_bitwise(left, right):
            return None
        return {"path": path, "left": left.hex(), "right": right.hex()}
    if isinstance(left, dict):
        if set(left) != set(right):
            return {"path": path, "left": sorted(left), "right": sorted(right)}
        for key in sorted(left):
            diff = first_difference(left[key], right[key], f"{path}.{key}")
            if diff is not None:
                return diff
        return None
    if isinstance(left, (list, tuple)):
        if len(left) != len(right):
            return {"path": path, "left": len(left), "right": len(right)}
        for index, (a, b) in enumerate(zip(left, right)):
            diff = first_difference(a, b, f"{path}[{index}]")
            if diff is not None:
                return diff
        return None
    if left != right:
        return {"path": path, "left": left, "right": right}
    return None


def chunked(items: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]

