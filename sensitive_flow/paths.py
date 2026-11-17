"""Canonical field addressing for dotted and indexed JSON paths."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .errors import E_PATH, E_PATH_INDEX, TraceError


@dataclass(frozen=True)
class PathToken:
    kind: str
    value: str | int


def _parse_dot_path(raw: str) -> list[PathToken]:
    tokens: list[PathToken] = []
    index = 0
    expecting_name = True
    length = len(raw)

    while index < length:
        char = raw[index]
        if char == ".":
            if expecting_name or index == length - 1:
                raise TraceError(E_PATH, f"invalid empty segment in path: {raw}")
            index += 1
            expecting_name = True
            continue
        if char == "[":
            if expecting_name and not tokens:
                pass
            elif expecting_name:
                raise TraceError(E_PATH, f"unexpected array segment in path: {raw}")
            end = raw.find("]", index + 1)
            if end < 0:
                raise TraceError(E_PATH, f"unterminated array index in path: {raw}")
            content = raw[index + 1 : end].strip()
            if not content.isdigit():
                raise TraceError(E_PATH, f"invalid array index in path: {raw}")
            tokens.append(PathToken("index", int(content)))
            index = end + 1
            expecting_name = False
            continue
        if char == "]":
            raise TraceError(E_PATH, f"unexpected ']' in path: {raw}")

        if not expecting_name:
            raise TraceError(E_PATH, f"missing separator in path: {raw}")
        start = index
        while index < length and raw[index] not in ".[]":
            index += 1
        segment = raw[start:index]
        if not segment or segment != segment.strip() or any(c.isspace() for c in segment):
            raise TraceError(E_PATH, f"invalid field segment in path: {raw}")
        if segment.isdigit():
            tokens.append(PathToken("index", int(segment)))
        else:
            tokens.append(PathToken("name", segment))
        expecting_name = False

    if expecting_name or not tokens:
        raise TraceError(E_PATH, f"invalid path: {raw}")
    return tokens


def _parse_pointer_path(raw: str) -> list[PathToken]:
    body = raw[1:]
    if not body:
        raise TraceError(E_PATH, "JSON pointer must address a field")
    parts = body.split("/")[1:] if body.startswith("/") else body.split("/")
    tokens: list[PathToken] = []
    for part in parts:
        if not part:
            raise TraceError(E_PATH, f"invalid empty JSON pointer segment: {raw}")
        value = part.replace("~1", "/").replace("~0", "~")
        if any(c.isspace() for c in value):
            raise TraceError(E_PATH, f"invalid JSON pointer segment: {raw}")
        tokens.append(
            PathToken("index", int(value)) if value.isdigit() else PathToken("name", value)
        )
    return tokens


def parse_path(raw: object) -> list[PathToken]:
    if not isinstance(raw, str):
        raise TraceError(E_PATH, "field path must be a string")
    text = raw.strip()
    if not text:
        raise TraceError(E_PATH, "field path must not be empty")
    if text.startswith("/"):
        return _parse_pointer_path(text)
    if text.startswith("$"):
        text = text[1:]
        if text.startswith("."):
            text = text[1:]
        if not text:
            raise TraceError(E_PATH, "root path must address a field")
    return _parse_dot_path(text)


def render_path(tokens: Iterable[PathToken]) -> str:
    result = ""
    for token in tokens:
        if token.kind == "index":
            result += f"[{token.value}]"
        else:
            if result and not result.endswith("]"):
                result += "."
            elif result:
                result += "."
            result += str(token.value)
    return result


def canonicalize_path(raw: object) -> str:
    return render_path(parse_path(raw))


def parse_array_sizes(value: object) -> dict[str, int]:
    """Accept either a mapping or a list of {path,length/size} declarations."""

    result: dict[str, int] = {}
    if value is None:
        return result
    if isinstance(value, Mapping):
        items = value.items()
        for path, size in items:
            key = canonicalize_path(path)
            if isinstance(size, list):
                size = len(size)
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise TraceError(
                    E_ARRAY_BOUNDS,
                    f"array size for {key} must be a non-negative integer",
                    {"path": key},
                )
            result[key] = size
        return result
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, Mapping):
                raise TraceError(E_ARRAY_BOUNDS, "array size entries must be objects")
            path = item.get("path")
            size = item.get("length", item.get("size", item.get("count")))
            key = canonicalize_path(path)
            if isinstance(size, list):
                size = len(size)
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise TraceError(
                    E_ARRAY_BOUNDS,
                    f"array size for {key} must be a non-negative integer",
                    {"path": key},
                )
            result[key] = size
        return result
    raise TraceError(E_ARRAY_BOUNDS, "arrays declaration must be an object or list")


def validate_array_bounds(tokens: list[PathToken], array_sizes: Mapping[str, int]) -> None:
    for position, token in enumerate(tokens):
        if token.kind != "index":
            continue
        base = render_path(tokens[:position])
        if base in array_sizes:
            size = array_sizes[base]
            if token.value >= size:
                raise TraceError(
                    E_PATH_INDEX,
                    f"array index {token.value} is outside {base}[0..{max(size - 1, 0)}]",
                    {"array": base, "index": token.value, "length": size},
                )


def canonical_and_validate(raw: object, array_sizes: Mapping[str, int]) -> str:
    tokens = parse_path(raw)
    validate_array_bounds(tokens, array_sizes)
    return render_path(tokens)
