"""Readers for the three controlled entity type-set formats."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import (
    E_TYPE_SET_CORRUPT,
    E_VERSION_UNSUPPORTED,
    fail,
)
from .models import AliasDefinition, TypeDefinition, TypeSet, is_valid_name

FORMAT_V1 = "v1"
FORMAT_V2 = "v2"
FORMAT_V3 = "v3"
SUPPORTED_FORMATS = (FORMAT_V1, FORMAT_V2, FORMAT_V3)
HEADER_NAME = "#NER_TYPE_SET"

_TRUE_VALUES = {"true", "1", "yes", "y"}
_FALSE_VALUES = {"false", "0", "no", "n"}


def _parse_bool(value: str, *, field: str, line: int) -> bool:
    raw = value.strip()
    if not raw:
        fail(
            E_TYPE_SET_CORRUPT,
            f"missing boolean value for {field}",
            field=field,
            line=line,
            reason="required_field_missing",
        )
    lowered = raw.lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    fail(
        E_TYPE_SET_CORRUPT,
        f"cannot parse boolean value {value!r} for {field}",
        field=field,
        line=line,
        reason="unparseable_enabled_marker",
        value=value,
    )
    raise AssertionError("unreachable")


def _validate_type_name(name: str, *, field: str, line: int) -> None:
    if not name:
        fail(
            E_TYPE_SET_CORRUPT,
            f"missing name for {field}",
            field=field,
            line=line,
            reason="required_field_missing",
        )
    if not is_valid_name(name):
        fail(
            E_TYPE_SET_CORRUPT,
            f"invalid entity type name {name!r}",
            field=field,
            line=line,
            reason="invalid_name",
            value=name,
        )


def _ensure_unique_names(
    types: list[TypeDefinition],
    *,
    alias_lines: dict[str, int] | None = None,
) -> None:
    type_names: set[str] = set()
    alias_names: dict[str, int | None] = {}
    for item in types:
        if item.name in type_names:
            fail(
                E_TYPE_SET_CORRUPT,
                f"duplicate entity type {item.name!r}",
                field="name",
                line=item.source_line,
                reason="duplicate_type",
                value=item.name,
            )
        type_names.add(item.name)
        for alias in item.aliases:
            if alias.name in alias_names:
                fail(
                    E_TYPE_SET_CORRUPT,
                    f"duplicate alias {alias.name!r}",
                    field="alias",
                    line=alias.source_line,
                    reason="duplicate_alias",
                    value=alias.name,
                )
            alias_names[alias.name] = alias.source_line
    for alias_name in sorted(alias_names):
        if alias_name in type_names:
            fail(
                E_TYPE_SET_CORRUPT,
                f"alias {alias_name!r} collides with an entity type",
                field="alias",
                line=alias_names[alias_name],
                reason="alias_type_collision",
                value=alias_name,
            )
    for item in types:
        for alias in item.aliases:
            if alias.target not in type_names:
                fail(
                    E_TYPE_SET_CORRUPT,
                    f"alias {alias.name!r} targets unknown type {alias.target!r}",
                    field="target",
                    line=alias.source_line,
                    reason="alias_target_missing",
                    value=alias.target,
                )


def _canonicalize(
    *,
    format_version: str,
    label_set_version: str,
    types: list[TypeDefinition],
    source_name: str | None,
    completions: list[dict[str, Any]] | None = None,
) -> TypeSet:
    for item in types:
        item.aliases.sort(key=lambda alias: alias.name)
    types.sort(key=lambda item: item.name)
    _ensure_unique_names(types)
    return TypeSet(
        format_version=format_version,
        label_set_version=label_set_version,
        types=types,
        source_name=source_name,
        completions=completions or [],
    )


def _read_text(path: str | Path) -> tuple[str, str]:
    source = str(path)
    try:
        return Path(path).read_text(encoding="utf-8"), source
    except OSError as exc:
        fail(
            E_TYPE_SET_CORRUPT,
            f"cannot read type set: {exc}",
            field="path",
            reason="read_error",
            source=source,
        )
        raise AssertionError("unreachable")


def _json_key_lines(text: str, key: str) -> list[int]:
    needle = f'"{key}"'
    result: list[int] = []
    offset = 0
    while True:
        index = text.find(needle, offset)
        if index < 0:
            break
        result.append(text.count("\n", 0, index) + 1)
        offset = index + len(needle)
    return result


def _parse_json_document(text: str, source_name: str | None) -> TypeSet:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        fail(
            E_TYPE_SET_CORRUPT,
            f"invalid JSON type set: {exc.msg}",
            field="$",
            line=exc.lineno,
            reason="invalid_json",
            column=exc.colno,
        )
        raise AssertionError("unreachable")
    if not isinstance(data, dict):
        fail(
            E_TYPE_SET_CORRUPT,
            "v3 type set must be a JSON object",
            field="$",
            line=1,
            reason="invalid_root_type",
        )
    raw_format = data.get("format")
    if raw_format is None:
        if "types" not in data:
            fail(
                E_VERSION_UNSUPPORTED,
                "JSON type set has neither a format marker nor a types field",
                field="format",
                line=1,
            )
        version = FORMAT_V3
    elif raw_format in ("NER_TYPE_SET_V3", FORMAT_V3):
        version = FORMAT_V3
    else:
        fail(
            E_VERSION_UNSUPPORTED,
            f"unsupported type-set version {raw_format!r}",
            field="format",
            line=_json_key_lines(text, "format")[0] if _json_key_lines(text, "format") else 1,
            version=raw_format,
        )
        raise AssertionError("unreachable")

    allowed_root = {"format", "label_set_version", "types"}
    unknown_root = sorted(set(data) - allowed_root)
    if unknown_root:
        fail(
            E_TYPE_SET_CORRUPT,
            f"unknown v3 root field {unknown_root[0]!r}",
            field=unknown_root[0],
            line=(
                _json_key_lines(text, unknown_root[0])[0]
                if _json_key_lines(text, unknown_root[0])
                else 1
            ),
            reason="unknown_field",
        )
    if "label_set_version" not in data:
        fail(
            E_TYPE_SET_CORRUPT,
            "missing required field label_set_version",
            field="label_set_version",
            line=1,
            reason="required_field_missing",
        )
    label_version = data["label_set_version"]
    if not isinstance(label_version, str) or not label_version.strip():
        fail(
            E_TYPE_SET_CORRUPT,
            "label_set_version must be a non-empty string",
            field="label_set_version",
            line=(
                _json_key_lines(text, "label_set_version")[0]
                if _json_key_lines(text, "label_set_version")
                else 1
            ),
            reason="invalid_field",
        )
    raw_types = data.get("types")
    if not isinstance(raw_types, list) or not raw_types:
        fail(
            E_TYPE_SET_CORRUPT,
            "types must be a non-empty array",
            field="types",
            line=_json_key_lines(text, "types")[0] if _json_key_lines(text, "types") else 1,
            reason="required_field_missing",
        )

    type_name_lines = _json_key_lines(text, "name")
    name_cursor = 0
    types: list[TypeDefinition] = []
    for type_index, raw_type in enumerate(raw_types):
        if not isinstance(raw_type, dict):
            fail(
                E_TYPE_SET_CORRUPT,
                "each types entry must be an object",
                field=f"types[{type_index}]",
                line=_json_key_lines(text, "types")[0] if _json_key_lines(text, "types") else 1,
                reason="invalid_field",
            )
        allowed_type = {"name", "bio_prefix_required", "enabled", "aliases"}
        unknown_type = sorted(set(raw_type) - allowed_type)
        type_line = (
            type_name_lines[name_cursor]
            if name_cursor < len(type_name_lines)
            else 1
        )
        if unknown_type:
            fail(
                E_TYPE_SET_CORRUPT,
                f"unknown type field {unknown_type[0]!r}",
                field=f"types[{type_index}].{unknown_type[0]}",
                line=type_line,
                reason="unknown_field",
            )
        for required in ("name", "bio_prefix_required", "enabled"):
            if required not in raw_type:
                fail(
                    E_TYPE_SET_CORRUPT,
                    f"missing required type field {required}",
                    field=f"types[{type_index}].{required}",
                    line=type_line,
                    reason="required_field_missing",
                )
        name = raw_type["name"]
        if not isinstance(name, str):
            fail(
                E_TYPE_SET_CORRUPT,
                "type name must be a string",
                field=f"types[{type_index}].name",
                line=type_line,
                reason="invalid_field",
            )
        _validate_type_name(name, field=f"types[{type_index}].name", line=type_line)
        name_cursor += 1
        bio = raw_type["bio_prefix_required"]
        enabled = raw_type["enabled"]
        if not isinstance(bio, bool):
            fail(
                E_TYPE_SET_CORRUPT,
                "bio_prefix_required must be a JSON boolean",
                field=f"types[{type_index}].bio_prefix_required",
                line=type_line,
                reason="unparseable_bio_prefix",
            )
        if not isinstance(enabled, bool):
            fail(
                E_TYPE_SET_CORRUPT,
                "enabled must be a JSON boolean",
                field=f"types[{type_index}].enabled",
                line=type_line,
                reason="unparseable_enabled_marker",
            )
        raw_aliases = raw_type.get("aliases", [])
        if not isinstance(raw_aliases, list):
            fail(
                E_TYPE_SET_CORRUPT,
                "aliases must be an array",
                field=f"types[{type_index}].aliases",
                line=type_line,
                reason="invalid_field",
            )
        aliases: list[AliasDefinition] = []
        for alias_index, raw_alias in enumerate(raw_aliases):
            if not isinstance(raw_alias, dict):
                fail(
                    E_TYPE_SET_CORRUPT,
                    "each alias entry must be an object",
                    field=f"types[{type_index}].aliases[{alias_index}]",
                    line=type_line,
                    reason="invalid_field",
                )
            alias_line = (
                type_name_lines[name_cursor]
                if name_cursor < len(type_name_lines)
                else type_line
            )
            allowed_alias = {"name", "target", "enabled", "metadata"}
            unknown_alias = sorted(set(raw_alias) - allowed_alias)
            if unknown_alias:
                fail(
                    E_TYPE_SET_CORRUPT,
                    f"unknown alias field {unknown_alias[0]!r}",
                    field=f"types[{type_index}].aliases[{alias_index}].{unknown_alias[0]}",
                    line=alias_line,
                    reason="unknown_field",
                )
            for required in ("name", "target", "enabled"):
                if required not in raw_alias:
                    fail(
                        E_TYPE_SET_CORRUPT,
                        f"missing required alias field {required}",
                        field=f"types[{type_index}].aliases[{alias_index}].{required}",
                        line=alias_line,
                        reason="required_field_missing",
                    )
            alias_name = raw_alias["name"]
            target = raw_alias["target"]
            alias_enabled = raw_alias["enabled"]
            metadata = raw_alias.get("metadata", {})
            if not isinstance(alias_name, str):
                fail(
                    E_TYPE_SET_CORRUPT,
                    "alias name must be a string",
                    field=f"types[{type_index}].aliases[{alias_index}].name",
                    line=alias_line,
                    reason="invalid_field",
                )
            _validate_type_name(
                alias_name,
                field=f"types[{type_index}].aliases[{alias_index}].name",
                line=alias_line,
            )
            name_cursor += 1
            if not isinstance(target, str):
                fail(
                    E_TYPE_SET_CORRUPT,
                    "alias target must be a string",
                    field=f"types[{type_index}].aliases[{alias_index}].target",
                    line=alias_line,
                    reason="invalid_field",
                )
            _validate_type_name(
                target,
                field=f"types[{type_index}].aliases[{alias_index}].target",
                line=alias_line,
            )
            if not isinstance(alias_enabled, bool):
                fail(
                    E_TYPE_SET_CORRUPT,
                    "alias enabled must be a JSON boolean",
                    field=f"types[{type_index}].aliases[{alias_index}].enabled",
                    line=alias_line,
                    reason="unparseable_enabled_marker",
                )
            if not isinstance(metadata, dict):
                fail(
                    E_TYPE_SET_CORRUPT,
                    "alias metadata must be an object",
                    field=f"types[{type_index}].aliases[{alias_index}].metadata",
                    line=alias_line,
                    reason="invalid_field",
                )
            aliases.append(
                AliasDefinition(
                    name=alias_name,
                    target=target,
                    enabled=alias_enabled,
                    metadata=metadata,
                    source_line=alias_line,
                )
            )
        types.append(
            TypeDefinition(
                name=name,
                bio_prefix_required=bio,
                enabled=enabled,
                aliases=aliases,
                source_line=type_line,
            )
        )
    return _canonicalize(
        format_version=version,
        label_set_version=label_version,
        types=types,
        source_name=source_name,
    )


def _parse_header(line: str) -> tuple[str, str]:
    stripped = line.strip()
    if not stripped.startswith(HEADER_NAME):
        fail(
            E_VERSION_UNSUPPORTED,
            "type-set format cannot be determined",
            field="header",
            line=1,
            reason="unknown_format",
        )
    parts = stripped.split("|")
    if parts[0].strip() != HEADER_NAME:
        fail(
            E_VERSION_UNSUPPORTED,
            "type-set header marker is not recognized",
            field="header",
            line=1,
            reason="unknown_format",
        )
    if len(parts) < 3 or not parts[2].strip():
        fail(
            E_TYPE_SET_CORRUPT,
            "truncated NER_TYPE_SET header",
            field="header",
            line=1,
            reason="header_truncated",
        )
    if len(parts) != 3:
        fail(
            E_TYPE_SET_CORRUPT,
            "NER_TYPE_SET header must contain exactly three pipe-separated fields",
            field="header",
            line=1,
            reason="header_field_count",
            actual=len(parts),
            expected=3,
        )
    version = parts[1].strip()
    if version not in SUPPORTED_FORMATS:
        fail(
            E_VERSION_UNSUPPORTED,
            f"unsupported type-set version {version!r}",
            field="version",
            line=1,
            version=version,
        )
    return version, parts[2].strip()


def _parse_v1(
    lines: list[str],
    *,
    header_label_version: str | None,
    header_line_index: int | None,
    source_name: str | None,
) -> TypeSet:
    label_version = header_label_version
    types: list[TypeDefinition] = []
    aliases_pending: list[AliasDefinition] = []
    completions: list[dict[str, Any]] = []
    start = (
        header_line_index + 1
        if header_line_index is not None
        else 0
    )
    for index in range(start, len(lines)):
        line_no = index + 1
        raw = lines[index].strip()
        if not raw or raw.startswith("#"):
            continue
        fields = raw.split("|")
        if len(fields) != 3:
            fail(
                E_TYPE_SET_CORRUPT,
                "v1 row must contain exactly three pipe-separated fields",
                field="columns",
                line=line_no,
                reason="pipe_field_count",
                actual=len(fields),
                expected=3,
            )
        kind, first, second = (part.strip() for part in fields)
        kind = kind.upper()
        if kind == "META":
            if first != "label_set_version":
                fail(
                    E_TYPE_SET_CORRUPT,
                    f"unknown v1 metadata key {first!r}",
                    field="META",
                    line=line_no,
                    reason="unknown_metadata",
                )
            if header_label_version is not None:
                fail(
                    E_TYPE_SET_CORRUPT,
                    "label_set_version is declared by both header and META row",
                    field="label_set_version",
                    line=line_no,
                    reason="duplicate_field",
                )
            if not second:
                fail(
                    E_TYPE_SET_CORRUPT,
                    "missing label_set_version value",
                    field="label_set_version",
                    line=line_no,
                    reason="required_field_missing",
                )
            if label_version is not None and label_version != second:
                fail(
                    E_TYPE_SET_CORRUPT,
                    "conflicting label_set_version values",
                    field="label_set_version",
                    line=line_no,
                    reason="conflicting_value",
                )
            label_version = second
        elif kind == "TYPE":
            _validate_type_name(first, field="name", line=line_no)
            enabled = _parse_bool(second, field="enabled", line=line_no)
            types.append(
                TypeDefinition(
                    name=first,
                    bio_prefix_required=True,
                    enabled=enabled,
                    source_line=line_no,
                )
            )
            completions.append(
                {
                    "item": f"types[{first}].bio_prefix_required",
                    "value": True,
                    "source": "v1_implicit_default",
                    "source_line": line_no,
                }
            )
        elif kind == "ALIAS":
            _validate_type_name(first, field="name", line=line_no)
            _validate_type_name(second, field="target", line=line_no)
            alias = AliasDefinition(
                name=first,
                target=second,
                enabled=True,
                metadata={},
                source_line=line_no,
            )
            aliases_pending.append(alias)
            completions.append(
                {
                    "item": f"aliases[{first}].enabled",
                    "value": True,
                    "source": "v1_implicit_default",
                    "source_line": line_no,
                }
            )
        else:
            fail(
                E_TYPE_SET_CORRUPT,
                f"unknown v1 row kind {kind!r}",
                field="kind",
                line=line_no,
                reason="unknown_row_kind",
                value=kind,
            )
    if not label_version:
        fail(
            E_VERSION_UNSUPPORTED,
            "v1 type set does not declare label_set_version",
            field="label_set_version",
            line=1,
            reason="missing_format_header",
        )
    if not types:
        fail(
            E_TYPE_SET_CORRUPT,
            "v1 type set contains no TYPE rows",
            field="types",
            line=1,
            reason="required_field_missing",
        )
    by_name = {item.name: item for item in types}
    for alias in aliases_pending:
        if alias.target not in by_name:
            fail(
                E_TYPE_SET_CORRUPT,
                f"alias {alias.name!r} targets unknown type {alias.target!r}",
                field="target",
                line=alias.source_line,
                reason="alias_target_missing",
                value=alias.target,
            )
        by_name[alias.target].aliases.append(alias)
    return _canonicalize(
        format_version=FORMAT_V1,
        label_set_version=label_version,
        types=types,
        source_name=source_name,
        completions=completions,
    )


def _parse_v2(
    lines: list[str],
    *,
    header_label_version: str | None,
    header_line_index: int | None,
    source_name: str | None,
) -> TypeSet:
    label_version = header_label_version
    start = (
        header_line_index + 1
        if header_line_index is not None
        else 0
    )
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def finish_current() -> None:
        nonlocal current
        if current is None:
            return
        type_line = current["line"]
        for required in ("bio_prefix_required", "enabled"):
            if required not in current:
                fail(
                    E_TYPE_SET_CORRUPT,
                    f"missing required v2 field {required}",
                    field=required,
                    line=type_line,
                    reason="required_field_missing",
                    type=current["name"],
                )
        blocks.append(current)
        current = None

    for index in range(start, len(lines)):
        line_no = index + 1
        raw = lines[index].strip()
        if not raw or raw.startswith("#"):
            continue
        if "=" not in raw:
            fail(
                E_TYPE_SET_CORRUPT,
                "v2 row must be key=value",
                field="line",
                line=line_no,
                reason="missing_key_value_separator",
            )
        key, value = (part.strip() for part in raw.split("=", 1))
        if key == "label_set_version":
            if header_label_version is not None:
                fail(
                    E_TYPE_SET_CORRUPT,
                    "label_set_version is declared by both header and body",
                    field="label_set_version",
                    line=line_no,
                    reason="duplicate_field",
                )
            if not value:
                fail(
                    E_TYPE_SET_CORRUPT,
                    "missing label_set_version value",
                    field="label_set_version",
                    line=line_no,
                    reason="required_field_missing",
                )
            if label_version is not None and label_version != value:
                fail(
                    E_TYPE_SET_CORRUPT,
                    "conflicting label_set_version values",
                    field="label_set_version",
                    line=line_no,
                    reason="conflicting_value",
                )
            label_version = value
            continue
        if key == "type":
            finish_current()
            if not value:
                fail(
                    E_TYPE_SET_CORRUPT,
                    "missing type name",
                    field="type",
                    line=line_no,
                    reason="required_field_missing",
                )
            _validate_type_name(value, field="type", line=line_no)
            if any(block["name"] == value for block in blocks):
                fail(
                    E_TYPE_SET_CORRUPT,
                    f"duplicate entity type {value!r}",
                    field="type",
                    line=line_no,
                    reason="duplicate_type",
                    value=value,
                )
            current = {"name": value, "line": line_no, "aliases": []}
            continue
        if current is None:
            fail(
                E_TYPE_SET_CORRUPT,
                f"v2 field {key!r} appears before any type block",
                field=key,
                line=line_no,
                reason="orphan_field",
            )
        if key in ("bio_prefix_required", "enabled"):
            if key in current:
                fail(
                    E_TYPE_SET_CORRUPT,
                    f"duplicate v2 field {key!r}",
                    field=key,
                    line=line_no,
                    reason="duplicate_field",
                )
            current[key] = _parse_bool(value, field=key, line=line_no)
        elif key == "alias":
            parts = value.split(">")
            if len(parts) != 3:
                fail(
                    E_TYPE_SET_CORRUPT,
                    "v2 alias must be alias>target>enabled",
                    field="alias",
                    line=line_no,
                    reason="alias_field_count",
                    actual=len(parts),
                    expected=3,
                )
            alias_name, target, enabled_raw = (part.strip() for part in parts)
            _validate_type_name(alias_name, field="alias", line=line_no)
            _validate_type_name(target, field="target", line=line_no)
            enabled = _parse_bool(enabled_raw, field="alias.enabled", line=line_no)
            current["aliases"].append(
                AliasDefinition(
                    name=alias_name,
                    target=target,
                    enabled=enabled,
                    metadata={},
                    source_line=line_no,
                )
            )
        else:
            fail(
                E_TYPE_SET_CORRUPT,
                f"unknown v2 field {key!r}",
                field=key,
                line=line_no,
                reason="unknown_field",
            )
    finish_current()
    if not label_version:
        fail(
            E_VERSION_UNSUPPORTED,
            "v2 type set does not declare label_set_version",
            field="label_set_version",
            line=1,
            reason="missing_format_header",
        )
    if not blocks:
        fail(
            E_TYPE_SET_CORRUPT,
            "v2 type set contains no type blocks",
            field="types",
            line=1,
            reason="required_field_missing",
        )
    types = [
        TypeDefinition(
            name=block["name"],
            bio_prefix_required=block["bio_prefix_required"],
            enabled=block["enabled"],
            aliases=block["aliases"],
            source_line=block["line"],
        )
        for block in blocks
    ]
    return _canonicalize(
        format_version=FORMAT_V2,
        label_set_version=label_version,
        types=types,
        source_name=source_name,
    )


def read_type_set_text(text: str, *, source_name: str | None = None) -> TypeSet:
    """Parse one type set without ever returning a partially parsed result."""

    if text.startswith("\ufeff"):
        text = text[1:]
    stripped = text.strip()
    if not stripped:
        fail(
            E_TYPE_SET_CORRUPT,
            "empty type-set document",
            field="header",
            line=1,
            reason="header_truncated",
        )
    if stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            fail(
                E_TYPE_SET_CORRUPT,
                f"invalid JSON type set: {exc.msg}",
                field="$",
                line=exc.lineno,
                reason="invalid_json",
                column=exc.colno,
            )
        if not isinstance(parsed, dict):
            fail(
                E_TYPE_SET_CORRUPT,
                "v3 type set must be a JSON object",
                field="$",
                line=1,
                reason="invalid_root_type",
            )
        return _parse_json_document(stripped, source_name)

    lines = text.splitlines()
    first_nonempty_index = next(
        (index for index, line in enumerate(lines) if line.strip()),
        None,
    )
    first_nonempty = (
        lines[first_nonempty_index].strip()
        if first_nonempty_index is not None
        else ""
    )
    if first_nonempty.startswith(HEADER_NAME):
        version, label_version = _parse_header(first_nonempty)
        if version == FORMAT_V1:
            return _parse_v1(
                lines,
                header_label_version=label_version,
                header_line_index=first_nonempty_index,
                source_name=source_name,
            )
        if version == FORMAT_V2:
            return _parse_v2(
                lines,
                header_label_version=label_version,
                header_line_index=first_nonempty_index,
                source_name=source_name,
            )
        fail(
            E_VERSION_UNSUPPORTED,
            f"unsupported type-set version {version!r}",
            field="version",
            line=1,
            version=version,
        )

    # Field-set dispatch for headerless documents.
    data_lines = [
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if data_lines and all("=" in line for line in data_lines):
        return _parse_v2(
            lines,
            header_label_version=None,
            header_line_index=None,
            source_name=source_name,
        )
    if data_lines and all("|" in line for line in data_lines):
        first_kinds = {line.split("|", 1)[0].strip().upper() for line in data_lines}
        if first_kinds <= {"TYPE", "ALIAS", "META"}:
            return _parse_v1(
                lines,
                header_label_version=None,
                header_line_index=None,
                source_name=source_name,
            )
    fail(
        E_VERSION_UNSUPPORTED,
        "cannot determine type-set version from header or fields",
        field="header",
        line=1,
        reason="unknown_format",
    )
    raise AssertionError("unreachable")


def load_type_set(path: str | Path) -> TypeSet:
    text, source = _read_text(path)
    return read_type_set_text(text, source_name=source)


def require_type_set(value: TypeSet | str | Path) -> TypeSet:
    if isinstance(value, TypeSet):
        return value
    return load_type_set(value)
