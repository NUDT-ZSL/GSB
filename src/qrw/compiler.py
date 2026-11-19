from __future__ import annotations

from typing import Any

from .canonical import checksum_tag, sha256_hex, walk_nodes
from .errors import (
    BusinessError,
    E_CONFIG_INVALID,
    E_FORMAT_CORRUPT,
)
from .models import (
    CompilerConfig,
    ParsedRule,
    ParsedTemplateSet,
    SlotDeclaration,
    SourceLocation,
)
from .placeholders import scan_placeholders


def _node_id(
    dialect: str,
    source_name: str,
    path: str,
    kind: str,
    line: int,
) -> str:
    raw = f"{dialect}|{source_name}|{path}|{kind}|{line}".encode("utf-8")
    return "n_" + sha256_hex(raw)[:20]


def _source(
    dialect: str,
    source_name: str,
    line: int,
    field: str,
    column: int = 1,
) -> dict[str, Any]:
    return SourceLocation(dialect, source_name, line, column, field).to_dict()


def _sequence(
    *,
    dialect: str,
    source_name: str,
    line: int,
    field: str,
    path: str,
    parts: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "node_id": _node_id(dialect, source_name, path, "sequence", line),
        "kind": "sequence",
        "source": _source(dialect, source_name, line, field),
        "parts": parts,
    }


def _slot_map(slots: list[SlotDeclaration]) -> dict[str, SlotDeclaration]:
    return {slot.name: slot for slot in slots}


def _text_parts(
    text: str,
    *,
    dialect: str,
    source_name: str,
    line: int,
    field: str,
    path: str,
    slots: dict[str, SlotDeclaration],
) -> list[dict[str, Any]]:
    spans = scan_placeholders(
        text,
        dialect=dialect,
        source_name=source_name,
        line=line,
        field=field,
    )
    result: list[dict[str, Any]] = []
    cursor = 0
    part_index = 0
    for span in spans:
        if span.start > cursor:
            literal = text[cursor : span.start]
            part_path = f"{path}.parts[{part_index}]"
            result.append(
                {
                    "node_id": _node_id(dialect, source_name, part_path, "text", line),
                    "kind": "text",
                    "text": literal,
                    "source": _source(dialect, source_name, line, field, cursor + 1),
                }
            )
            part_index += 1
        if span.name not in slots:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                f"placeholder {span.name} has no slot declaration",
                line=line,
                field=field,
                column=span.start + 1,
                details={"dialect": dialect, "source": source_name, "slot": span.name},
            )
        declaration = slots[span.name]
        part_path = f"{path}.parts[{part_index}]"
        result.append(
            {
                "node_id": _node_id(dialect, source_name, part_path, "slot", line),
                "kind": "slot",
                "name": declaration.name,
                "slot_type": declaration.slot_type,
                "declared": declaration.declared,
                "origin": declaration.origin,
                "source": _source(dialect, source_name, line, field, span.start + 1),
            }
        )
        part_index += 1
        cursor = span.end
    if cursor < len(text):
        part_path = f"{path}.parts[{part_index}]"
        result.append(
            {
                "node_id": _node_id(dialect, source_name, part_path, "text", line),
                "kind": "text",
                "text": text[cursor:],
                "source": _source(dialect, source_name, line, field, cursor + 1),
            }
        )
    return result


def _normalize_condition(raw: Any, *, line: int, field: str, source_name: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise BusinessError(
            E_FORMAT_CORRUPT,
            "v3 condition must be an object",
            line=line,
            field=field,
            column=1,
            details={"source": source_name},
        )
    slot = raw.get("slot")
    if not isinstance(slot, str) or not slot:
        raise BusinessError(
            E_FORMAT_CORRUPT,
            "v3 condition is missing slot",
            line=line,
            field=f"{field}.slot",
            column=1,
            details={"source": source_name},
        )
    operator = raw.get("operator")
    value = raw.get("value")
    if "equals" in raw:
        operator = "equals"
        value = raw["equals"]
    if operator is None:
        operator = "equals"
    if operator not in ("equals", "not_equals", "in"):
        raise BusinessError(
            E_FORMAT_CORRUPT,
            "v3 condition operator must be equals, not_equals or in",
            line=line,
            field=f"{field}.operator",
            column=1,
            details={"source": source_name, "operator": operator},
        )
    if operator == "in":
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "v3 'in' condition value must be a string array",
                line=line,
                field=f"{field}.value",
                column=1,
                details={"source": source_name},
            )
    elif not isinstance(value, str):
        raise BusinessError(
            E_FORMAT_CORRUPT,
            "v3 scalar condition value must be a string",
            line=line,
            field=f"{field}.value",
            column=1,
            details={"source": source_name},
        )
    return {"slot": slot, "operator": operator, "value": value}


