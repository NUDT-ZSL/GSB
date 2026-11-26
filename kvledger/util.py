from __future__ import annotations

import hashlib
import json
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Optional

from .errors import Code, KvError


LAYER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def read_json(path: os.PathLike[str] | str, *, parse_decimal: bool = False) -> Any:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise KvError(Code.FILE_NOT_FOUND, f"file not found: {path}") from exc
    except OSError as exc:
        raise KvError(Code.FILE_NOT_FOUND, f"cannot read {path}: {exc}") from exc
    try:
        if parse_decimal:
            return json.loads(text, parse_float=Decimal)
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise KvError(Code.JSON_INVALID, f"invalid JSON in {path}: {exc}") from exc


def write_json(path: os.PathLike[str] | str, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def canonical_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def canonical_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: os.PathLike[str] | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_clock_ms(clock_ms: Any) -> int:
    if not is_int(clock_ms) or clock_ms < 0:
        raise KvError(Code.CLOCK_INVALID, "logical clock must be a nonnegative integer millisecond value")
    return int(clock_ms)


def stable_json_dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"


def deterministic_payload(entry_id: str, size_bytes: int) -> bytes:
    """Create an exact-size deterministic simulated KV payload."""
    if size_bytes <= 0:
        raise KvError(Code.KV_BYTES_INVALID, "payload size must be positive")
    seed = hashlib.sha256(entry_id.encode("utf-8")).digest()
    chunks: list[bytes] = []
    counter = 0
    remaining = size_bytes
    while remaining > 0:
        block = hashlib.sha256(seed + counter.to_bytes(8, "big")).digest()
        take = min(remaining, len(block))
        chunks.append(block[:take])
        remaining -= take
        counter += 1
    return b"".join(chunks)


def overlaps(offset_a: int, length_a: int, offset_b: int, length_b: int) -> bool:
    return offset_a < offset_b + length_b and offset_b < offset_a + length_a


def sorted_dicts(items: Iterable[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: item[key])
