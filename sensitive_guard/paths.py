"""Canonical field-path parsing.

The canonical form always uses bracketed array indexes and dot-separated
object properties, for example ``orders[0].customer.name``.  Both
``orders.0.customer.name`` and ``orders[0].customer.name`` normalize to that
same key.
"""

from __future__ import annotations

import re


class PathError(ValueError):
    """Raised when a field path cannot be normalized."""


_INDEX_GROUP = re.compile(r"^\[([0-9]+)\]")
_PROPERTY = re.compile(r"^[^\s.\[\]]+$")


def _normalize_index(value: str) -> str:
    try:
        return str(int(value))
    except (ValueError, OverflowError) as exc:
        raise PathError(f"invalid array index {value!r}") from exc


def _parse_piece(piece: str) -> str:
    if not piece:
        raise PathError("empty path segment")

    bracket_at = piece.find("[")
    if bracket_at < 0:
        base = piece
        suffix = ""
    else:
        base = piece[:bracket_at]
        suffix = piece[bracket_at:]

    indexes: list[str] = []
    rest = suffix
    while rest:
        match = _INDEX_GROUP.match(rest)
        if not match:
            raise PathError(f"invalid array index syntax in {piece!r}")
        indexes.append(_normalize_index(match.group(1)))
        rest = rest[match.end() :]

    if not base and not indexes:
        raise PathError("empty path segment")
    if base and not _PROPERTY.match(base):
        raise PathError(f"invalid property segment {base!r}")

    rendered_indexes = "".join(f"[{index}]" for index in indexes)
    if not base:
        return rendered_indexes
    if base.isdigit():
        return f"[{_normalize_index(base)}]{rendered_indexes}"
    return f"{base}{rendered_indexes}"


def canonical_path(raw: object) -> str:
    """Return the canonical addressing key for *raw*.

    Dot-separated numeric components are treated as array indexes.  Leading
    indexes are supported (``[0].name``).  Empty segments and malformed
    bracket expressions are rejected.
    """

    if not isinstance(raw, str):
        raise PathError("field path must be a string")
    text = raw.strip()
    if not text:
        raise PathError("field path must not be empty")
    if text.startswith(".") or text.endswith("."):
        raise PathError("field path must not start or end with '.'")

    pieces = [_parse_piece(piece) for piece in text.split(".")]
    canonical = pieces[0]
    for piece in pieces[1:]:
        if piece.startswith("["):
            canonical += piece
        else:
            canonical += "." + piece
    return canonical