def _compile_replacement_parts(
    raw_parts: list[dict[str, Any]],
    *,
    dialect: str,
    source_name: str,
    line: int,
    field_path: str,
    path: str,
    slots: dict[str, SlotDeclaration],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_parts):
        item_path = f"{path}.items[{index}]"
        item_field = f"{field_path}[{index}]"
        if "text" in raw:
            if not isinstance(raw["text"], str):
                raise BusinessError(
                    E_FORMAT_CORRUPT,
                    "v3 text node must contain a string",
                    line=line,
                    field=f"{item_field}.text",
                    column=1,
                    details={"source": source_name},
                )
            result.extend(
                _text_parts(
                    raw["text"],
                    dialect=dialect,
                    source_name=source_name,
                    line=line,
                    field=item_field,
                    path=item_path,
                    slots=slots,
                )
            )
            continue
        if "slot" in raw:
            name = raw["slot"]
            if not isinstance(name, str) or name not in slots:
                raise BusinessError(
                    E_FORMAT_CORRUPT,
                    "v3 slot node references an undeclared slot",
                    line=line,
                    field=f"{item_field}.slot",
                    column=1,
                    details={"source": source_name, "slot": name},
                )
            declaration = slots[name]
            result.append(
                {
                    "node_id": _node_id(dialect, source_name, item_path, "slot", line),
                    "kind": "slot",
                    "name": declaration.name,
                    "slot_type": declaration.slot_type,
                    "declared": declaration.declared,
                    "origin": declaration.origin,
                    "source": _source(dialect, source_name, line, item_field),
                }
            )
            continue
        if "ref" in raw:
            target = raw["ref"]
            if not isinstance(target, str) or not target:
                raise BusinessError(
                    E_FORMAT_CORRUPT,
                    "v3 reference node requires a non-empty target",
                    line=line,
                    field=f"{item_field}.ref",
                    column=1,
                    details={"source": source_name},
                )
            result.append(
                {
                    "node_id": _node_id(dialect, source_name, item_path, "reference", line),
                    "kind": "reference",
                    "target": target,
                    "source": _source(dialect, source_name, line, item_field),
                }
            )
            continue
        if "if" in raw:
            condition = _normalize_condition(
                raw["if"],
                line=line,
                field=f"{item_field}.if",
                source_name=source_name,
            )
            for branch_name in ("then", "else"):
                if branch_name not in raw or not isinstance(raw[branch_name], list):
                    raise BusinessError(
                        E_FORMAT_CORRUPT,
                        f"v3 condition node requires a {branch_name} node array",
                        line=line,
                        field=f"{item_field}.{branch_name}",
                        column=1,
                        details={"source": source_name},
                    )
            then_parts = _compile_replacement_parts(
                raw["then"],
                dialect=dialect,
                source_name=source_name,
                line=line,
                field_path=f"{item_field}.then",
                path=f"{item_path}.then",
                slots=slots,
            )
            else_parts = _compile_replacement_parts(
                raw["else"],
                dialect=dialect,
                source_name=source_name,
                line=line,
                field_path=f"{item_field}.else",
                path=f"{item_path}.else",
                slots=slots,
            )
            result.append(
                {
                    "node_id": _node_id(dialect, source_name, item_path, "condition", line),
                    "kind": "condition",
                    "condition": condition,
                    "then": _sequence(
                        dialect=dialect,
                        source_name=source_name,
                        line=line,
                        field=f"{item_field}.then",
                        path=f"{item_path}.then",
                        parts=then_parts,
                    ),
                    "else": _sequence(
                        dialect=dialect,
                        source_name=source_name,
                        line=line,
                        field=f"{item_field}.else",
                        path=f"{item_path}.else",
                        parts=else_parts,
                    ),
                    "source": _source(dialect, source_name, line, item_field),
                }
            )
            continue
        raise BusinessError(
            E_FORMAT_CORRUPT,
            "v3 replacement node must contain text, slot, ref or if",
            line=line,
            field=item_field,
            column=1,
            details={"source": source_name, "keys": sorted(raw)},
        )
    return result


