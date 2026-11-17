"""Addressing normalisation for the two supported field-address formats.

The workbench accepts two textual addressing formats:

* dot paths:            ``a.b.c``
* array index paths:    ``a[2].b`` (``users[0].id_number``)

Both are parsed into a *token tuple* where object members are ``str`` tokens
and array positions are ``int`` tokens.  The token tuple is rendered into a
single canonical addressing key via :func:`canonical_key`, so the same logical
field always has one identity regardless of which textual spelling was used.

The module deliberately depends only on the Python standard library so it runs
unchanged on an offline Windows workstation.
"""

from __future__ import annotations

import re
from typing import Any, List, Sequence, Tuple, Union

Token = Union[str, int]

_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
# A leading segment must be a bare object member name.
_PATH_RE = re.compile(rf"^{_NAME}(?:\.{_NAME}|\[\d+\])*$")
_NAME_RE = re.compile(_NAME)


class PathError(ValueError):
    """Raised when an addressing string is syntactically invalid."""


def parse_path(text: str) -> Tuple[Token, ...]:
    """Parse a dot/index addressing string into a normalised token tuple.

    Raises :class:`PathError` for empty input or any malformed spelling.
    """

    if not isinstance(text, str) or not text:
        raise PathError("empty or non-string path")
    if not _PATH_RE.match(text):
        raise PathError(f"illegal addressing format: {text!r}")

    head = _NAME_RE.match(text)
    tokens: List[Token] = [head.group(0)]
    index = head.end()
    size = len(text)
    while index < size:
        if text[index] == ".":
            member = _NAME_RE.match(text, index + 1)
            if member is None:
                raise PathError(f"illegal addressing format: {text!r}")
            tokens.append(member.group(0))
            index = member.end()
        elif text[index] == "[":
            close = text.find("]", index)
            digits = text[index + 1:close]
            if close == -1 or not digits.isdigit():
                raise PathError(f"illegal array index in: {text!r}")
            tokens.append(int(digits))
            index = close + 1
        else:
            raise PathError(f"illegal addressing format: {text!r}")
    return tuple(tokens)


def canonical_key(tokens: Sequence[Token]) -> str:
    """Render a token tuple into the canonical addressing key.

    Object members are joined with ``.`` and array positions rendered as
    ``[index]``.  This is the single identity used everywhere in reports.
    """

    parts: List[str] = []
    for token in tokens:
        if isinstance(token, int):
            parts.append(f"[{token}]")
        else:
            parts.append(str(token))
    key = ""
    for part in parts:
        if part.startswith("["):
            key += part
        else:
            key = part if not key else key + "." + part
    return key


def normalize(text: str) -> str:
    """Convenience helper: text -> canonical key."""

    return canonical_key(parse_path(text))


class BoundaryError(LookupError):
    """Raised when an array index is out of range or a member is missing."""


def resolve(data: Any, tokens: Sequence[Token]) -> Any:
    """Walk ``data`` following ``tokens`` and return the addressed value.

    Missing object members and out-of-range array indexes raise
    :class:`BoundaryError`, which the parser converts into an ``E_FIELD``
    rejection record while continuing with the remaining entries.
    """

    current = data
    for depth, token in enumerate(tokens):
        if isinstance(token, int):
            if not isinstance(current, list):
                raise BoundaryError(
                    f"index {token} applied to non-array at {canonical_key(tokens[:depth])}"
                )
            if token < 0 or token >= len(current):
                raise BoundaryError(
                    f"array index {token} out of range (size {len(current)}) at "
                    f"{canonical_key(tokens[:depth + 1])}"
                )
            current = current[token]
        else:
            if not isinstance(current, dict) or token not in current:
                raise BoundaryError(
                    f"missing object member {token!r} at "
                    f"{canonical_key(tokens[:depth + 1])}"
                )
            current = current[token]
    return current
