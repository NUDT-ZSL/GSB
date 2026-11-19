from __future__ import annotations

import re
from typing import Any

from .canonical import checksum_tag
from .compiler import rule_map
from .errors import BusinessError, E_CONFIG_INVALID, E_FORMAT_CORRUPT
from .models import CompilerConfig
from .scoring import make_score_detail, rank_candidates


def _type_values(slot_type: str, compiled: dict[str, Any]) -> list[str]:
    types = compiled["slot_types"]["types"]
    if slot_type not in types:
        raise BusinessError(
            E_CONFIG_INVALID,
            f"slot type {slot_type} is not available",
            field=f"slot_types.{slot_type}",
            details={"slot_type": slot_type, "available_types": sorted(types)},
        )
    return list(types[slot_type])


def _compile_pattern(
    rule: dict[str, Any],
    compiled: dict[str, Any],
) -> tuple[re.Pattern[str], list[tuple[str, str]]]:
    pattern = rule["pattern"]
    if pattern is None:
        raise BusinessError(
            E_FORMAT_CORRUPT,
            "emit rule has no pattern",
            line=rule["source"]["line"],
            field=f"rules.{rule['rule_id']}.pattern",
            details={"rule_id": rule["rule_id"]},
        )
    chunks: list[str] = []
    groups: list[tuple[str, str]] = []
    for part in pattern["parts"]:
        if part["kind"] == "text":
            chunks.append(re.escape(part["text"]))
            continue
        group_name = f"g{len(groups)}"
        values = sorted(_type_values(part["slot_type"], compiled), key=lambda item: (-len(item), item))
        if not values:
            alternatives = "(?!)"
        else:
            alternatives = "(?:" + "|".join(re.escape(item) for item in values) + ")"
        chunks.append(f"(?P<{group_name}>{alternatives})")
        groups.append((group_name, part["name"]))
    try:
        return re.compile("".join(chunks)), groups
    except re.error as exc:
        raise BusinessError(
            E_CONFIG_INVALID,
            "compiled pattern is not a valid regular expression",
            line=rule["source"]["line"],
            field=f"rules.{rule['rule_id']}.pattern",
            details={"rule_id": rule["rule_id"], "reason": str(exc)},
        ) from exc


def _match_fills(
    rule: dict[str, Any],
    compiled: dict[str, Any],
    query_text: str,
) -> list[dict[str, Any]] | None:
    regex, groups = _compile_pattern(rule, compiled)
    match = regex.fullmatch(query_text)
    if match is None:
        return None
    declarations = {slot["name"]: slot for slot in rule["slots"]}
    fills: list[dict[str, Any]] = []
    first_by_name: dict[str, dict[str, Any]] = {}
    for group_name, slot_name in groups:
        query_start, query_end = match.span(group_name)
        value = match.group(group_name)
        previous = first_by_name.get(slot_name)
        if previous is not None and previous["value"] != value:
            return None
        fill = {
            "name": slot_name,
            "value": value,
            "query_start": query_start,
            "query_end": query_end,
            "slot_type": declarations[slot_name]["type"],
        }
        fills.append(fill)
        first_by_name.setdefault(slot_name, fill)
    return fills


def _append_text(
    output: list[str],
    offsets: list[dict[str, Any]],
    text: str,
) -> None:
    if not text:
        return
    start = sum(len(part) for part in output)
    output.append(text)
    offsets.append(
        {
            "kind": "text",
            "output_start": start,
            "output_end": start + len(text),
            "query_start": None,
            "query_end": None,
        }
    )


def _append_slot(
    output: list[str],
    offsets: list[dict[str, Any]],
    fill: dict[str, Any],
    *,
    slot_name: str,
) -> None:
    start = sum(len(part) for part in output)
    value = fill["value"]
    output.append(value)
    offsets.append(
        {
            "kind": "slot",
            "slot": slot_name,
            "slot_type": fill["slot_type"],
            "output_start": start,
            "output_end": start + len(value),
            "query_start": fill["query_start"],
            "query_end": fill["query_end"],
        }
    )


def _evaluate_condition(condition: dict[str, Any], fills_by_name: dict[str, dict[str, Any]]) -> bool:
    slot_name = condition["slot"]
    if slot_name not in fills_by_name:
        raise BusinessError(
            E_CONFIG_INVALID,
            f"condition slot {slot_name} was not captured by the pattern",
            field=f"condition.{slot_name}",
            details={"slot": slot_name},
        )
    actual = fills_by_name[slot_name]["value"]
    operator = condition["operator"]
    expected = condition["value"]
    if operator == "equals":
        return actual == expected
    if operator == "not_equals":
        return actual != expected
    if operator == "in":
        return actual in expected
    raise BusinessError(
        E_CONFIG_INVALID,
        f"unsupported condition operator {operator}",
        field="condition.operator",
        details={"operator": operator},
    )


def _depth_error(rule: dict[str, Any], required_depth: int, state: dict[str, Any]) -> BusinessError:
    return BusinessError(
        E_CONFIG_INVALID,
        "template expansion exceeded the rule max_depth",
        line=rule["source"]["line"],
        field=f"rules.{rule['rule_id']}.max_depth",
        column=rule["source"]["column"],
        details={
            "reason": "expansion_depth_exceeded",
            "rule_id": rule["rule_id"],
            "required_depth": required_depth,
            "max_depth": rule["max_depth"],
            "rule_chain": list(state["chain"]),
        },
    )


