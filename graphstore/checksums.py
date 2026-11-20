from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

from .errors import E_INVALID_JSON, GraphStoreError, require


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_object(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


class _DuplicateKey(ValueError):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def load_json(path: Path, *, label: str = "JSON file") -> Any:
    require(path.is_file(), E_INVALID_JSON, f"{label} does not exist", path=str(path))
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GraphStoreError(
            E_INVALID_JSON, f"cannot read {label}", path=str(path), reason=str(exc)
        ) from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=_reject_constant,
        )
    except _DuplicateKey as exc:
        raise GraphStoreError(
            "GS_DUPLICATE_JSON_KEY",
            "JSON object contains an ambiguous duplicate key",
            path=str(path),
            key=exc.key,
        ) from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise GraphStoreError(
            E_INVALID_JSON, f"invalid {label}", path=str(path), reason=str(exc)
        ) from exc


def is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def digest_edges(edges: Iterable[tuple[str, str, str, float]]) -> str:
    rows = [
        [source, target, label, float(weight)]
        for source, target, label, weight in sorted(
            edges, key=lambda item: (item[0], item[1], item[2], item[3])
        )
    ]
    return sha256_object(rows)


def tiered_sort_key(source_format: str, logical_clock_ms: int, generation: int) -> tuple:
    priority = 20 if source_format == "v2" else 10
    return (logical_clock_ms, generation, priority)

