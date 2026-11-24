"""Rule merge, dependency cycles, transition-constraint evaluation."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from .codes import E_RULE_CYCLE
from .models import ParsedMessage, Rule, RuleHit, TransitionConstraint
from .util import BusinessError


def merge_rule_view(
    rules: Iterable[Rule],
    at_ms: int,
) -> tuple[dict[str, Rule], list[dict[str, Any]]]:
    """Return the authoritative active rule per canonical name at ``at_ms``.

    Disabled and tombstone records participate in authority selection. Thus a
    newer tombstone masks every older active record, even when the older record
    was the previous winner.
    """

    grouped: dict[str, list[Rule]] = defaultdict(list)
    for rule in rules:
        grouped[rule.canonical_name].append(rule)

    active: dict[str, Rule] = {}
    audit: list[dict[str, Any]] = []
    for name in sorted(grouped):
        candidates = [
            rule
            for rule in grouped[name]
            if rule.effective_from_ms <= at_ms
            and (rule.effective_to_ms is None or at_ms < rule.effective_to_ms)
        ]
        if not candidates:
            audit.append(
                {
                    "canonical_name": name,
                    "selected": None,
                    "reason": "NO_ACTIVE_WINDOW",
                    "candidates": [],
                }
            )
            continue
        candidates.sort(key=lambda item: item.authority_key())
        winner = candidates[-1]
        selected = None if winner.tombstone or not winner.enabled else winner
        if selected is not None:
            active[name] = selected
        audit.append(
            {
                "canonical_name": name,
                "selected": winner.to_dict() if selected is not None else None,
                "winner": winner.to_dict(),
                "reason": (
                    "TOMBSTONE"
                    if winner.tombstone
                    else "DISABLED"
                    if not winner.enabled
                    else "ACTIVE"
                ),
                "candidates": [rule.to_dict() for rule in candidates],
            }
        )
    return active, audit


def latest_rule_records(rules: Iterable[Rule]) -> dict[str, Rule]:
    """Latest record per name regardless of enabled/tombstone status."""

    grouped: dict[str, list[Rule]] = defaultdict(list)
    for rule in rules:
        grouped[rule.canonical_name].append(rule)
    return {
        name: sorted(items, key=lambda item: item.authority_key())[-1]
        for name, items in grouped.items()
    }


def detect_rule_cycles(rules: Iterable[Rule]) -> None:
    graph: dict[str, set[str]] = defaultdict(set)
    for rule in latest_rule_records(rules).values():
        graph.setdefault(rule.canonical_name, set())
        for dependency in rule.depends_on:
            graph[rule.canonical_name].add(dependency)

    state: dict[str, int] = {}
    stack: list[str] = []
    cycles: list[list[str]] = []

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for neighbor in sorted(graph.get(node, ())):
            if neighbor not in graph:
                continue
            if state.get(neighbor, 0) == 0:
                visit(neighbor)
            elif state.get(neighbor) == 1:
                start = stack.index(neighbor)
                cycle = stack[start:] + [neighbor]
                canonical = _canonical_cycle(cycle)
                if canonical not in cycles:
                    cycles.append(canonical)
        stack.pop()
        state[node] = 2

    for node in sorted(graph):
        if state.get(node, 0) == 0:
            visit(node)

    if cycles:
        cycles.sort(key=lambda items: tuple(items))
        cycle = cycles[0]
        raise BusinessError(
            E_RULE_CYCLE,
            "rule dependency cycle detected",
            {
                "cycle": cycle,
                "rule_names": cycle[:-1],
                "all_cycles": cycles,
            },
        )


def _canonical_cycle(cycle: list[str]) -> list[str]:
    body = cycle[:-1]
    rotations = [body[index:] + body[:index] for index in range(len(body))]
    best = min(rotations, key=lambda items: tuple(items))
    return best + [best[0]]


def rule_dependency_closure(
    changed: Iterable[str],
    rules: Iterable[Rule],
) -> set[str]:
    """Return changed rules plus rules that transitively depend on them."""

    graph: dict[str, set[str]] = defaultdict(set)
    for rule in latest_rule_records(rules).values():
        graph.setdefault(rule.canonical_name, set())
        for dependency in rule.depends_on:
            graph.setdefault(dependency, set()).add(rule.canonical_name)
    closure = set(changed)
    queue = sorted(closure)
    while queue:
        current = queue.pop(0)
        for dependent in sorted(graph.get(current, ())):
            if dependent not in closure:
                closure.add(dependent)
                queue.append(dependent)
                queue.sort()
    return closure


def _context_values(context: dict[str, Any], key: str) -> set[str]:
    value = context.get(key)
    if value is None:
        return set()
    if isinstance(value, str):
        return {value.casefold()}
    if isinstance(value, (set, list, tuple)):
        return {str(item).casefold() for item in value}
    return {str(value).casefold()}


def _condition_matches(
    conditions: dict[str, Any],
    context: dict[str, Any],
) -> bool:
    for key, expected in conditions.items():
        normalized = key.casefold()
        if normalized in {"header_names_any", "header_name"}:
            wanted = _as_lower_set(expected)
            if not wanted & _context_values(context, "header_names"):
                return False
        elif normalized in {"header_names_all"}:
            wanted = _as_lower_set(expected)
            if not wanted <= _context_values(context, "header_names"):
                return False
        elif normalized in {
            "duplicate_header",
            "duplicate",
            "same_name_header",
            "repeated_header",
        }:
            if bool(expected) != bool(context.get("duplicate_header")):
                return False
        elif normalized == "framing":
            wanted = _as_lower_set(expected)
            framing_values = _context_values(context, "framing")
            semantic_match = bool(wanted & framing_values)
            if wanted & {"ambiguous", "te_cl", "te-cl"}:
                semantic_match = semantic_match or len(framing_values) > 1
            if not semantic_match:
                return False
        elif normalized == "framing_pattern":
            pattern = str(expected).upper()
            framing_values = {
                str(item).upper() for item in context.get("framing", set())
            }
            ambiguous = (
                len(framing_values) > 1
                or "AMBIGUOUS" in framing_values
                or bool(context.get("framing_ambiguous"))
            )
            if pattern == "AMBIGUOUS" and not ambiguous:
                return False
            if pattern == "CHUNKED" and "CHUNKED" not in framing_values:
                return False
            if (
                pattern == "CONTENT_LENGTH"
                and "CONTENT_LENGTH" not in framing_values
            ):
                return False
        elif normalized in {"anomaly", "anomaly_any"}:
            wanted = _as_lower_set(expected)
            if not wanted & _context_values(context, "anomalies"):
                return False
        elif normalized == "chunk_pattern":
            pattern = str(expected).upper()
            anomalies = _context_values(context, "anomalies")
            if pattern in {"INVALID", "MALFORMED"} and not any(
                "chunk" in item and ("invalid" in item or "malformed" in item)
                for item in anomalies
            ):
                return False
            if pattern == "ZERO" and not context.get("chunk_zero"):
                return False
            if pattern == "EXTENSION" and not context.get("chunk_extension"):
                return False
        elif normalized == "trailer_present":
            if bool(expected) != bool(context.get("trailer_present")):
                return False
        elif normalized == "te_token":
            if str(expected).casefold() not in _context_values(
                context, "transfer_encoding"
            ):
                return False
        elif normalized == "method":
            if str(expected).upper() != str(context.get("method", "")).upper():
                return False
        elif normalized == "request_target":
            if str(expected) != str(context.get("request_target", "")):
                return False
        else:
            if expected != context.get(normalized):
                return False
    return True


def _as_lower_set(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value.casefold()}
    if isinstance(value, (list, tuple, set)):
        return {str(item).casefold() for item in value}
    return {str(value).casefold()}


@dataclass
class Evaluation:
    hits: list[RuleHit] = field(default_factory=list)
    suppressed: list[dict[str, Any]] = field(default_factory=list)
    candidate_rules: set[str] = field(default_factory=set)


class RuleEngine:
    """Apply transition constraints to state-machine events."""

    def __init__(
        self,
        active_rules: Iterable[Rule],
        *,
        audit_rules: Iterable[Rule] | None = None,
    ):
        self.active_rules = sorted(
            active_rules,
            key=lambda rule: rule.authority_key(),
            reverse=True,
        )
        self.audit_rules = sorted(
            audit_rules if audit_rules is not None else self.active_rules,
            key=lambda rule: rule.authority_key(),
            reverse=True,
        )

    def evaluate(
        self,
        *,
        state: str,
        event: str,
        byte_offset: int,
        context: dict[str, Any],
    ) -> Evaluation:
        matches: list[tuple[Rule, TransitionConstraint]] = []
        candidates: set[str] = set()
        for rule in self.audit_rules:
            for transition in rule.transitions:
                if state not in transition.from_states:
                    continue
                if transition.event is not None and transition.event != event:
                    continue
                if _condition_matches(transition.conditions, context):
                    candidates.add(rule.canonical_name)
                    if rule in self.active_rules:
                        matches.append((rule, transition))

        matches.sort(
            key=lambda item: (
                -item[0].generation,
                -item[0].effective_from_ms,
                item[0].canonical_name,
                item[1].index,
            )
        )
        result = Evaluation(candidate_rules=candidates)
        if not matches:
            return result

        winner_rule, winner_transition = matches[0]
        winner_action = winner_transition.action
        for rule, transition in matches:
            hit = RuleHit(
                rule_key=rule.canonical_name,
                generation=rule.generation,
                effective_from_ms=rule.effective_from_ms,
                state=state,
                event=event,
                byte_offset=byte_offset,
                action=transition.action,
                code=transition.code,
                transition_index=transition.index,
            )
            if transition.action == winner_action:
                result.hits.append(hit)
            else:
                result.suppressed.append(
                    {
                        **hit.to_dict(),
                        "suppressed_by": winner_rule.canonical_name,
                        "winner_action": winner_action,
                    }
                )
        return result

    def evaluate_message(self, message: ParsedMessage) -> Evaluation:
        combined = Evaluation()
        context = _base_context(message)
        for transition in message.state_trace:
            evaluation = self.evaluate(
                state=transition.state,
                event=transition.event,
                byte_offset=transition.offset,
                context=context,
            )
            combined.hits.extend(evaluation.hits)
            combined.suppressed.extend(evaluation.suppressed)
            combined.candidate_rules.update(evaluation.candidate_rules)
        return combined


def _base_context(message: ParsedMessage) -> dict[str, Any]:
    names = [name.casefold() for name, _value in message.headers]
    values: dict[str, list[str]] = defaultdict(list)
    for name, value in message.headers:
        values[name.casefold()].append(value)
    duplicate = any(len(items) > 1 for items in values.values())
    return {
        "header_names": set(names),
        "header_values": dict(values),
        "duplicate_header": duplicate,
        "framing": {
            value.casefold()
            for value in message.interpretation.get("framing_candidates", [message.framing])
        },
        "anomalies": {item.casefold() for item in message.anomalies},
        "transfer_encoding": {item.casefold() for item in message.transfer_encoding},
        "trailer_present": bool(message.trailers),
        "method": message.method,
        "request_target": message.target,
        "chunk_zero": 0 in message.chunk_sizes,
        "chunk_extension": any(
            bool(item.get("extension")) for item in message.interpretation.get("chunks", [])
        ),
    }
