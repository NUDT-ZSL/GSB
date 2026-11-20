"""Readers and deterministic writers for query-rewrite rule versions v1/v2/v3."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from .errors import format_corrupt, unsupported_version
from .models import Condition, RewriteRule, RuleSet, Target
from .util import (
    checksum_object,
    parse_bool,
    require_float,
    require_int,
    stable_json_bytes,
)

_VERSION_HEADER = re.compile(r"^\s*#\s*version\s*=\s*(\S+)\s*$", re.IGNORECASE)
_SUPPORTED_VERSIONS = {"v1", "v2", "v3"}
_V2_FIELDS = {
    "rule_id",
    "source_lang",
    "source_term",
    "target_lang",
    "target_term",
    "bridge_confidence_weight",
    "max_expansion_depth",
    "enabled",
    "condition_terms",
}
_V3_RULE_FIELDS = {
    "rule_id",
    "source_lang",
    "source_term",
    "targets",
    "target_lang",
    "target_term",
    "condition",
    "bridge_confidence_weight",
    "max_expansion_depth",
    "enabled",
}


def parse_rules(
    path: str | Path,
    *,
    expected_version: str | None = None,
) -> RuleSet:
    """Read a rule file, dispatching on the version header or field set."""

    source_path = Path(path)
    try:
        text = source_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise format_corrupt(
            "cannot read rule file",
            source=str(source_path),
            reason=str(exc),
        ) from exc
    return parse_rules_text(
        text,
        source=str(source_path),
        expected_version=expected_version,
    )


def parse_rules_text(
    text: str,
    *,
    source: str = "<memory>",
    expected_version: str | None = None,
) -> RuleSet:
    detected_version, payload_start = _detect_version(text, source=source)
    if expected_version is not None and expected_version != detected_version:
        raise unsupported_version(
            "requested rule version does not match input",
            source=source,
            expected_version=expected_version,
            detected_version=detected_version,
        )
    if detected_version not in _SUPPORTED_VERSIONS:
        raise unsupported_version(
            "unsupported rule version",
            source=source,
            version=detected_version,
        )
    if detected_version == "v3":
        return _parse_v3(text, payload_start=payload_start, source=source)
    return _parse_pipe_rules(
        text,
        payload_start=payload_start,
        source=source,
        version=detected_version,
    )


def _detect_version(text: str, *, source: str) -> tuple[str, int]:
    lines = text.splitlines()
    first_data_line: int | None = None
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        header = _VERSION_HEADER.match(line)
        if header:
            version = header.group(1).lower()
            return version, index + 1
        first_data_line = index
        break

    if first_data_line is None:
        raise format_corrupt("rule file is empty", source=source)

    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return "v3", 0

    data_lines = [
        line
        for line in lines[first_data_line:]
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not data_lines:
        raise format_corrupt("rule file has no rule rows", source=source)
    if all("=" in line for line in data_lines):
        return "v2", first_data_line
    return "v1", first_data_line


def _parse_pipe_rules(
    text: str,
    *,
    payload_start: int,
    source: str,
    version: str,
) -> RuleSet:
    rules: list[RewriteRule] = []
    seen_ids: set[str] = set()
    lines = text.splitlines()
    for line_index in range(payload_start, len(lines)):
        raw_line = lines[line_index]
        line_number = line_index + 1
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        location = {"source": source, "line": line_number}
        if version == "v1":
            rule = _parse_v1_line(raw_line, location)
        else:
            rule = _parse_v2_line(raw_line, location)
        if rule.rule_id in seen_ids:
            raise format_corrupt(
                "duplicate rule_id",
                rule_id=rule.rule_id,
                location=location,
            )
        seen_ids.add(rule.rule_id)
        rules.append(rule)
    if not rules:
        raise format_corrupt("rule file has no rule rows", source=source)
    return RuleSet(format_version=version, rules=tuple(rules))


def _parse_v1_line(raw_line: str, location: dict[str, Any]) -> RewriteRule:
    fields = raw_line.strip().split("|")
    if len(fields) != 5:
        raise format_corrupt(
            "v1 rule row must contain exactly five fields",
            expected_field_count=5,
            actual_field_count=len(fields),
            row=raw_line,
            location=location,
        )
    rule_id, source_lang, source_term, target_lang, target_term = (
        field.strip() for field in fields
    )
    _require_nonempty(
        {
            "rule_id": rule_id,
            "source_lang": source_lang,
            "source_term": source_term,
            "target_lang": target_lang,
            "target_term": target_term,
        },
        location=location,
    )
    return RewriteRule(
        rule_id=rule_id,
        source_lang=source_lang,
        source_term=source_term,
        targets=(Target(target_lang, target_term),),
        source_location=location,
    )


def _parse_v2_line(raw_line: str, location: dict[str, Any]) -> RewriteRule:
    values: dict[str, str] = {}
    for field in raw_line.strip().split("|"):
        if "=" not in field:
            raise format_corrupt(
                "v2 rule field must use name=value syntax",
                field=field,
                location=location,
            )
        name, value = field.split("=", 1)
        name = name.strip()
        if not name:
            raise format_corrupt(
                "v2 rule field has an empty name",
                field=field,
                location=location,
            )
        if name not in _V2_FIELDS:
            raise format_corrupt(
                "unknown v2 rule field",
                field=name,
                location=location,
            )
        if name in values:
            raise format_corrupt(
                "duplicate v2 rule field",
                field=name,
                location=location,
            )
        if "=" in value:
            raise format_corrupt(
                "v2 rule values cannot contain '='",
                field=name,
                location=location,
            )
        values[name] = value.strip()

    required = ("rule_id", "source_lang", "source_term", "target_lang", "target_term")
    missing = [field for field in required if not values.get(field)]
    if missing:
        raise format_corrupt(
            "v2 rule is missing required fields",
            missing_fields=missing,
            location=location,
        )

    weight = None
    if values.get("bridge_confidence_weight"):
        weight = require_float(
            values["bridge_confidence_weight"],
            field="bridge_confidence_weight",
            minimum=0.0,
            maximum=1.0,
            location=location,
        )
    depth = None
    if values.get("max_expansion_depth"):
        depth = require_int(
            values["max_expansion_depth"],
            field="max_expansion_depth",
            minimum=1,
            location=location,
        )
    enabled = None
    if values.get("enabled"):
        enabled = parse_bool(
            values["enabled"],
            field="enabled",
            location=location,
        )
    condition_terms = _split_condition_terms(
        values.get("condition_terms", ""),
        location=location,
    )

    return RewriteRule(
        rule_id=values["rule_id"],
        source_lang=values["source_lang"],
        source_term=values["source_term"],
        targets=(Target(values["target_lang"], values["target_term"]),),
        bridge_confidence_weight=weight,
        max_expansion_depth=depth,
        enabled=enabled,
        condition_terms=condition_terms,
        source_location=location,
    )


def _parse_v3(
    text: str,
    *,
    payload_start: int,
    source: str,
) -> RuleSet:
    payload_lines = text.splitlines()
    payload = "\n".join(payload_lines[payload_start:])
    if not payload.strip():
        raise format_corrupt("v3 rule file has no JSON payload", source=source)
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise format_corrupt(
            "invalid v3 JSON",
            source=source,
            line=exc.lineno + payload_start,
            column=exc.colno,
            reason=exc.msg,
        ) from exc
    if not isinstance(document, dict):
        raise format_corrupt("v3 rule document must be an object", source=source)
    format_version = document.get("format_version", "v3")
    if format_version != "v3":
        raise unsupported_version(
            "unsupported JSON rule version",
            source=source,
            version=format_version,
        )
    raw_rules = document.get("rules")
    if not isinstance(raw_rules, list):
        raise format_corrupt(
            "v3 document must contain a rules array",
            source=source,
        )

    rules: list[RewriteRule] = []
    seen_ids: set[str] = set()
    for index, raw_rule in enumerate(raw_rules):
        location = {
            "source": source,
            "line": _nth_rule_id_line(payload_lines, payload_start, index),
            "rule_index": index,
        }
        if not isinstance(raw_rule, dict):
            raise format_corrupt(
                "v3 rule must be an object",
                location=location,
            )
        unknown = sorted(set(raw_rule) - _V3_RULE_FIELDS)
        if unknown:
            raise format_corrupt(
                "unknown v3 rule fields",
                fields=unknown,
                location=location,
            )
        for required_field in ("rule_id", "source_lang", "source_term"):
            if not isinstance(raw_rule.get(required_field), str) or not raw_rule[required_field]:
                raise format_corrupt(
                    "v3 rule is missing a required string field",
                    field=required_field,
                    location=location,
                )

        targets_raw = raw_rule.get("targets")
        if targets_raw is None and ("target_lang" in raw_rule or "target_term" in raw_rule):
            targets_raw = [
                {
                    "lang": raw_rule.get("target_lang"),
                    "term": raw_rule.get("target_term"),
                }
            ]
        if not isinstance(targets_raw, list) or not targets_raw:
            raise format_corrupt(
                "v3 rule requires at least one target",
                location=location,
            )
        targets: list[Target] = []
        for target_index, target_raw in enumerate(targets_raw):
            if not isinstance(target_raw, dict):
                raise format_corrupt(
                    "v3 target must be an object",
                    target_index=target_index,
                    location=location,
                )
            unknown_target = sorted(set(target_raw) - {"lang", "term"})
            if unknown_target:
                raise format_corrupt(
                    "unknown v3 target fields",
                    fields=unknown_target,
                    target_index=target_index,
                    location=location,
                )
            lang = target_raw.get("lang")
            term = target_raw.get("term")
            if not isinstance(lang, str) or not lang or not isinstance(term, str) or not term:
                raise format_corrupt(
                    "v3 target requires lang and term strings",
                    target_index=target_index,
                    location=location,
                )
            targets.append(Target(lang, term))
        targets = sorted(set(targets), key=lambda item: (item.lang, item.term))

        condition = None
        if raw_rule.get("condition") is not None:
            condition = _parse_condition(raw_rule["condition"], location=location)

        weight = None
        if raw_rule.get("bridge_confidence_weight") is not None:
            weight = require_float(
                raw_rule["bridge_confidence_weight"],
                field="bridge_confidence_weight",
                minimum=0.0,
                maximum=1.0,
                location=location,
            )
        depth = None
        if raw_rule.get("max_expansion_depth") is not None:
            depth = require_int(
                raw_rule["max_expansion_depth"],
                field="max_expansion_depth",
                minimum=1,
                location=location,
            )
        enabled = None
        if raw_rule.get("enabled") is not None:
            enabled = parse_bool(
                raw_rule["enabled"],
                field="enabled",
                location=location,
            )

        rule = RewriteRule(
            rule_id=raw_rule["rule_id"],
            source_lang=raw_rule["source_lang"],
            source_term=raw_rule["source_term"],
            targets=tuple(targets),
            condition=condition,
            bridge_confidence_weight=weight,
            max_expansion_depth=depth,
            enabled=enabled,
            source_location=location,
        )
        if rule.rule_id in seen_ids:
            raise format_corrupt(
                "duplicate rule_id",
                rule_id=rule.rule_id,
                location=location,
            )
        seen_ids.add(rule.rule_id)
        rules.append(rule)
    return RuleSet(format_version="v3", rules=tuple(rules))


def _nth_rule_id_line(
    lines: list[str],
    payload_start: int,
    rule_index: int,
) -> int:
    seen = 0
    for offset, line in enumerate(lines[payload_start:], start=payload_start):
        occurrences = line.count('"rule_id"')
        if seen + occurrences > rule_index:
            return offset + 1
        seen += occurrences
    return payload_start + 1 if payload_start < len(lines) else 1


def _parse_condition(raw: Any, *, location: dict[str, Any]) -> Condition:
    if not isinstance(raw, dict):
        raise format_corrupt(
            "condition must be an object",
            location=location,
        )
    if "op" in raw:
        op = raw["op"]
        children_raw = raw.get("children", [])
    elif set(raw) == {"term_present"}:
        op = "term_present"
        term = raw["term_present"]
        if not isinstance(term, str) or not term:
            raise format_corrupt(
                "term_present condition requires a term",
                location=location,
            )
        return Condition(op="term_present", term=term)
    elif set(raw) == {"min_freq"}:
        detail = raw["min_freq"]
        if not isinstance(detail, dict) or set(detail) != {"term", "value"}:
            raise format_corrupt(
                "min_freq condition requires term and value",
                location=location,
            )
        term = detail["term"]
        if not isinstance(term, str) or not term:
            raise format_corrupt(
                "min_freq term must be a non-empty string",
                location=location,
            )
        value = require_int(
            detail["value"],
            field="condition.min_freq.value",
            minimum=1,
            location=location,
        )
        return Condition(op="min_freq", term=term, value=value)
    elif set(raw) in ({"all"}, {"any"}):
        op = next(iter(raw))
        children_raw = raw[op]
    else:
        raise format_corrupt(
            "unknown condition shape",
            condition=raw,
            location=location,
        )

    if op not in {"all", "any", "term_present", "min_freq"}:
        raise format_corrupt(
            "unknown condition operator",
            operator=op,
            location=location,
        )
    if op in {"all", "any"}:
        if not isinstance(children_raw, list) or not children_raw:
            raise format_corrupt(
                "logical condition requires non-empty children",
                operator=op,
                location=location,
            )
        return Condition(
            op=op,
            children=tuple(
                _parse_condition(child, location=location) for child in children_raw
            ),
        )

    allowed = {"op", "term"} | ({"value"} if op == "min_freq" else set())
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise format_corrupt(
            "unknown condition fields",
            fields=unknown,
            location=location,
        )
    term = raw.get("term")
    if not isinstance(term, str) or not term:
        raise format_corrupt(
            "condition term must be a non-empty string",
            location=location,
        )
    if op == "term_present":
        return Condition(op=op, term=term)
    value = require_int(
        raw.get("value"),
        field="condition.min_freq.value",
        minimum=1,
        location=location,
    )
    return Condition(op=op, term=term, value=value)


def _split_condition_terms(
    value: str,
    *,
    location: dict[str, Any],
) -> tuple[str, ...]:
    if not value:
        return ()
    terms = [term.strip() for term in value.split(",")]
    if any(not term for term in terms):
        raise format_corrupt(
            "condition_terms contains an empty term",
            location=location,
        )
    if len(set(terms)) != len(terms):
        raise format_corrupt(
            "condition_terms contains duplicate terms",
            location=location,
        )
    return tuple(sorted(terms))


def _require_nonempty(fields: Mapping[str, str], *, location: dict[str, Any]) -> None:
    missing = sorted(name for name, value in fields.items() if not value)
    if missing:
        raise format_corrupt(
            "rule row is missing required values",
            fields=missing,
            location=location,
        )


def serialize_ruleset(ruleset: RuleSet) -> bytes:
    """Return canonical native bytes for the ruleset's declared format."""

    if ruleset.format_version == "v1":
        lines = ["# version=v1"]
        for rule in sorted(ruleset.rules, key=lambda item: item.rule_id):
            _ensure_expressible_v1(rule)
            target = rule.targets[0]
            lines.append(
                "|".join(
                    [
                        _v1_value(rule.rule_id),
                        _v1_value(rule.source_lang),
                        _v1_value(rule.source_term),
                        _v1_value(target.lang),
                        _v1_value(target.term),
                    ]
                )
            )
        return ("\n".join(lines) + "\n").encode("utf-8")

    if ruleset.format_version == "v2":
        lines = ["# version=v2"]
        for rule in sorted(ruleset.rules, key=lambda item: item.rule_id):
            _ensure_expressible_v2(rule)
            target = rule.targets[0]
            fields = [
                ("rule_id", _v2_value(rule.rule_id)),
                ("source_lang", _v2_value(rule.source_lang)),
                ("source_term", _v2_value(rule.source_term)),
                ("target_lang", _v2_value(target.lang)),
                ("target_term", _v2_value(target.term)),
            ]
            if rule.bridge_confidence_weight is not None:
                fields.append(
                    (
                        "bridge_confidence_weight",
                        repr(float(rule.bridge_confidence_weight)),
                    )
                )
            if rule.max_expansion_depth is not None:
                fields.append(("max_expansion_depth", str(rule.max_expansion_depth)))
            if rule.enabled is not None:
                fields.append(("enabled", "true" if rule.enabled else "false"))
            if rule.condition_terms:
                fields.append(
                    ("condition_terms", _v2_value(",".join(rule.condition_terms)))
                )
            lines.append("|".join(f"{name}={value}" for name, value in fields))
        return ("\n".join(lines) + "\n").encode("utf-8")

    if ruleset.format_version == "v3":
        document = {
            "format_version": "v3",
            "rules": [
                _rule_to_v3_dict(rule)
                for rule in sorted(ruleset.rules, key=lambda item: item.rule_id)
            ],
        }
        return stable_json_bytes(document)

    raise unsupported_version(
        "cannot serialize unsupported rule version",
        version=ruleset.format_version,
    )


