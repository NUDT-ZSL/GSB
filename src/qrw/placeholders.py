from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .errors import BusinessError, E_FORMAT_CORRUPT


_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class PlaceholderSpan:
    name: str
    start: int
    end: int


def scan_placeholders(
    text: str,
    *,
    dialect: str,
    source_name: str,
    line: int,
    field: str,
) -> list[PlaceholderSpan]:
    spans: list[PlaceholderSpan] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "}":
            raise BusinessError(
                E_FORMAT_CORRUPT,
                f"{dialect} {field} contains an unmatched closing brace",
                line=line,
                field=field,
                column=index + 1,
                details={"source": source_name, "dialect": dialect},
            )
        if char != "{":
            index += 1
            continue
        close = text.find("}", index + 1)
        nested = text.find("{", index + 1)
        if close < 0 or (nested >= 0 and nested < close):
            raise BusinessError(
                E_FORMAT_CORRUPT,
                f"{dialect} {field} contains an unterminated placeholder",
                line=line,
                field=field,
                column=index + 1,
                details={"source": source_name, "dialect": dialect},
            )
        name = text[index + 1 : close]
        if not _NAME_RE.fullmatch(name):
            raise BusinessError(
                E_FORMAT_CORRUPT,
                f"{dialect} {field} has an invalid placeholder name",
                line=line,
                field=field,
                column=index + 2,
                details={"source": source_name, "dialect": dialect, "placeholder": name},
            )
        spans.append(PlaceholderSpan(name=name, start=index, end=close + 1))
        index = close + 1
    return spans


def placeholder_names(spans: Iterable[PlaceholderSpan]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for span in spans:
        if span.name not in seen:
            seen.add(span.name)
            result.append(span.name)
    return result

