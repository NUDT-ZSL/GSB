from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Iterable


NULL_KEY_TOKEN = "\x00null"


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_value(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def key_token(key: Any) -> str:
    if key is None:
        return NULL_KEY_TOKEN
    if isinstance(key, bool):
        return f"b:{int(key)}"
    if isinstance(key, int):
        return f"i:{key}"
    if isinstance(key, str):
        return f"s:{key}"
    if isinstance(key, float) and math.isfinite(key):
        return f"f:{key:.17g}"
    return "j:" + canonical_json(key)


def key_sort_value(key: Any) -> tuple[int, Any]:
    if isinstance(key, bool):
        return (0, int(key))
    if isinstance(key, int):
        return (0, key)
    if isinstance(key, float):
        return (0, key)
    if isinstance(key, str):
        return (1, key)
    return (2, key_token(key))


def merge_sort_key(row: dict[str, Any]) -> tuple[int, str, int]:
    key = row.get("_join_key")
    if key is None:
        return (1, "", int(row["row_id"]))
    return (0, key_token(key), int(row["row_id"]))


def result_order_key(row: dict[str, Any]) -> tuple[int, int, int]:
    left_id = row.get("left_row_id")
    right_id = row.get("right_row_id")
    if left_id is not None:
        return (0, int(left_id), -1 if right_id is None else int(right_id))
    return (1, int(right_id), 0)


def stable_mod(text: str, modulus: int) -> int:
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % modulus


def filter_passes(query_id: str, left_id: int, right_id: int, selectivity: float) -> bool:
    if selectivity >= 1.0:
        return True
    bucket = stable_mod(f"{query_id}|residual|{left_id}|{right_id}", 1_000_000)
    return bucket < int(selectivity * 1_000_000)


def digest_rows(rows: Iterable[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(canonical_bytes(row))
        digest.update(b"\n")
    return digest.hexdigest()


def table_row_digest(row: dict[str, Any]) -> bytes:
    return canonical_bytes(row)


@dataclass
class LogicalClock:
    step: float
    start: float = 0.0

    def __post_init__(self) -> None:
        self._units = 0
        self.current = float(self.start)

    def tick(self, units: int = 1) -> float:
        if units < 0:
            raise ValueError("logical clock units must be non-negative")
        self._units += int(units)
        self.current = float(self.start) + self._units * self.step
        return self.current

    @property
    def elapsed(self) -> float:
        return float(self._units) * self.step

    @property
    def units(self) -> int:
        return self._units

