from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return format(obj, "f")
        if isinstance(obj, Fraction):
            return {
                "numerator": obj.numerator,
                "denominator": obj.denominator,
                "decimal": format(Decimal(obj.numerator) / Decimal(obj.denominator), "f"),
            }
        return super().default(obj)


def dumps(value: Any, *, indent: int | None = 2) -> str:
    return json.dumps(
        value,
        indent=indent,
        ensure_ascii=False,
        sort_keys=True,
        cls=DecimalEncoder,
    )


def dump_json(path: str | Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(value) + "\n", encoding="utf-8")


def loads(text: str) -> Any:
    return json.loads(text, parse_float=Decimal, parse_int=int)


def load_json(path: str | Path) -> Any:
    return loads(Path(path).read_text(encoding="utf-8"))


def canonical_bytes(value: Any) -> bytes:
    def default(obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return format(obj, "f")
        if isinstance(obj, Fraction):
            return {
                "numerator": obj.numerator,
                "denominator": obj.denominator,
                "decimal": format(Decimal(obj.numerator) / Decimal(obj.denominator), "f"),
            }
        if isinstance(obj, Path):
            return str(obj)
        raise TypeError(f"Object of type {type(obj)!r} is not JSON serializable")

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=default,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_hash(value: Any) -> str:
    return sha256_json(value)[:16]