def _ensure_expressible_v1(rule: RewriteRule) -> None:
    if len(rule.targets) != 1:
        raise format_corrupt(
            "v1 cannot express multiple targets",
            rule_id=rule.rule_id,
            target_count=len(rule.targets),
        )
    if (
        rule.condition is not None
        or rule.condition_terms
        or rule.bridge_confidence_weight is not None
        or rule.max_expansion_depth is not None
        or rule.enabled is not None
    ):
        raise format_corrupt(
            "v1 cannot express optional migration fields or conditions",
            rule_id=rule.rule_id,
        )


def _ensure_expressible_v2(rule: RewriteRule) -> None:
    if len(rule.targets) != 1:
        raise format_corrupt(
            "v2 cannot express multiple targets",
            rule_id=rule.rule_id,
            target_count=len(rule.targets),
        )
    if rule.condition is not None:
        terms = rule.condition.conjunction_terms()
        if terms is None or terms != tuple(sorted(rule.condition_terms)):
            raise format_corrupt(
                "v2 can only express an AND-of-term_present condition",
                rule_id=rule.rule_id,
            )


def _v1_value(value: str) -> str:
    if not value or "|" in value or "\n" in value or "\r" in value:
        raise format_corrupt(
            "v1 field cannot be empty or contain delimiters",
            value=value,
        )
    return value


