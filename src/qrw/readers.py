from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .errors import (
    BusinessError,
    E_DUP_QUERY,
    E_FORMAT_CORRUPT,
    E_PATTERN_EMPTY,
    E_VERSION_UNSUPPORTED,
)
from .models import (
    CompilerConfig,
    FillRecord,
    ParsedRule,
    ParsedTemplateSet,
    SlotDeclaration,
    SourceLocation,
)
from .placeholders import placeholder_names, scan_placeholders


_SLOT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TYPE_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_KNOWN_HEADER_RE = re.compile(r"^#V(\d+)$")


def _read_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BusinessError(
            E_FORMAT_CORRUPT,
            "file is not valid UTF-8",
            line=1,
            field="encoding",
            column=exc.start + 1,
            details={"source": str(path)},
        ) from exc
    except OSError as exc:
        raise BusinessError(
            E_FORMAT_CORRUPT,
            f"cannot read file: {exc}",
            line=1,
            field="file",
            details={"source": str(path)},
        ) from exc


def load_slot_types(path: Path) -> dict[str, Any]:
    text = _read_utf8(path)
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BusinessError(
            E_FORMAT_CORRUPT,
            "slot type file is not valid JSON",
            line=exc.lineno,
            field="slot_types",
            column=exc.colno,
            details={"source": str(path), "reason": exc.msg},
        ) from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("types"), dict):
        raise BusinessError(
            E_FORMAT_CORRUPT,
            "slot type file must contain an object field 'types'",
            line=1,
            field="types",
            column=1,
            details={"source": str(path)},
        )
    normalized: dict[str, list[str]] = {}
    for type_name, config in raw["types"].items():
        if not isinstance(type_name, str) or not _TYPE_NAME_RE.fullmatch(type_name):
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "slot type names must match [A-Z][A-Z0-9_]*",
                line=1,
                field="types",
                column=1,
                details={"source": str(path), "type": type_name},
            )
        values = config.get("values") if isinstance(config, dict) else config
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise BusinessError(
                E_FORMAT_CORRUPT,
                f"slot type {type_name} must contain a string list 'values'",
                line=1,
                field=f"types.{type_name}.values",
                column=1,
                details={"source": str(path)},
            )
        if len(set(values)) != len(values):
            raise BusinessError(
                E_FORMAT_CORRUPT,
                f"slot type {type_name} contains duplicate values",
                line=1,
                field=f"types.{type_name}.values",
                column=1,
                details={"source": str(path)},
            )
        normalized[type_name] = list(values)
    return {
        "version": str(raw.get("version", "1")),
        "types": normalized,
    }


def read_queries(path: Path, allowed_versions: set[str] | None = None) -> list[dict[str, Any]]:
    text = _read_utf8(path)
    queries: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "query line is not valid JSON",
                line=line_no,
                field="query",
                column=exc.colno,
                details={"source": str(path), "reason": exc.msg},
            ) from exc
        if not isinstance(item, dict):
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "query line must be a JSON object",
                line=line_no,
                field="query",
                column=1,
                details={"source": str(path)},
            )
        for field_name in ("query_id", "query_text", "locale", "template_set_version"):
            if field_name not in item:
                raise BusinessError(
                    E_FORMAT_CORRUPT,
                    f"query is missing required field {field_name}",
                    line=line_no,
                    field=field_name,
                    column=1,
                    details={"source": str(path)},
                )
        if not isinstance(item["query_id"], str) or not item["query_id"]:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "query_id must be a non-empty string",
                line=line_no,
                field="query_id",
                column=1,
                details={"source": str(path)},
            )
        if not isinstance(item["query_text"], str):
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "query_text must be a string",
                line=line_no,
                field="query_text",
                column=1,
                details={"source": str(path)},
            )
        if not isinstance(item["locale"], str):
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "locale must be a string",
                line=line_no,
                field="locale",
                column=1,
                details={"source": str(path)},
            )
        version = str(item["template_set_version"])
        if allowed_versions is not None and version not in allowed_versions:
            raise BusinessError(
                E_VERSION_UNSUPPORTED,
                f"query references unsupported template set version {version}",
                line=line_no,
                field="template_set_version",
                column=1,
                details={"source": str(path), "version": version},
            )
        query_id = item["query_id"]
        if query_id in seen:
            raise BusinessError(
                E_DUP_QUERY,
                f"duplicate query_id {query_id}",
                line=line_no,
                field="query_id",
                column=1,
                details={"source": str(path), "first_line": seen[query_id]},
            )
        seen[query_id] = line_no
        normalized = dict(item)
        normalized["template_set_version"] = version
        queries.append(normalized)
    return queries


