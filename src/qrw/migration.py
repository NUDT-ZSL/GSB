from __future__ import annotations

import json
from typing import Any

from .canonical import pretty_json
from .errors import BusinessError, E_FORMAT_CORRUPT
from .models import CompilerConfig, ParsedRule, ParsedTemplateSet
from .placeholders import scan_placeholders


def _v2_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _v2_line(values: dict[str, Any]) -> str:
    order = ("id", "pattern", "replacement", "slots", "weight", "enabled", "max_depth", "order")
    return "; ".join(f"{key}={_v2_value(values[key])}" for key in order)


def migrate_v1_to_v2(parsed: ParsedTemplateSet, config: CompilerConfig) -> dict[str, Any]:
    del config
    if parsed.dialect != "v1":
        raise ValueError("migrate_v1_to_v2 requires a v1 document")
    lines = ["#V2"]
    fill_ins: list[dict[str, Any]] = []
    for rule in parsed.rules:
        slots_text = ",".join(f"{slot.name}:{slot.slot_type}" for slot in rule.slots) or "-"
        lines.append(
            _v2_line(
                {
                    "id": rule.rule_id,
                    "pattern": rule.pattern_text,
                    "replacement": rule.replacement_text,
                    "slots": slots_text,
                    "weight": rule.weight,
                    "enabled": rule.enabled,
                    "max_depth": rule.max_depth,
                    "order": rule.priority,
                }
            )
        )
        fill_ins.extend(fill.to_dict() for fill in rule.fills)
    return {
        "source_dialect": "v1",
        "target_dialect": "v2",
        "artifact": "\n".join(lines) + "\n",
        "fill_ins": fill_ins,
        "discards": [],
    }


def migrate_v2_to_v1(parsed: ParsedTemplateSet) -> dict[str, Any]:
    if parsed.dialect != "v2":
        raise ValueError("migrate_v2_to_v1 requires a v2 document")
    lines = ["#V1"]
    discards: list[dict[str, Any]] = []
    for rule in parsed.rules:
        pattern = rule.pattern_text or ""
        replacement = rule.replacement_text or ""
        if "|" in pattern or "|" in replacement:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "v1 cannot represent a pipe inside pattern or replacement",
                line=rule.source.line,
                field="pattern" if "|" in pattern else "replacement",
                column=1,
                details={"rule_id": rule.rule_id},
            )
        lines.append(f"{pattern}|{replacement}|{rule.priority}")
        if rule.slots:
            discards.append(
                {
                    "rule_id": rule.rule_id,
                    "source_line": rule.source.line,
                    "category": "槽位",
                    "policy": "flatten_to_implicit_placeholders",
                    "detail": {
                        "placeholders_retained": [slot.name for slot in rule.slots],
                        "discarded_type_constraints": {
                            slot.name: slot.slot_type for slot in rule.slots
                        },
                    },
                }
            )
        for field_name, value in (
            ("weight", rule.weight),
            ("enabled", rule.enabled),
            ("max_depth", rule.max_depth),
        ):
            discards.append(
                {
                    "rule_id": rule.rule_id,
                    "source_line": rule.source.line,
                    "category": field_name,
                    "policy": "drop_not_representable_in_v1",
                    "detail": {"value": value},
                }
            )
    return {
        "source_dialect": "v2",
        "target_dialect": "v1",
        "artifact": "\n".join(lines) + "\n",
        "fill_ins": [],
        "discards": discards,
    }


def _replacement_nodes(text: str, dialect: str, rule_id: str, line: int) -> list[dict[str, Any]]:
    spans = scan_placeholders(
        text,
        dialect=dialect,
        source_name=f"migration:{rule_id}",
        line=line,
        field="replacement",
    )
    result: list[dict[str, Any]] = []
    cursor = 0
    for span in spans:
        if span.start > cursor:
            result.append({"text": text[cursor : span.start]})
        result.append({"slot": span.name})
        cursor = span.end
    if cursor < len(text) or not result:
        result.append({"text": text[cursor:]})
    return result


def migrate_v2_to_v3(parsed: ParsedTemplateSet) -> dict[str, Any]:
    if parsed.dialect != "v2":
        raise ValueError("migrate_v2_to_v3 requires a v2 document")
    document: dict[str, Any] = {
        "version": "V3",
        "template_set_id": parsed.source_name,
        "rules": [],
    }
    fill_ins: list[dict[str, Any]] = []
    for rule in parsed.rules:
        document["rules"].append(
            {
                "id": rule.rule_id,
                "kind": "rule",
                "priority": rule.priority,
                "weight": rule.weight,
                "enabled": rule.enabled,
                "max_depth": rule.max_depth,
                "pattern": rule.pattern_text,
                "slots": [
                    {"name": slot.name, "type": slot.slot_type, "source": "query"}
                    for slot in rule.slots
                ],
                "replacement": _replacement_nodes(
                    rule.replacement_text or "",
                    "v2",
                    rule.rule_id,
                    rule.source.line,
                ),
            }
        )
        fill_ins.extend(fill.to_dict() for fill in rule.fills)
        fill_ins.append(
            {
                "rule_id": rule.rule_id,
                "source_line": rule.source.line,
                "field": "conditions",
                "default": [],
                "origin": "v3_upgrade_default",
            }
        )
        fill_ins.append(
            {
                "rule_id": rule.rule_id,
                "source_line": rule.source.line,
                "field": "references",
                "default": [],
                "origin": "v3_upgrade_default",
            }
        )
    return {
        "source_dialect": "v2",
        "target_dialect": "v3",
        "artifact": pretty_json(document) + "\n",
        "document": document,
        "fill_ins": fill_ins,
        "discards": [],
    }


