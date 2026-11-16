"""Small deterministic helpers shared by the engine."""

from __future__ import annotations

import hashlib
import json
import math
import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def round_half_up(value: float | Decimal) -> int:
    """Deterministic rounding that does not depend on banker's rounding."""

    if not math.isfinite(float(value)):
        raise ValueError("cannot round a non-finite value")
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def ratio_ppm(estimated: int, actual: int) -> int | None:
    if actual == 0:
        return None
    return round_half_up(Decimal(estimated) * Decimal(1_000_000) / Decimal(actual))


def deviation_score(estimated: int, actual: int) -> int:
    """Absolute relative error in basis points, capped at 10000.

    Zero actual rows are handled without division.  If the estimate is also
    zero, the score is zero; otherwise a perfect-sentinel score of 10000 is
    returned.
    """

    if actual == 0:
        return 0 if estimated == 0 else 10_000
    value = round_half_up(
        Decimal(abs(estimated - actual)) * Decimal(10_000) / Decimal(actual)
    )
    return min(10_000, value)


def heat_band(score: int) -> str:
    if score < 100:
        return "low"
    if score < 500:
        return "medium"
    if score < 2000:
        return "high"
    return "critical"


def typed_value(value: Any) -> dict[str, Any]:
    """A JSON-safe type-tagged value used in content digests."""

    if value is None:
        return {"t": "null", "v": None}
    if isinstance(value, bool):
        return {"t": "bool", "v": bool(value)}
    if isinstance(value, int):
        return {"t": "int", "v": int(value)}
    if isinstance(value, float):
        if not math.isfinite(value):
            return {"t": "float", "v": repr(value)}
        return {"t": "float", "v": repr(value)}
    if isinstance(value, bytes):
        return {"t": "blob", "v": value.hex()}
    return {"t": "text", "v": str(value)}


def typed_row(row: Iterable[Any]) -> list[dict[str, Any]]:
    return [typed_value(value) for value in row]


def sql_quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def sql_quote_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def parse_null_overrides(values: list[str] | None) -> dict[str, float]:
    overrides: dict[str, float] = {}
    for raw in values or []:
        if "=" not in raw:
            raise ValueError(f"invalid null override: {raw!r}; expected table.column=0.0..1.0")
        key, raw_value = raw.split("=", 1)
        key = key.strip().lower()
        if not re.fullmatch(r"[a-z_][a-z0-9_$]*\.[a-z_][a-z0-9_$]*", key):
            raise ValueError(f"invalid null override column: {key!r}")
        fraction = float(raw_value)
        if fraction < 0.0 or fraction > 1.0:
            raise ValueError(f"null fraction out of range: {fraction}")
        overrides[key] = fraction
    return dict(sorted(overrides.items()))


def split_sql_script(script: str) -> list[str]:
    """Split a SQL script without executing it and without implicit commits.

    Semicolons inside SQL strings, quoted identifiers and comments are ignored.
    """

    statements: list[str] = []
    buffer: list[str] = []
    i = 0
    quote: str | None = None
    bracket = False
    line_comment = False
    block_comment = False
    while i < len(script):
        ch = script[i]
        nxt = script[i + 1] if i + 1 < len(script) else ""
        if line_comment:
            buffer.append(ch)
            if ch == "\n":
                line_comment = False
            i += 1
            continue
        if block_comment:
            buffer.append(ch)
            if ch == "*" and nxt == "/":
                buffer.append(nxt)
                block_comment = False
                i += 2
            else:
                i += 1
            continue
        if quote is not None:
            buffer.append(ch)
            if ch == quote:
                if nxt == quote:
                    buffer.append(nxt)
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if bracket:
            buffer.append(ch)
            if ch == "]":
                bracket = False
            i += 1
            continue
        if ch == "-" and nxt == "-":
            buffer.extend((ch, nxt))
            line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            buffer.extend((ch, nxt))
            block_comment = True
            i += 2
            continue
        if ch == "'" or ch == '"':
            buffer.append(ch)
            quote = ch
            i += 1
            continue
        if ch == "[":
            buffer.append(ch)
            bracket = True
            i += 1
            continue
        if ch == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            i += 1
            continue
        buffer.append(ch)
        i += 1
    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


def read_text(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def ensure_parent(path: str | Path) -> Path:
    result = Path(path)
    result.parent.mkdir(parents=True, exist_ok=True)
    return result