def detect_dialect(text: str, source_name: str = "<memory>") -> str:
    lines = text.splitlines()
    first_line = 1
    first = ""
    for line_no, line in enumerate(lines, start=1):
        if line.strip():
            first_line = line_no
            first = line.strip()
            break
    if not first:
        raise BusinessError(
            E_FORMAT_CORRUPT,
            "template source is empty",
            line=1,
            field="header",
            column=1,
            details={"source": source_name},
        )
    if first.startswith("#"):
        if first == "#V1":
            return "v1"
        if first == "#V2":
            return "v2"
        match = _KNOWN_HEADER_RE.fullmatch(first)
        if match:
            raise BusinessError(
                E_VERSION_UNSUPPORTED,
                f"unsupported template version header {first}",
                line=first_line,
                field="header",
                column=1,
                details={"source": source_name, "version": first[2:]},
            )
        raise BusinessError(
            E_FORMAT_CORRUPT,
            "truncated or malformed version header",
            line=first_line,
            field="header",
            column=len(first) + 1,
            details={"source": source_name, "header": first},
        )
    if first.startswith("{"):
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "template JSON is malformed",
                line=exc.lineno,
                field="json",
                column=exc.colno,
                details={"source": source_name, "reason": exc.msg},
            ) from exc
        version = raw.get("version") if isinstance(raw, dict) else None
        if version is not None and str(version).upper() != "V3":
            raise BusinessError(
                E_VERSION_UNSUPPORTED,
                f"unsupported template version {version}",
                line=1,
                field="version",
                column=1,
                details={"source": source_name, "version": version},
            )
        if isinstance(raw, dict) and "rules" in raw:
            return "v3"
        raise BusinessError(
            E_FORMAT_CORRUPT,
            "JSON template source does not contain a 'rules' field",
            line=1,
            field="rules",
            column=1,
            details={"source": source_name},
        )
    raise BusinessError(
        E_FORMAT_CORRUPT,
        "missing version header or structured field set",
        line=first_line,
        field="header",
        column=1,
        details={"source": source_name},
    )


def read_template_file(
    path: Path,
    slot_types: dict[str, Any],
    config: CompilerConfig,
) -> ParsedTemplateSet:
    text = _read_utf8(path)
    return read_template_text(text, str(path), slot_types, config)


def read_template_text(
    text: str,
    source_name: str,
    slot_types: dict[str, Any],
    config: CompilerConfig,
) -> ParsedTemplateSet:
    dialect = detect_dialect(text, source_name)
    if dialect == "v1":
        return _read_v1(text, source_name, slot_types, config)
    if dialect == "v2":
        return _read_v2(text, source_name, slot_types, config)
    return _read_v3(text, source_name, slot_types, config)


def _header_line(text: str) -> int:
    for index, line in enumerate(text.splitlines(), start=1):
        if line.strip():
            return index
    return 1