def _sequence_text(sequence: dict[str, Any], *, rule_id: str) -> tuple[str, list[dict[str, Any]]]:
    chunks: list[str] = []
    flattened: list[dict[str, Any]] = []
    for part in sequence["parts"]:
        if part["kind"] == "text":
            chunks.append(part["text"])
            flattened.append({"category": "text", "field": part["source"]["field"]})
            continue
        if part["kind"] == "slot":
            chunks.append("{" + part["name"] + "}")
            flattened.append({"category": "槽位", "slot": part["name"]})
            continue
        raise BusinessError(
            E_FORMAT_CORRUPT,
            f"cannot flatten {part['kind']} node into v1",
            line=part["source"]["line"],
            field=f"rules.{rule_id}.replacement",
            column=part["source"]["column"],
            details={"rule_id": rule_id, "node_kind": part["kind"]},
        )
    return "".join(chunks), flattened


def _contains_kind(sequence: dict[str, Any], kinds: set[str]) -> list[str]:
    found: list[str] = []
    for part in sequence["parts"]:
        if part["kind"] in kinds:
            found.append(part["kind"])
        if part["kind"] == "condition":
            found.extend(_contains_kind(part["then"], kinds))
            found.extend(_contains_kind(part["else"], kinds))
    return found


def downgrade_ir_to_v1(compiled: dict[str, Any]) -> dict[str, Any]:
    lines = ["#V1"]
    discards: list[dict[str, Any]] = []
    for rule in sorted(compiled["rules"], key=lambda item: (item["priority"], item["source"]["line"])):
        if not rule["emit"]:
            discards.append(
                {
                    "rule_id": rule["rule_id"],
                    "source_line": rule["source"]["line"],
                    "category": "引用",
                    "policy": "drop_fragment_not_representable_in_v1",
                    "detail": {"rule_kind": rule["rule_kind"]},
                }
            )
            continue
        unsupported = sorted(set(_contains_kind(rule["replacement"], {"condition", "reference"})))
        if unsupported:
            categories = []
            if "condition" in unsupported:
                categories.append("条件分支")
            if "reference" in unsupported:
                categories.append("引用")
            discards.append(
                {
                    "rule_id": rule["rule_id"],
                    "source_line": rule["source"]["line"],
                    "category": ",".join(categories),
                    "policy": "drop_rule_explicitly",
                    "detail": {"unsupported_node_kinds": unsupported},
                }
            )
            continue
        pattern, _ = _sequence_text(rule["pattern"], rule_id=rule["rule_id"])
        replacement, replacement_map = _sequence_text(
            rule["replacement"],
            rule_id=rule["rule_id"],
        )
        if "|" in pattern or "|" in replacement:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "v1 cannot represent a pipe inside pattern or replacement",
                line=rule["source"]["line"],
                field=f"rules.{rule['rule_id']}.replacement",
                column=1,
                details={"rule_id": rule["rule_id"]},
            )
        lines.append(f"{pattern}|{replacement}|{rule['priority']}")
        if rule["slots"]:
            discards.append(
                {
                    "rule_id": rule["rule_id"],
                    "source_line": rule["source"]["line"],
                    "category": "槽位",
                    "policy": "flatten_to_implicit_placeholders",
                    "detail": {
                        "placeholders_retained": [slot["name"] for slot in rule["slots"]],
                        "discarded_type_constraints": {
                            slot["name"]: slot["type"] for slot in rule["slots"]
                        },
                        "flattened_nodes": replacement_map,
                    },
                }
            )
        for field_name in ("weight", "enabled", "max_depth"):
            discards.append(
                {
                    "rule_id": rule["rule_id"],
                    "source_line": rule["source"]["line"],
                    "category": field_name,
                    "policy": "drop_not_representable_in_v1",
                    "detail": {"value": rule[field_name]},
                }
            )
    return {
        "source_dialect": compiled["dialect"],
        "target_dialect": "v1",
        "artifact": "\n".join(lines) + "\n",
        "fill_ins": [],
        "discards": discards,
    }