def _compile_rule(
    rule: ParsedRule,
    *,
    dialect: str,
    source_name: str,
    slots_by_name: dict[str, SlotDeclaration],
) -> dict[str, Any]:
    line = rule.source.line
    pattern: dict[str, Any] | None = None
    if rule.pattern_text is not None:
        pattern = _sequence(
            dialect=dialect,
            source_name=source_name,
            line=line,
            field="pattern",
            path=f"rules.{rule.rule_id}.pattern",
            parts=_text_parts(
                rule.pattern_text,
                dialect=dialect,
                source_name=source_name,
                line=line,
                field="pattern",
                path=f"rules.{rule.rule_id}.pattern",
                slots=slots_by_name,
            ),
        )
    if rule.replacement_nodes is not None:
        replacement_parts = _compile_replacement_parts(
            rule.replacement_nodes,
            dialect=dialect,
            source_name=source_name,
            line=line,
            field_path="replacement",
            path=f"rules.{rule.rule_id}.replacement",
            slots=slots_by_name,
        )
    else:
        replacement_parts = _text_parts(
            rule.replacement_text or "",
            dialect=dialect,
            source_name=source_name,
            line=line,
            field="replacement",
            path=f"rules.{rule.rule_id}.replacement",
            slots=slots_by_name,
        )
    replacement = _sequence(
        dialect=dialect,
        source_name=source_name,
        line=line,
        field="replacement",
        path=f"rules.{rule.rule_id}.replacement",
        parts=replacement_parts,
    )
    serialized_slots = [
        {
            **slot.to_dict(),
            "kind": "slot_declaration",
            "node_id": _node_id(
                dialect,
                source_name,
                f"rules.{rule.rule_id}.slots.{slot.name}",
                "slot_declaration",
                line,
            ),
        }
        for slot in rule.slots
    ]
    return {
        "node_id": _node_id(dialect, source_name, f"rules.{rule.rule_id}", "rule", line),
        "kind": "rule",
        "rule_id": rule.rule_id,
        "rule_kind": rule.kind,
        "emit": rule.kind == "rule",
        "enabled": rule.enabled,
        "weight": rule.weight,
        "max_depth": rule.max_depth,
        "priority": rule.priority,
        "source": rule.source.to_dict(),
        "slots": serialized_slots,
        "pattern": pattern,
        "replacement": replacement,
        "fills": [fill.to_dict() for fill in rule.fills],
    }