def _read_v1(
    text: str,
    source_name: str,
    slot_types: dict[str, Any],
    config: CompilerConfig,
) -> ParsedTemplateSet:
    rules: list[ParsedRule] = []
    fills: list[FillRecord] = []
    header_line = _header_line(text)
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        if line_no == header_line or not raw_line.strip():
            continue
        fields = raw_line.split("|")
        if len(fields) != 3:
            if len(fields) < 3:
                column = len(raw_line) + 1
                segment = len(fields) + 1
            else:
                column = sum(len(item) for item in fields[:3]) + 4
                segment = 4
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "v1 rule must contain exactly three pipe-separated fields",
                line=line_no,
                field=f"segment[{segment}]",
                column=column,
                details={
                    "source": source_name,
                    "expected_fields": ["pattern", "replacement", "order"],
                    "actual_count": len(fields),
                },
            )
        pattern, replacement, order_text = fields
        if pattern == "":
            raise BusinessError(
                E_PATTERN_EMPTY,
                "v1 rule pattern is empty",
                line=line_no,
                field="pattern",
                column=1,
                details={"source": source_name},
            )
        try:
            order = int(order_text.strip())
        except ValueError as exc:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "v1 order must be an integer",
                line=line_no,
                field="order",
                column=len(pattern) + len(replacement) + 3,
                details={"source": source_name, "value": order_text},
            ) from exc
        if order < 0:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "v1 order must be non-negative",
                line=line_no,
                field="order",
                column=len(pattern) + len(replacement) + 3,
                details={"source": source_name, "value": order},
            )
        rule_id = f"v1_r{line_no}"
        pattern_spans = scan_placeholders(
            pattern,
            dialect="v1",
            source_name=source_name,
            line=line_no,
            field="pattern",
        )
        replacement_spans = scan_placeholders(
            replacement,
            dialect="v1",
            source_name=source_name,
            line=line_no,
            field="replacement",
        )
        inferred_names = placeholder_names([*pattern_spans, *replacement_spans])
        slots: list[SlotDeclaration] = []
        for name in inferred_names:
            slot_type = config.inferred_slot_type(name)
            if slot_type is None:
                raise BusinessError(
                    E_FORMAT_CORRUPT,
                    f"v1 placeholder {name} cannot be mapped to a controlled slot type",
                    line=line_no,
                    field=f"pattern.{{{name}}}",
                    column=1,
                    details={"source": source_name, "slot": name},
                )
            slots.append(
                SlotDeclaration(
                    name=name,
                    slot_type=slot_type,
                    source=SourceLocation("v1", source_name, line_no, 1, f"slot.{name}"),
                    declared=False,
                    origin="inferred_from_placeholder_and_slot_name_types",
                )
            )
            fill = FillRecord(
                rule_id=rule_id,
                source_line=line_no,
                field=f"slots.{name}.type",
                default={"name": name, "type": slot_type},
                origin="inferred_from_placeholder_and_slot_name_types",
            )
            fills.append(fill)
        for field_name, value in (
            ("weight", config.default_weight),
            ("enabled", config.default_enabled),
            ("max_depth", config.default_max_depth),
        ):
            fills.append(
                FillRecord(
                    rule_id=rule_id,
                    source_line=line_no,
                    field=field_name,
                    default=value,
                    origin="compiler_default",
                )
            )
        rules.append(
            ParsedRule(
                rule_id=rule_id,
                kind="rule",
                pattern_text=pattern,
                replacement_text=replacement,
                replacement_nodes=None,
                slots=slots,
                weight=config.default_weight,
                enabled=config.default_enabled,
                max_depth=config.default_max_depth,
                priority=order,
                source=SourceLocation("v1", source_name, line_no, 1, "rule"),
                fills=[fill for fill in fills if fill.rule_id == rule_id],
            )
        )
    return ParsedTemplateSet(
        dialect="v1",
        source_name=source_name,
        rules=rules,
        slot_types=slot_types,
        raw_text=text,
        fills=fills,
    )


