"""Canonical JSON and configuration-path normalization."""

from __future__ import annotations

import ast
import json
import math
import re
from typing import Any, Iterable

_SAFE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


class PathNormalizationError(ValueError):
    pass


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def load_json_bytes(data: bytes) -> Any:
    text = data.decode("utf-8")
    return json.loads(text, parse_constant=_reject_json_constant)


def validate_json_value(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite numbers are not allowed")
    if isinstance(value, list):
        for item in value:
            validate_json_value(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            validate_json_value(item)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError("value is not a JSON scalar/container")


def canonical_json_text(value: Any) -> str:
    validate_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_json_bytes(value: Any) -> bytes:
    return canonical_json_text(value).encode("utf-8")


def _format_key(component: str) -> str:
    if _SAFE_KEY.fullmatch(component):
        return component
    return "[" + json.dumps(component, ensure_ascii=False) + "]"


def _parse_bracket(content: str) -> tuple[str, str | int]:
    content = content.strip()
    if not content:
        raise PathNormalizationError("empty array index or quoted key")
    if content.isdigit():
        return "index", int(content)
    if (content.startswith('"') and content.endswith('"')) or (
        content.startswith("'") and content.endswith("'")
    ):
        try:
            key = json.loads(content) if content[0] == '"' else ast.literal_eval(content)
        except (ValueError, SyntaxError, json.JSONDecodeError) as exc:
            raise PathNormalizationError("invalid quoted path component") from exc
        if not isinstance(key, str) or not key:
            raise PathNormalizationError("bracket key must be a non-empty string")
        return "key", key
    raise PathNormalizationError("array index must be a non-negative integer")


def _parse_path_text(raw: str) -> list[tuple[str, str | int]]:
    if not raw or raw != raw.strip():
        raise PathNormalizationError("path must be non-empty and have no outer whitespace")
    components: list[tuple[str, str | int]] = []
    index = 0
    expecting_component = True
    previous_was_bracket = False

    while index < len(raw):
        char = raw[index]
        if char == ".":
            if expecting_component:
                raise PathNormalizationError("empty path component")
            expecting_component = True
            previous_was_bracket = False
            index += 1
            if index == len(raw):
                raise PathNormalizationError("path cannot end with a dot")
            continue

        if char == "[":
            if previous_was_bracket:
                pass
            elif not expecting_component and index == 0:
                pass
            elif expecting_component and components:
                raise PathNormalizationError("array index must follow a component")
            end = raw.find("]", index + 1)
            if end < 0:
                raise PathNormalizationError("unterminated array index")
            kind, component = _parse_bracket(raw[index + 1 : end])
            components.append((kind, component))
            previous_was_bracket = True
            expecting_component = False
            index = end + 1
            if index < len(raw) and raw[index] not in ".[":
                raise PathNormalizationError("path components must be separated")
            continue

        if previous_was_bracket:
            raise PathNormalizationError("path components must be separated")
        if not expecting_component:
            raise PathNormalizationError("path components must be separated")

        start = index
        while index < len(raw) and raw[index] not in ".[":
            if raw[index] == "]":
                raise PathNormalizationError("unexpected closing bracket")
            index += 1
        token = raw[start:index]
        if not token:
            raise PathNormalizationError("empty path component")
        if token.isdigit():
            components.append(("index", int(token)))
        else:
            components.append(("key", token))
        previous_was_bracket = False
        expecting_component = False

    if not components:
        raise PathNormalizationError("path has no components")
    return components


def _format_components(components: Iterable[tuple[str, str | int]]) -> str:
    output = ""
    for kind, component in components:
        if kind == "index":
            output += f"[{component}]"
        elif not output:
            output = _format_key(str(component))
        elif _SAFE_KEY.fullmatch(str(component)):
            output += "." + str(component)
        else:
            output += "[" + json.dumps(str(component), ensure_ascii=False) + "]"
    return output


def normalize_config_path(path: Any) -> str:
    """Normalize dot paths and array subscripts to one canonical key form."""
    if isinstance(path, (list, tuple)):
        components: list[tuple[str, str | int]] = []
        for item in path:
            if isinstance(item, bool):
                raise PathNormalizationError("boolean path components are invalid")
            if isinstance(item, int):
                if item < 0:
                    raise PathNormalizationError("array indexes must be non-negative")
                components.append(("index", item))
            elif isinstance(item, str) and item:
                components.append(("key", item))
            else:
                raise PathNormalizationError("path component is invalid")
        if not components:
            raise PathNormalizationError("path has no components")
        return _format_components(components)
    if not isinstance(path, str):
        raise PathNormalizationError("path must be a string or component list")
    return _format_components(_parse_path_text(path))


def observation_digest_payload(normalized_path: str, value: Any) -> bytes:
    """Canonical bytes authenticated by sha256/hmac-sha256 evidence digests."""
    return canonical_json_bytes({"path": normalized_path, "value": value})