def _v2_value(value: str) -> str:
    if not value or "|" in value or "=" in value or "\n" in value or "\r" in value:
        raise format_corrupt(
            "v2 field cannot be empty or contain delimiters",
            value=value,
        )
    return value


def _rule_to_v3_dict(rule: RewriteRule) -> dict[str, Any]:
    result: dict[str, Any] = {
        "rule_id": rule.rule_id,
        "source_lang": rule.source_lang,
        "source_term": rule.source_term,
        "targets": [
            target.to_dict()
            for target in sorted(rule.targets, key=lambda item: (item.lang, item.term))
        ],
    }
    if rule.condition is not None:
        result["condition"] = rule.condition.to_dict()
    elif rule.condition_terms:
        result["condition"] = Condition(
            op="all",
            children=tuple(
                Condition(op="term_present", term=term)
                for term in rule.condition_terms
            ),
        ).to_dict()
    if rule.bridge_confidence_weight is not None:
        result["bridge_confidence_weight"] = rule.bridge_confidence_weight
    if rule.max_expansion_depth is not None:
        result["max_expansion_depth"] = rule.max_expansion_depth
    if rule.enabled is not None:
        result["enabled"] = rule.enabled
    return result


def ruleset_semantic_dict(ruleset: RuleSet, *, include_format_version: bool = True) -> dict[str, Any]:
    result: dict[str, Any] = {
        "rules": [
            _rule_semantic_dict(rule)
            for rule in sorted(ruleset.rules, key=lambda item: item.rule_id)
        ]
    }
    if include_format_version:
        result["format_version"] = ruleset.format_version
    return result