def _split_quoted(line: str, separator: str) -> list[str]:
    parts: list[str] = []
    buffer: list[str] = []
    in_quotes = False
    escaped = False
    for char in line:
        if escaped:
            buffer.append(char)
            escaped = False
            continue
        if char == "\\" and in_quotes:
            buffer.append(char)
            escaped = True
            continue
        if char == '"':
            in_quotes = not in_quotes
            buffer.append(char)
            continue
        if char == separator and not in_quotes:
            parts.append("".join(buffer))
            buffer = []
            continue
        buffer.append(char)
    if in_quotes:
        raise ValueError("unterminated quoted value")
    parts.append("".join(buffer))
    return parts


def _decode_kv_value(value: str) -> str:
    value = value.strip()
    if value.startswith('"'):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid quoted value: {exc.msg}") from exc
        if not isinstance(decoded, str):
            raise ValueError("quoted value must decode to a string")
        return decoded
    return value


def _split_kv_line(line: str, line_no: int, source_name: str) -> dict[str, str]:
    try:
        segments = _split_quoted(line, ";")
    except ValueError as exc:
        raise BusinessError(
            E_FORMAT_CORRUPT,
            "v2 key/value line has unterminated quoted value",
            line=line_no,
            field="line",
            column=len(line) + 1,
            details={"source": source_name},
        ) from exc
    result: dict[str, str] = {}
    for segment in segments:
        if not segment.strip():
            continue
        if "=" not in segment:
            column = max(1, line.find(segment) + 1)
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "v2 rule segment must use key=value syntax",
                line=line_no,
                field=segment.strip(),
                column=column,
                details={"source": source_name},
            )
        key, raw_value = segment.split("=", 1)
        key = key.strip()
        if not key:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "v2 rule contains an empty field name",
                line=line_no,
                field="field_name",
                column=1,
                details={"source": source_name},
            )
        if key in result:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                f"v2 rule contains duplicate field {key}",
                line=line_no,
                field=key,
                column=line.find(key) + 1,
                details={"source": source_name},
            )
        try:
            result[key] = _decode_kv_value(raw_value)
        except ValueError as exc:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                f"v2 field {key} is malformed",
                line=line_no,
                field=key,
                column=line.find(key) + 1,
                details={"source": source_name, "reason": str(exc)},
            ) from exc
    return result


def _parse_bool(value: str, *, line: int, field: str, source: str) -> bool:
    lowered = value.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    raise BusinessError(
        E_FORMAT_CORRUPT,
        f"v2 {field} must be true or false",
        line=line,
        field=field,
        column=1,
        details={"source": source, "value": value},
    )


def _parse_v2_slots(
    raw: str,
    *,
    line: int,
    source_name: str,
    dialect: str = "v2",
) -> list[SlotDeclaration]:
    if raw.strip() in ("", "-", "[]"):
        return []
    slots: list[SlotDeclaration] = []
    seen: set[str] = set()
    for index, item in enumerate(raw.split(",")):
        if ":" not in item:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "slot declaration must use name:type syntax",
                line=line,
                field=f"slots[{index}]",
                column=1,
                details={"source": source_name, "value": item},
            )
        name, slot_type = (part.strip() for part in item.split(":", 1))
        if not _SLOT_NAME_RE.fullmatch(name) or not _TYPE_NAME_RE.fullmatch(slot_type):
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "slot declaration has an invalid name or type",
                line=line,
                field=f"slots[{index}]",
                column=1,
                details={"source": source_name, "value": item},
            )
        if name in seen:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                f"duplicate slot declaration {name}",
                line=line,
                field=f"slots[{index}]",
                column=1,
                details={"source": source_name},
            )
        seen.add(name)
        slots.append(
            SlotDeclaration(
                name=name,
                slot_type=slot_type,
                source=SourceLocation(dialect, source_name, line, 1, f"slots.{name}"),
                declared=True,
                origin="explicit",
            )
        )
    return slots


