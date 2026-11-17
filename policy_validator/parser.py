"""Declaration parsing, validation and normalization (requirement 1).

Invalid entries produce rejection records carrying the entry index and
business error codes; parsing always continues with the remaining entries.
Normalized rules keep their original raw declaration for reproducibility.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import errors
from .condition import ConditionError, parse_condition
from .models import NormalizedRule, Rejection

TARGET_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
RULE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")

PRIORITY_MIN = 1
PRIORITY_MAX = 10000

ACTIONS = ("allow", "deny", "audit")


@dataclass
class ParseResult:
    rules: list[NormalizedRule] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    policy_version: str = "0.0.0"


def _validate_entry(index: int, entry: Any, seen_ids: set[str]):
    codes: list[str] = []
    details: list[str] = []
    rule_id = entry.get("id") if isinstance(entry, dict) else None

    if not isinstance(entry, dict):
        return None, Rejection(index, None, (errors.E_MISSING_FIELD,), "entry is not an object")

    for key in ("id", "target", "action", "condition", "priority", "version"):
        if key not in entry:
            codes.append(errors.E_MISSING_FIELD)
            details.append(f"missing field '{key}'")

    if "id" in entry:
        if not isinstance(rule_id, str) or not RULE_ID_RE.match(rule_id):
            codes.append(errors.E_MISSING_FIELD)
            details.append("rule id must match " + RULE_ID_RE.pattern)
        elif rule_id in seen_ids:
            codes.append(errors.E_DUPLICATE_RULE_ID)
            details.append(f"duplicate rule id '{rule_id}'")

    target = entry.get("target")
    if "target" in entry and (not isinstance(target, str) or not TARGET_RE.match(target)):
        codes.append(errors.E_INVALID_TARGET)
        details.append(f"invalid target {target!r}")

    action = entry.get("action")
    if "action" in entry and action not in ACTIONS:
        codes.append(errors.E_INVALID_ACTION)
        details.append(f"action must be one of {ACTIONS}")

    dnf = None
    if "condition" in entry:
        try:
            dnf = parse_condition(entry.get("condition"))
        except ConditionError as exc:
            codes.append(exc.code)
            details.append(exc.detail)

    priority = entry.get("priority")
    if "priority" in entry:
        if not isinstance(priority, int) or isinstance(priority, bool) \
                or not (PRIORITY_MIN <= priority <= PRIORITY_MAX):
            codes.append(errors.E_PRIORITY_RANGE)
            details.append(f"priority must be an integer in [{PRIORITY_MIN}, {PRIORITY_MAX}]")

    version = entry.get("version")
    if "version" in entry and (not isinstance(version, str) or not VERSION_RE.match(version)):
        codes.append(errors.E_VERSION_FORMAT)
        details.append(f"invalid version format {version!r}")

    depends_on = entry.get("depends_on", [])
    if not isinstance(depends_on, list) or not all(isinstance(d, str) for d in depends_on):
        codes.append(errors.E_MISSING_FIELD)
        details.append("depends_on must be a list of rule ids")
        depends_on = []

    if codes:
        uniq = tuple(sorted(set(codes)))
        return None, Rejection(index, rule_id if isinstance(rule_id, str) else None,
                               uniq, "; ".join(details))

    seen_ids.add(rule_id)
    rule = NormalizedRule(
        rule_id=rule_id,
        target=target,
        action=action,
        priority=priority,
        version=version,
        depends_on=tuple(sorted(set(depends_on) - {rule_id})),
        condition_text=entry["condition"],
        dnf=dnf,
        raw=dict(entry),
    )
    return rule, None


def parse_policies(doc: dict[str, Any]) -> ParseResult:
    result = ParseResult()
    if not isinstance(doc, dict) or not isinstance(doc.get("rules"), list):
        raise ValueError("policy document must be an object with a 'rules' list")
    result.policy_version = str(doc.get("policy_version", "0.0.0"))
    seen_ids: set[str] = set()
    for index, entry in enumerate(doc["rules"]):
        rule, rejection = _validate_entry(index, entry, seen_ids)
        if rule is not None:
            result.rules.append(rule)
        if rejection is not None:
            result.rejections.append(rejection)
    # Normalized output is order-independent: rules are keyed by id.
    result.rules.sort(key=lambda r: r.rule_id)
    return result
