from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .models import Event
from .util import sha256_value, short_hash

ALLOWED_MATCH_FIELDS = {"actor", "action", "object", "trace_id"}


class RuleConfigError(ValueError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = "E_RULES"
        self.details = details


@dataclass(frozen=True, slots=True)
class CorrelationRule:
    rule_id: str
    match_fields: tuple[str, ...]
    window_ms: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.rule_id,
            "match_fields": list(self.match_fields),
            "window_ms": self.window_ms,
        }


@dataclass(frozen=True, slots=True)
class RuleConfig:
    version: str
    fingerprint: str
    rules: tuple[CorrelationRule, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "fingerprint": self.fingerprint,
            "rules": [rule.as_dict() for rule in self.rules],
        }


DEFAULT_RULES_DOCUMENT: dict[str, Any] = {
    "version": "default-v1",
    "rules": [
        {
            "id": "same-actor-object",
            "match_fields": ["actor", "object"],
            "window_ms": 5000,
        }
    ],
}


def _parse_rule(value: Any, index: int) -> CorrelationRule:
    if not isinstance(value, dict):
        raise RuleConfigError(
            "each rule must be an object",
            rule_index=index,
        )

    rule_id = value.get("id", f"rule-{index + 1}")
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise RuleConfigError(
            "rule id must be a non-empty string",
            rule_index=index,
        )

    match_fields = value.get("match_fields")
    if (
        not isinstance(match_fields, list)
        or not match_fields
        or any(not isinstance(field, str) for field in match_fields)
    ):
        raise RuleConfigError(
            "match_fields must be a non-empty string list",
            rule_id=rule_id,
        )
    unsupported = sorted(set(match_fields) - ALLOWED_MATCH_FIELDS)
    if unsupported:
        raise RuleConfigError(
            "rule uses unsupported match fields",
            rule_id=rule_id,
            fields=unsupported,
        )
    if len(set(match_fields)) != len(match_fields):
        raise RuleConfigError(
            "match_fields must not contain duplicates",
            rule_id=rule_id,
        )

    window_ms = value.get("window_ms", 5000)
    if (
        not isinstance(window_ms, int)
        or isinstance(window_ms, bool)
        or window_ms < 0
    ):
        raise RuleConfigError(
            "window_ms must be a non-negative integer",
            rule_id=rule_id,
        )

    return CorrelationRule(
        rule_id=rule_id,
        match_fields=tuple(match_fields),
        window_ms=window_ms,
    )


def _normalize_rules(value: Any) -> tuple[CorrelationRule, ...]:
    if not isinstance(value, list):
        raise RuleConfigError("rules must be a list")
    rules = tuple(_parse_rule(rule, index) for index, rule in enumerate(value))
    rule_ids = [rule.rule_id for rule in rules]
    if len(set(rule_ids)) != len(rule_ids):
        raise RuleConfigError("rule ids must be unique")
    return rules


def parse_rule_document(document: Any) -> RuleConfig:
    if isinstance(document, list):
        version_value = None
        rule_values = document
    elif isinstance(document, dict):
        version_value = document.get("version")
        rule_values = document.get("rules", [])
    else:
        raise RuleConfigError("rule document must be an object or a list")

    rules = _normalize_rules(rule_values)
    fingerprint = sha256_value([rule.as_dict() for rule in rules])
    if version_value is None:
        version = "auto:" + fingerprint.split(":", 1)[1][:16]
    elif isinstance(version_value, bool) or not isinstance(
        version_value, (str, int)
    ):
        raise RuleConfigError("version must be a string or integer")
    else:
        version = str(version_value)

    return RuleConfig(
        version=version,
        fingerprint=fingerprint,
        rules=rules,
    )


def default_rule_config() -> RuleConfig:
    return parse_rule_document(DEFAULT_RULES_DOCUMENT)


def load_rule_config(path: str | Path | None) -> RuleConfig:
    if path is None:
        return default_rule_config()
    rule_path = Path(path)
    try:
        raw = rule_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuleConfigError("cannot read rule file", path=str(rule_path)) from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuleConfigError(
            "rule file is not valid JSON",
            path=str(rule_path),
            line=exc.lineno,
            column=exc.colno,
        ) from exc
    return parse_rule_document(document)