def _read_v2(
    text: str,
    source_name: str,
    slot_types: dict[str, Any],
    config: CompilerConfig,
) -> ParsedTemplateSet:
    rules: list[ParsedRule] = []
    fills: list[FillRecord] = []
    header_line = _header_line(text)
    seen_ids: dict[str, int] = {}
    required = ("id", "pattern", "replacement", "slots", "order")
    optional = ("weight", "enabled", "max_depth")
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        if line_no == header_line or not raw_line.strip():
            continue
        fields = _split_kv_line(raw_line, line_no, source_name)
        for required_field in required:
            if required_field not in fields:
                raise BusinessError(
                    E_FORMAT_CORRUPT,
                    f"v2 rule is missing required field {required_field}",
                    line=line_no,
                    field=required_field,
                    column=1,
                    details={"source": source_name, "rule_fields": sorted(fields)},
                )
        extra = sorted(set(fields) - set(required) - set(optional))
        if extra:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                f"v2 rule contains unknown field {extra[0]}",
                line=line_no,
                field=extra[0],
                column=raw_line.find(extra[0]) + 1,
                details={"source": source_name, "unknown_fields": extra},
            )
        rule_id = fields["id"]
        if not rule_id:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "v2 rule id is empty",
                line=line_no,
                field="id",
                column=1,
                details={"source": source_name},
            )
        if rule_id in seen_ids:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                f"duplicate v2 rule id {rule_id}",
                line=line_no,
                field="id",
                column=1,
                details={"source": source_name, "first_line": seen_ids[rule_id]},
            )
        seen_ids[rule_id] = line_no
        pattern = fields["pattern"]
        replacement = fields["replacement"]
        if pattern == "":
            raise BusinessError(
                E_PATTERN_EMPTY,
                "v2 rule pattern is empty",
                line=line_no,
                field="pattern",
                column=1,
                details={"source": source_name},
            )
        slots = _parse_v2_slots(fields["slots"], line=line_no, source_name=source_name)
        declared = {slot.name for slot in slots}
        pattern_names = placeholder_names(
            scan_placeholders(
                pattern,
                dialect="v2",
                source_name=source_name,
                line=line_no,
                field="pattern",
            )
        )
        replacement_names = placeholder_names(
            scan_placeholders(
                replacement,
                dialect="v2",
                source_name=source_name,
                line=line_no,
                field="replacement",
            )
        )
        for field_name, names in (("pattern", pattern_names), ("replacement", replacement_names)):
            for name in names:
                if name not in declared:
                    raise BusinessError(
                        E_FORMAT_CORRUPT,
                        f"v2 {field_name} placeholder {name} is not declared in slots",
                        line=line_no,
                        field=f"{field_name}.{{{name}}}",
                        column=1,
                        details={"source": source_name, "slot": name, "declared_slots": sorted(declared)},
                    )
        rule_fills: list[FillRecord] = []
        if "weight" in fields:
            try:
                weight = float(fields["weight"])
            except ValueError as exc:
                raise BusinessError(
                    E_FORMAT_CORRUPT,
                    "v2 weight must be numeric",
                    line=line_no,
                    field="weight",
                    column=1,
                    details={"source": source_name, "value": fields["weight"]},
                ) from exc
        else:
            weight = config.default_weight
            rule_fills.append(
                FillRecord(
                    rule_id=rule_id,
                    source_line=line_no,
                    field="weight",
                    default=weight,
                    origin="compiler_default",
                )
            )
        if "enabled" in fields:
            enabled = _parse_bool(fields["enabled"], line=line_no, field="enabled", source=source_name)
        else:
            enabled = config.default_enabled
            rule_fills.append(
                FillRecord(
                    rule_id=rule_id,
                    source_line=line_no,
                    field="enabled",
                    default=enabled,
                    origin="compiler_default",
                )
            )
        if "max_depth" in fields:
            try:
                max_depth = int(fields["max_depth"])
            except ValueError as exc:
                raise BusinessError(
                    E_FORMAT_CORRUPT,
                    "v2 max_depth must be an integer",
                    line=line_no,
                    field="max_depth",
                    column=1,
                    details={"source": source_name, "value": fields["max_depth"]},
                ) from exc
        else:
            max_depth = config.default_max_depth
            rule_fills.append(
                FillRecord(
                    rule_id=rule_id,
                    source_line=line_no,
                    field="max_depth",
                    default=max_depth,
                    origin="compiler_default",
                )
            )
        try:
            order = int(fields["order"])
        except ValueError as exc:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "v2 order must be an integer",
                line=line_no,
                field="order",
                column=1,
                details={"source": source_name, "value": fields["order"]},
            ) from exc
        if order < 0:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "v2 order must be non-negative",
                line=line_no,
                field="order",
                column=1,
                details={"source": source_name, "value": order},
            )
        fills.extend(rule_fills)
        rules.append(
            ParsedRule(
                rule_id=rule_id,
                kind="rule",
                pattern_text=pattern,
                replacement_text=replacement,
                replacement_nodes=None,
                slots=slots,
                weight=weight,
                enabled=enabled,
                max_depth=max_depth,
                priority=order,
                source=SourceLocation("v2", source_name, line_no, 1, "rule"),
                fills=rule_fills,
            )
        )
    return ParsedTemplateSet(
        dialect="v2",
        source_name=source_name,
        rules=rules,
        slot_types=slot_types,
        raw_text=text,
        fills=fills,
    )