def _render_parts(
    parts: list[dict[str, Any]],
    *,
    rule: dict[str, Any],
    compiled: dict[str, Any],
    fragments: dict[str, dict[str, Any]],
    fills_by_name: dict[str, dict[str, Any]],
    depth: int,
    state: dict[str, Any],
    output: list[str],
    offsets: list[dict[str, Any]],
) -> None:
    for part in parts:
        kind = part["kind"]
        if kind == "text":
            _append_text(output, offsets, part["text"])
            continue
        if kind == "slot":
            name = part["name"]
            if name not in fills_by_name:
                raise BusinessError(
                    E_CONFIG_INVALID,
                    f"replacement slot {name} was not captured by the pattern",
                    line=part["source"]["line"],
                    field=f"rules.{rule['rule_id']}.replacement.{name}",
                    column=part["source"]["column"],
                    details={"rule_id": rule["rule_id"], "slot": name},
                )
            _append_slot(output, offsets, fills_by_name[name], slot_name=name)
            continue
        if kind == "condition":
            branch = part["then"] if _evaluate_condition(part["condition"], fills_by_name) else part["else"]
            _render_parts(
                branch["parts"],
                rule=rule,
                compiled=compiled,
                fragments=fragments,
                fills_by_name=fills_by_name,
                depth=depth,
                state=state,
                output=output,
                offsets=offsets,
            )
            continue
        if kind == "reference":
            target = part["target"]
            fragment = fragments.get(target)
            if fragment is None or not fragment.get("enabled", True):
                raise BusinessError(
                    E_CONFIG_INVALID,
                    f"reference target {target} is unavailable",
                    line=part["source"]["line"],
                    field=f"rules.{rule['rule_id']}.replacement.ref",
                    column=part["source"]["column"],
                    details={"rule_id": rule["rule_id"], "target": target},
                )
            next_depth = depth + 1
            if next_depth > rule["max_depth"]:
                raise _depth_error(rule, next_depth, state)
            if next_depth > compiled["compiler_config"]["max_depth_limit"]:
                raise _depth_error(rule, next_depth, state)
            state["max_depth"] = max(state["max_depth"], next_depth)
            state["chain"].append(target)
            _render_parts(
                fragment["replacement"]["parts"],
                rule=rule,
                compiled=compiled,
                fragments=fragments,
                fills_by_name=fills_by_name,
                depth=next_depth,
                state=state,
                output=output,
                offsets=offsets,
            )
            continue
        raise BusinessError(
            E_CONFIG_INVALID,
            f"unknown IR node kind {kind}",
            line=part.get("source", {}).get("line"),
            field=f"rules.{rule['rule_id']}.replacement",
            details={"kind": kind},
        )


def expand_query(
    compiled: dict[str, Any],
    query: dict[str, Any],
    config: CompilerConfig,
) -> list[dict[str, Any]]:
    query_text = query.get("query_text")
    if not isinstance(query_text, str):
        raise BusinessError(
            E_FORMAT_CORRUPT,
            "query_text must be a string",
            field="query_text",
            details={"query_id": query.get("query_id")},
        )
    rules = rule_map(compiled)
    fragments = {rule_id: rule for rule_id, rule in rules.items() if rule["rule_kind"] == "fragment"}
    candidates: list[dict[str, Any]] = []
    ordered_rules = sorted(
        (rule for rule in rules.values() if rule["emit"] and rule["enabled"]),
        key=lambda item: (item["priority"], item["source"]["line"], item["rule_id"]),
    )
    for rule in ordered_rules:
        fills = _match_fills(rule, compiled, query_text)
        if fills is None:
            continue
        fills_by_name: dict[str, dict[str, Any]] = {}
        for fill in fills:
            fills_by_name.setdefault(fill["name"], fill)
        output: list[str] = []
        offsets: list[dict[str, Any]] = []
        state = {
            "max_depth": 1,
            "chain": [rule["rule_id"]],
        }
        _render_parts(
            rule["replacement"]["parts"],
            rule=rule,
            compiled=compiled,
            fragments=fragments,
            fills_by_name=fills_by_name,
            depth=1,
            state=state,
            output=output,
            offsets=offsets,
        )
        candidate_text = "".join(output)
        detail = make_score_detail(
            weight=rule["weight"],
            slot_hits=len(fills),
            expansion_depth=state["max_depth"],
            config=config,
            rule_id=rule["rule_id"],
        )
        key = {
            "rule_id": rule["rule_id"],
            "candidate": candidate_text,
            "offset_map": offsets,
            "score_detail": detail,
        }
        candidates.append(
            {
                "candidate_id": "c_" + checksum_tag(key).split(":", 1)[1][:20],
                "candidate": candidate_text,
                "rule_id": rule["rule_id"],
                "rule_chain": list(state["chain"]),
                "source_line": rule["source"]["line"],
                "expansion_depth": state["max_depth"],
                "slot_hits": len(fills),
                "fills": fills,
                "score": detail["score"],
                "score_detail": detail,
                "offset_map": offsets,
            }
        )
    return rank_candidates(candidates)


def expand_all_queries(
    compiled: dict[str, Any],
    queries: list[dict[str, Any]],
    config: CompilerConfig,
) -> dict[str, list[dict[str, Any]]]:
    return {query["query_id"]: expand_query(compiled, query, config) for query in queries}