def _iter_reference_targets(node: Any):
    if isinstance(node, dict):
        if node.get("kind") == "reference":
            yield node
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                yield from _iter_reference_targets(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_reference_targets(item)


def _validate_compiled(
    rules: list[dict[str, Any]],
    config: CompilerConfig,
    slot_types: dict[str, Any],
) -> None:
    rule_map = {rule["rule_id"]: rule for rule in rules}
    available_types = set(slot_types["types"])
    for rule in rules:
        if rule["weight"] <= 0:
            raise BusinessError(
                E_CONFIG_INVALID,
                "rule weight must be positive",
                line=rule["source"]["line"],
                field=f"rules.{rule['rule_id']}.weight",
                column=rule["source"]["column"],
                details={
                    "source": rule["source"]["source"],
                    "rule_id": rule["rule_id"],
                    "weight": rule["weight"],
                },
            )
        if rule["max_depth"] < 1 or rule["max_depth"] > config.max_depth_limit:
            raise BusinessError(
                E_CONFIG_INVALID,
                "rule max_depth is outside the compiler allowed range",
                line=rule["source"]["line"],
                field=f"rules.{rule['rule_id']}.max_depth",
                column=rule["source"]["column"],
                details={
                    "source": rule["source"]["source"],
                    "rule_id": rule["rule_id"],
                    "max_depth": rule["max_depth"],
                    "allowed": [1, config.max_depth_limit],
                },
            )
        if rule["priority"] < 0:
            raise BusinessError(
                E_CONFIG_INVALID,
                "rule priority/order must be non-negative",
                line=rule["source"]["line"],
                field=f"rules.{rule['rule_id']}.priority",
                column=rule["source"]["column"],
                details={"source": rule["source"]["source"], "rule_id": rule["rule_id"]},
            )
        for slot in rule["slots"]:
            if slot["type"] not in available_types:
                raise BusinessError(
                    E_CONFIG_INVALID,
                    "slot type is outside the controlled slot type set",
                    line=slot["source"]["line"],
                    field=f"rules.{rule['rule_id']}.slots.{slot['name']}.type",
                    column=slot["source"]["column"],
                    details={
                        "reason": "slot_type_out_of_bounds",
                        "source": slot["source"]["source"],
                        "rule_id": rule["rule_id"],
                        "slot": slot["name"],
                        "slot_type": slot["type"],
                        "available_types": sorted(available_types),
                    },
                )
        for reference in _iter_reference_targets(rule["replacement"]):
            target = reference["target"]
            if target not in rule_map:
                raise BusinessError(
                    E_FORMAT_CORRUPT,
                    f"reference target {target} does not exist",
                    line=reference["source"]["line"],
                    field=f"rules.{rule['rule_id']}.replacement.ref",
                    column=reference["source"]["column"],
                    details={"source": reference["source"]["source"], "target": target},
                )
            if rule_map[target]["rule_kind"] != "fragment":
                raise BusinessError(
                    E_FORMAT_CORRUPT,
                    f"reference target {target} is not a fragment",
                    line=reference["source"]["line"],
                    field=f"rules.{rule['rule_id']}.replacement.ref",
                    column=reference["source"]["column"],
                    details={"source": reference["source"]["source"], "target": target},
                )
        for condition in _iter_conditions(rule["replacement"]):
            slot_name = condition["condition"]["slot"]
            declared = {slot["name"] for slot in rule["slots"]}
            if slot_name not in declared:
                raise BusinessError(
                    E_FORMAT_CORRUPT,
                    f"condition references undeclared slot {slot_name}",
                    line=condition["source"]["line"],
                    field=f"rules.{rule['rule_id']}.replacement.if.slot",
                    column=condition["source"]["column"],
                    details={"source": condition["source"]["source"], "slot": slot_name},
                )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(rule_id: str) -> None:
        if rule_id in visited:
            return
        if rule_id in visiting:
            raise BusinessError(
                E_CONFIG_INVALID,
                f"reference cycle detected at {rule_id}",
                line=rule_map[rule_id]["source"]["line"],
                field=f"rules.{rule_id}.replacement",
                column=1,
                details={"source": rule_map[rule_id]["source"]["source"], "rule_id": rule_id},
            )
        visiting.add(rule_id)
        for reference in _iter_reference_targets(rule_map[rule_id]["replacement"]):
            visit(reference["target"])
        visiting.remove(rule_id)
        visited.add(rule_id)

    for rule_id in sorted(rule_map):
        visit(rule_id)


def _iter_conditions(node: Any):
    if isinstance(node, dict):
        if node.get("kind") == "condition":
            yield node
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                yield from _iter_conditions(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_conditions(item)


def compile_templates(
    parsed: ParsedTemplateSet,
    config: CompilerConfig,
) -> dict[str, Any]:
    rules: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for rule in parsed.rules:
        if rule.rule_id in seen_ids:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                f"duplicate rule id {rule.rule_id}",
                line=rule.source.line,
                field="id",
                column=rule.source.column,
                details={"source": parsed.source_name},
            )
        seen_ids.add(rule.rule_id)
        rules.append(
            _compile_rule(
                rule,
                dialect=parsed.dialect,
                source_name=parsed.source_name,
                slots_by_name=_slot_map(rule.slots),
            )
        )
    _validate_compiled(rules, config, parsed.slot_types)
    body = {
        "format": "qrw-ir/1",
        "dialect": parsed.dialect,
        "source": {
            "name": parsed.source_name,
            "sha256": sha256_hex(parsed.raw_text.encode("utf-8")),
        },
        "compiler_config": config.to_dict(),
        "slot_types": parsed.slot_types,
        "rules": sorted(rules, key=lambda item: (item["priority"], item["source"]["line"], item["rule_id"])),
        "fills": [fill.to_dict() for fill in parsed.fills],
    }
    body["checksum"] = checksum_tag(body)
    for _path, node in walk_nodes(body):
        source = node.get("source")
        if not isinstance(source, dict) or not isinstance(source.get("line"), int) or source["line"] < 1:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "compiled node is missing a source line",
                line=1,
                field="ir.source",
                column=1,
                details={"node_id": node.get("node_id"), "dialect": parsed.dialect},
            )
    return body


def compile_bytes(compiled: dict[str, Any]) -> bytes:
    from .canonical import canonical_bytes

    return canonical_bytes(compiled)


def rule_map(compiled: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {rule["rule_id"]: rule for rule in compiled["rules"]}