def _json_rule_lines(text: str) -> tuple[dict[str, int], int]:
    id_lines: dict[str, int] = {}
    rules_line = 1
    id_pattern = re.compile(r'"id"\s*:\s*("(?:\\.|[^"\\])*")')
    for line_no, line in enumerate(text.splitlines(), start=1):
        if '"rules"' in line and rules_line == 1:
            rules_line = line_no
        for match in id_pattern.finditer(line):
            try:
                rule_id = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if isinstance(rule_id, str):
                id_lines.setdefault(rule_id, line_no)
    return id_lines, rules_line


def _v3_slots(
    raw_slots: Any,
    *,
    line: int,
    source_name: str,
) -> list[SlotDeclaration]:
    items: list[tuple[str, str]] = []
    if isinstance(raw_slots, dict):
        for name, value in raw_slots.items():
            if isinstance(value, str):
                items.append((name, value))
            elif isinstance(value, dict) and isinstance(value.get("type"), str):
                items.append((name, value["type"]))
            else:
                raise BusinessError(
                    E_FORMAT_CORRUPT,
                    "v3 slot declaration value must be a type string or an object with type",
                    line=line,
                    field=f"slots.{name}",
                    column=1,
                    details={"source": source_name},
                )
    elif isinstance(raw_slots, list):
        for index, item in enumerate(raw_slots):
            if not isinstance(item, dict) or "name" not in item or "type" not in item:
                raise BusinessError(
                    E_FORMAT_CORRUPT,
                    "v3 slot declaration must contain name and type",
                    line=line,
                    field=f"slots[{index}]",
                    column=1,
                    details={"source": source_name},
                )
            items.append((str(item["name"]), str(item["type"])))
    else:
        raise BusinessError(
            E_FORMAT_CORRUPT,
            "v3 slots must be an object or array",
            line=line,
            field="slots",
            column=1,
            details={"source": source_name},
        )
    result: list[SlotDeclaration] = []
    seen: set[str] = set()
    for index, (name, slot_type) in enumerate(items):
        if not _SLOT_NAME_RE.fullmatch(name) or not _TYPE_NAME_RE.fullmatch(slot_type):
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "v3 slot name or type is invalid",
                line=line,
                field=f"slots[{index}]",
                column=1,
                details={"source": source_name, "name": name, "type": slot_type},
            )
        if name in seen:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                f"duplicate v3 slot declaration {name}",
                line=line,
                field=f"slots[{index}]",
                column=1,
                details={"source": source_name},
            )
        seen.add(name)
        result.append(
            SlotDeclaration(
                name=name,
                slot_type=slot_type,
                source=SourceLocation("v3", source_name, line, 1, f"slots.{name}"),
                declared=True,
                origin="explicit",
            )
        )
    return result


