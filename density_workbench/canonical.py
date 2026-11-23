from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, np.ndarray):
        return _normalize(value.tolist())
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float cannot be canonicalized")
        return value
    if isinstance(value, Path):
        return str(value)
    return value


def canonical_dumps(value: Any) -> str:
    return json.dumps(
        _normalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_dumps(value).encode("utf-8")


def sha256_obj(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_json_atomic(path: str | Path, value: Any, *, indent: int | None = None) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        _normalize(value),
        ensure_ascii=False,
        sort_keys=True,
        indent=indent,
        separators=(",", ":") if indent is None else None,
        allow_nan=False,
    )
    tmp = target.with_name(f".{target.name}.tmp-{id(value)}")
    tmp.write_text(text + ("\n" if indent is not None else ""), encoding="utf-8")
    tmp.replace(target)