class _DisjointSet:
    def __init__(self, items: list[str]) -> None:
        self.parent = {item: item for item in items}
        self.rank = {item: 0 for item in items}
        self.rule_ids = {item: set() for item in items}

    def find(self, item: str) -> str:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != item:
            parent = self.parent[item]
            self.parent[item] = root
            item = parent
        return root

    def union(self, left: str, right: str, rule_id: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            self.rule_ids[left_root].add(rule_id)
            return
        if self.rank[left_root] < self.rank[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        self.rule_ids[left_root].update(self.rule_ids.pop(right_root))
        self.rule_ids[left_root].add(rule_id)
        if self.rank[left_root] == self.rank[right_root]:
            self.rank[left_root] += 1


def _causal_path(event: Event, by_id: dict[str, Event]) -> list[str]:
    path = [event.event_id]
    seen = {event.event_id}
    current = event
    while current.parent_event_id is not None:
        parent_id = current.parent_event_id
        parent = by_id.get(parent_id)
        if (
            parent is None
            or parent_id in seen
            or parent.trace_id != current.trace_id
        ):
            break
        path.append(parent_id)
        seen.add(parent_id)
        current = parent
    path.reverse()
    return path


def build_cases(events: Iterable[Event], rules: RuleConfig) -> list[dict[str, Any]]:
    event_list = sorted(events, key=lambda event: event.canonical_key)
    by_id = {event.event_id: event for event in event_list}
    event_ids = sorted(by_id)
    if len(event_ids) < 2 or not rules.rules:
        return []

    dsu = _DisjointSet(event_ids)
    for rule in rules.rules:
        groups: dict[tuple[str, ...], list[Event]] = {}
        for event in event_list:
            values = tuple(
                str(getattr(event, field_name))
                for field_name in rule.match_fields
            )
            groups.setdefault(values, []).append(event)

        for group in groups.values():
            if len(group) < 2:
                continue
            group.sort(key=lambda event: (event.ts, event.event_id))
            right = 1
            for left in range(len(group)):
                right = max(right, left + 1)
                while (
                    right < len(group)
                    and group[right].ts - group[left].ts <= rule.window_ms
                ):
                    dsu.union(
                        group[left].event_id,
                        group[right].event_id,
                        rule.rule_id,
                    )
                    right += 1

    components: dict[str, list[str]] = {}
    for event_id in event_ids:
        components.setdefault(dsu.find(event_id), []).append(event_id)

    cases: list[dict[str, Any]] = []
    for root, members in components.items():
        if len(members) < 2:
            continue
        members.sort()
        member_events = [by_id[event_id] for event_id in members]
        canonical_members = sorted(
            member_events, key=lambda event: event.canonical_key
        )
        rule_ids = sorted(dsu.rule_ids.get(root, set()))
        identity = {
            "rule_version": rules.version,
            "rule_fingerprint": rules.fingerprint,
            "rule_ids": rule_ids,
            "event_ids": members,
        }
        case_id = "case-" + short_hash(identity, 24)
        evidence = []
        for event in canonical_members:
            row = event.as_dict()
            row["causal_path"] = _causal_path(event, by_id)
            evidence.append(row)
        cases.append(
            {
                "case_id": case_id,
                "rule_version": rules.version,
                "rule_fingerprint": rules.fingerprint,
                "rule_ids": rule_ids,
                "event_count": len(members),
                "event_ids": members,
                "traces": sorted({event.trace_id for event in member_events}),
                "actors": sorted({event.actor for event in member_events}),
                "objects": sorted({event.object for event in member_events}),
                "first_ts": min(event.ts for event in member_events),
                "last_ts": max(event.ts for event in member_events),
                "evidence": evidence,
            }
        )

    cases.sort(key=lambda item: item["case_id"])
    return cases


def case_identity(cases: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "case_id": case["case_id"],
            "event_ids": case["event_ids"],
            "rule_ids": case["rule_ids"],
        }
        for case in sorted(cases, key=lambda item: item["case_id"])
    ]