def _read_v3(
    text: str,
    source_name: str,
    slot_types: dict[str, Any],
    config: CompilerConfig,
) -> ParsedTemplateSet:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BusinessError(
            E_FORMAT_CORRUPT,
            "v3 template JSON is malformed",
            line=exc.lineno,
            field="json",
            column=exc.colno,
            details={"source": source_name, "reason": exc.msg},
        ) from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("rules"), list):
        raise BusinessError(
            E_FORMAT_CORRUPT,
            "v3 template must contain a rules array",
            line=_json_rule_lines(text)[1],
            field="rules",
            column=1,
            details={"source": source_name},
        )
    version = raw.get("version")
    if version is not None and str(version).upper() != "V3":
        raise BusinessError(
            E_VERSION_UNSUPPORTED,
            f"unsupported template version {version}",
            line=1,
            field="version",
            column=1,
            details={"source": source_name, "version": version},
        )
    line_map, rules_line = _json_rule_lines(text)
    rules: list[ParsedRule] = []
    fills: list[FillRecord] = []
    seen_ids: dict[str, int] = {}
    for index, raw_rule in enumerate(raw["rules"]):
        if not isinstance(raw_rule, dict):
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "v3 rule must be a JSON object",
                line=rules_line,
                field=f"rules[{index}]",
                column=1,
                details={"source": source_name},
            )
        rule_id = raw_rule.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "v3 rule is missing a non-empty id",
                line=line_map.get(str(rule_id), rules_line),
                field=f"rules[{index}].id",
                column=1,
                details={"source": source_name},
            )
        line = line_map.get(rule_id, rules_line)
        if rule_id in seen_ids:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                f"duplicate v3 rule id {rule_id}",
                line=line,
                field=f"rules[{index}].id",
                column=1,
                details={"source": source_name, "first_line": seen_ids[rule_id]},
            )
        seen_ids[rule_id] = line
        kind = raw_rule.get("kind", "rule")
        if kind not in ("rule", "fragment"):
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "v3 rule kind must be 'rule' or 'fragment'",
                line=line,
                field=f"rules[{index}].kind",
                column=1,
                details={"source": source_name, "kind": kind},
            )
        if "replacement" not in raw_rule:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "v3 rule is missing required field replacement",
                line=line,
                field=f"rules[{index}].replacement",
                column=1,
                details={"source": source_name},
            )
        if "slots" not in raw_rule:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "v3 rule is missing required field slots",
                line=line,
                field=f"rules[{index}].slots",
                column=1,
                details={"source": source_name},
            )
        slots = _v3_slots(raw_rule["slots"], line=line, source_name=source_name)
        pattern: str | None
        if kind == "rule":
            if "pattern" not in raw_rule:
                raise BusinessError(
                    E_FORMAT_CORRUPT,
                    "v3 emit rule is missing required field pattern",
                    line=line,
                    field=f"rules[{index}].pattern",
                    column=1,
                    details={"source": source_name},
                )
            if not isinstance(raw_rule["pattern"], str):
                raise BusinessError(
                    E_FORMAT_CORRUPT,
                    "v3 pattern must be a string",
                    line=line,
                    field=f"rules[{index}].pattern",
                    column=1,
                    details={"source": source_name},
                )
            pattern = raw_rule["pattern"]
            if pattern == "":
                raise BusinessError(
                    E_PATTERN_EMPTY,
                    "v3 rule pattern is empty",
                    line=line,
                    field=f"rules[{index}].pattern",
                    column=1,
                    details={"source": source_name},
                )
        else:
            pattern = raw_rule.get("pattern")
            if pattern is not None and not isinstance(pattern, str):
                raise BusinessError(
                    E_FORMAT_CORRUPT,
                    "v3 fragment pattern must be a string when present",
                    line=line,
                    field=f"rules[{index}].pattern",
                    column=1,
                    details={"source": source_name},
                )
        replacement_raw = raw_rule["replacement"]
        replacement_text: str | None = None
        replacement_nodes: list[dict[str, Any]] | None = None
        if isinstance(replacement_raw, str):
            replacement_text = replacement_raw
        elif isinstance(replacement_raw, list):
            if not all(isinstance(item, dict) for item in replacement_raw):
                raise BusinessError(
                    E_FORMAT_CORRUPT,
                    "v3 replacement nodes must be JSON objects",
                    line=line,
                    field=f"rules[{index}].replacement",
                    column=1,
                    details={"source": source_name},
                )
            replacement_nodes = replacement_raw
        else:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "v3 replacement must be a string or node array",
                line=line,
                field=f"rules[{index}].replacement",
                column=1,
                details={"source": source_name},
            )
        declared = {slot.name for slot in slots}
        for field_name, value in (("pattern", pattern), ("replacement", replacement_text)):
            if value is None:
                continue
            names = placeholder_names(
                scan_placeholders(
                    value,
                    dialect="v3",
                    source_name=source_name,
                    line=line,
                    field=f"rules[{index}].{field_name}",
                )
            )
            for name in names:
                if name not in declared:
                    raise BusinessError(
                        E_FORMAT_CORRUPT,
                        f"v3 {field_name} placeholder {name} is not declared in slots",
                        line=line,
                        field=f"rules[{index}].{field_name}.{{{name}}}",
                        column=1,
                        details={"source": source_name, "slot": name, "declared_slots": sorted(declared)},
                    )
        default_values = {
            "weight": config.default_weight,
            "enabled": config.default_enabled,
            "max_depth": config.default_max_depth,
            "priority": int(raw_rule.get("order", 0)),
        }
        rule_fills: list[FillRecord] = []
        weight_raw = raw_rule.get("weight", default_values["weight"])
        enabled_raw = raw_rule.get("enabled", default_values["enabled"])
        depth_raw = raw_rule.get("max_depth", default_values["max_depth"])
        priority_raw = raw_rule.get("priority", default_values["priority"])
        for field_name, present in (
            ("weight", "weight" in raw_rule),
            ("enabled", "enabled" in raw_rule),
            ("max_depth", "max_depth" in raw_rule),
            ("priority", "priority" in raw_rule),
        ):
            if not present:
                rule_fills.append(
                    FillRecord(
                        rule_id=rule_id,
                        source_line=line,
                        field=field_name,
                        default=default_values[field_name],
                        origin="compiler_default",
                    )
                )
        try:
            weight = float(weight_raw)
        except (TypeError, ValueError) as exc:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "v3 weight must be numeric",
                line=line,
                field=f"rules[{index}].weight",
                column=1,
                details={"source": source_name, "value": weight_raw},
            ) from exc
        if not isinstance(enabled_raw, bool):
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "v3 enabled must be boolean",
                line=line,
                field=f"rules[{index}].enabled",
                column=1,
                details={"source": source_name, "value": enabled_raw},
            )
        try:
            max_depth = int(depth_raw)
            priority = int(priority_raw)
        except (TypeError, ValueError) as exc:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "v3 max_depth and priority must be integers",
                line=line,
                field=f"rules[{index}].max_depth",
                column=1,
                details={"source": source_name, "max_depth": depth_raw, "priority": priority_raw},
            ) from exc
        fills.extend(rule_fills)
        rules.append(
            ParsedRule(
                rule_id=rule_id,
                kind=kind,
                pattern_text=pattern,
                replacement_text=replacement_text,
                replacement_nodes=replacement_nodes,
                slots=slots,
                weight=weight,
                enabled=enabled_raw,
                max_depth=max_depth,
                priority=priority,
                source=SourceLocation("v3", source_name, line, 1, "rule"),
                fills=rule_fills,
            )
        )
    return ParsedTemplateSet(
        dialect="v3",
        source_name=source_name,
        rules=rules,
        slot_types=slot_types,
        raw_text=text,
        fills=fills,
    )