def _rule_semantic_dict(rule: RewriteRule) -> dict[str, Any]:
    result: dict[str, Any] = {
        "rule_id": rule.rule_id,
        "source_lang": rule.source_lang,
        "source_term": rule.source_term,
        "targets": [
            target.to_dict()
            for target in sorted(rule.targets, key=lambda item: (item.lang, item.term))
        ],
    }
    if rule.condition is not None:
        result["condition"] = rule.condition.to_dict()
    if rule.bridge_confidence_weight is not None:
        result["bridge_confidence_weight"] = rule.bridge_confidence_weight
    if rule.max_expansion_depth is not None:
        result["max_expansion_depth"] = rule.max_expansion_depth
    if rule.enabled is not None:
        result["enabled"] = rule.enabled
    if rule.condition_terms:
        result["condition_terms"] = list(rule.condition_terms)
    return result


def ruleset_checksum(ruleset: RuleSet) -> str:
    return checksum_object(ruleset_semantic_dict(ruleset, include_format_version=False))


def canonical_rules_bytes(ruleset: RuleSet) -> bytes:
    return serialize_ruleset(ruleset)


def ruleset_from_semantic_dict(value: Mapping[str, Any], *, format_version: str = "v3") -> RuleSet:
    """Rebuild a ruleset from snapshot JSON without trusting file locations."""

    rules: list[RewriteRule] = []
    raw_rules = value.get("rules")
    if not isinstance(raw_rules, list):
        raise format_corrupt("snapshot rules payload is invalid")
    for index, raw in enumerate(raw_rules):
        if not isinstance(raw, dict):
            raise format_corrupt("snapshot rule must be an object", rule_index=index)
        if "targets" not in raw:
            raise format_corrupt("snapshot rule has no targets", rule_index=index)
        targets = tuple(
            Target(str(target["lang"]), str(target["term"]))
            for target in raw["targets"]
        )
        condition = (
            _condition_from_dict(raw["condition"])
            if raw.get("condition") is not None
            else None
        )
        rules.append(
            RewriteRule(
                rule_id=str(raw["rule_id"]),
                source_lang=str(raw["source_lang"]),
                source_term=str(raw["source_term"]),
                targets=targets,
                condition=condition,
                bridge_confidence_weight=raw.get("bridge_confidence_weight"),
                max_expansion_depth=raw.get("max_expansion_depth"),
                enabled=raw.get("enabled"),
                condition_terms=tuple(raw.get("condition_terms", ())),
            )
        )
    return RuleSet(format_version=format_version, rules=tuple(rules))


def _condition_from_dict(value: Mapping[str, Any]) -> Condition:
    op = value.get("op")
    if op in {"term_present", "min_freq"}:
        return Condition(
            op=op,
            term=str(value["term"]),
            value=value.get("value"),
        )
    if op in {"all", "any"}:
        return Condition(
            op=op,
            children=tuple(_condition_from_dict(child) for child in value["children"]),
        )
    raise format_corrupt("snapshot condition has unknown operator", operator=op)
