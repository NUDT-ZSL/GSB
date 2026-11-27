"""Small deterministic and serialization helpers used by every layer."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable


MASK64 = (1 << 64) - 1


def canonical_json(value: Any, *, pretty: bool = True) -> str:
    """Return the one canonical JSON representation used for stdout and files."""
    if pretty:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            separators=(",", ": "),
        )
    else:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return text + "\n"


def compact_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(compact_json(value))


def canonical_clone(value: Any) -> Any:
    return json.loads(compact_json(value))


def deep_copy(value: Any) -> Any:
    return copy.deepcopy(value)


def attribute_bytes(attributes: Any) -> int:
    if not isinstance(attributes, dict):
        return 0
    return len(compact_json(attributes).encode("utf-8"))


def iso_from_us(microseconds: int) -> str:
    seconds, micros = divmod(int(microseconds), 1_000_000)
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=micros)
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_time_us(value: str | int | None) -> int | None:
    """Parse integer microseconds or an ISO-8601 timestamp."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = value.strip()
    if not text:
        return None
    try:
        return int(text, 10)
    except ValueError:
        pass
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(
            f"time must be integer microseconds or ISO-8601, got {value!r}"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000)


def stable_u64(*parts: Any) -> int:
    material = "\x1f".join(str(part) for part in parts)
    return int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest()[:8], "big")


def stable_bucket(*parts: Any, modulus: int = 10_000) -> int:
    return stable_u64(*parts) % modulus


class StableRandom:
    """SplitMix64-based PRNG whose output does not depend on Python's random module."""

    def __init__(self, seed: int, stream: str = "generator") -> None:
        self._state = stable_u64("obs-telemetry-prng-v1", seed, stream)

    def next_u64(self) -> int:
        self._state = (self._state + 0x9E3779B97F4A7C15) & MASK64
        z = self._state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        return (z ^ (z >> 31)) & MASK64

    def randbelow(self, upper: int) -> int:
        if upper <= 0:
            raise ValueError("upper must be positive")
        return self.next_u64() % upper

    def randint(self, low: int, high: int) -> int:
        if high < low:
            raise ValueError("high must be >= low")
        return low + self.randbelow(high - low + 1)

    def choice(self, values: Iterable[Any]) -> Any:
        sequence = list(values)
        if not sequence:
            raise ValueError("cannot choose from an empty sequence")
        return sequence[self.randbelow(len(sequence))]

    def chance(self, numerator: int, denominator: int) -> bool:
        if denominator <= 0:
            raise ValueError("denominator must be positive")
        return self.randbelow(denominator) < numerator


def first_by(rows: Iterable[dict[str, Any]], keys: tuple[str, ...]) -> dict[str, Any] | None:
    rows_list = list(rows)
    if not rows_list:
        return None
    return min(rows_list, key=lambda row: tuple(str(row.get(key, "")) for key in keys))

