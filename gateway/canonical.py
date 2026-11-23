"""Strict JSON loading and canonical serialisation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .errors import GatewayError


class DuplicateKeyError(ValueError):
    def __init__(self, key: str) -> None:
        super().__init__(f"duplicate JSON object key: {key}")
        self.key = key


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(key)
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def loads_strict(text: str, source: str = "<string>") -> Any:
    try:
        return json.loads(
            text,
            object_pairs_hook=_object_pairs,
            parse_constant=_reject_constant,
        )
    except DuplicateKeyError as exc:
        raise GatewayError(
            "E_JSON_DUPLICATE_KEY",
            f"JSON input contains duplicate object key: {exc.key}",
            source=source,
        ) from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise GatewayError(
            "E_JSON_INVALID",
            f"invalid JSON input: {exc}",
            source=source,
        ) from exc


def load_json(path: str | Path) -> Any:
    source = str(path)
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise GatewayError(
            "E_INPUT_IO",
            f"cannot read input file: {exc}",
            source=source,
        ) from exc
    return loads_strict(text, source=source)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_obj(value: Any) -> str:
    return sha256_text(canonical_json(value))


def clone_json(value: Any) -> Any:
    return json.loads(canonical_json(value))
