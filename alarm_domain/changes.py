"""Changeset parsing and structural validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import spec_error
from .util import display_file, locate_line, read_text


_RULE_OPS = {"add", "update", "delete"}
_EDGE_OPS = {"add", "update", "delete"}


@dataclass(frozen=True)
class ChangeSet:
    changeset_id: str
    rule_changes: tuple[dict[str, Any], ...]
    edge_changes: tuple[dict[str, Any], ...]
    source_file: str = "<api>"

    def to_dict(self) -> dict[str, Any]:
        return {
            "changeset_id": self.changeset_id,
            "rules": [dict(change) for change in self.rule_changes],
            "edges": [dict(change) for change in self.edge_changes],
        }


def _reject_constant(token: str) -> None:
    raise ValueError(f"non-finite JSON constant {token}")


def _rule_target(change: dict[str, Any], line: int, source: str) -> str:
    op = change.get("op")
    if op not in _RULE_OPS:
        raise spec_error(
            "rule change op must be add, update or delete",
            file=source,
            line=line,
            detail={"op": op},
        )
    if op == "add":
        rule = change.get("rule")
        if not isinstance(rule, dict):
            raise spec_error(
                "add rule change requires a rule object", file=source, line=line
            )
        target = rule.get("rule_id")
    else:
        target = change.get("rule_id")
        if op == "update" and not isinstance(change.get("changes"), dict):
            raise spec_error(
                "update rule change requires a changes object", file=source, line=line
            )
    if not isinstance(target, str) or not target.strip():
        raise spec_error("rule change target must be non-empty", file=source, line=line)
    return target


def _edge_target(change: dict[str, Any], line: int, source: str) -> tuple[str, str]:
    op = change.get("op")
    if op not in _EDGE_OPS:
        raise spec_error(
            "edge change op must be add, update or delete",
            file=source,
            line=line,
            detail={"op": op},
        )
    edge = change.get("edge")
    if not isinstance(edge, dict):
        raise spec_error(
            "edge change requires an edge object", file=source, line=line
        )
    upstream = edge.get("upstream")
    downstream = edge.get("downstream")
    if not isinstance(upstream, str) or not isinstance(downstream, str):
        raise spec_error(
            "edge change requires string upstream and downstream",
            file=source,
            line=line,
        )
    if op == "update" and "weight" not in edge:
        raise spec_error(
            "update edge change requires edge.weight", file=source, line=line
        )
    return upstream, downstream


def changeset_from_dict(document: Any, *, source_file: str = "<api>") -> ChangeSet:
    if not isinstance(document, dict):
        raise spec_error("changeset must be a JSON object", file=source_file, line=1)
    changeset_id = document.get("changeset_id")
    if not isinstance(changeset_id, str) or not changeset_id.strip():
        raise spec_error(
            "changeset_id must be a non-empty string", file=source_file, line=1
        )
    raw_rule_changes = document.get("rules", [])
    raw_edge_changes = document.get("edges", [])
    if not isinstance(raw_rule_changes, list) or not isinstance(raw_edge_changes, list):
        raise spec_error(
            "changeset rules and edges must be arrays", file=source_file, line=1
        )

    rule_changes: list[dict[str, Any]] = []
    seen_rules: set[str] = set()
    for index, raw_change in enumerate(raw_rule_changes):
        line = index + 1
        if not isinstance(raw_change, dict):
            raise spec_error(
                f"rules[{index}] must be an object", file=source_file, line=line
            )
        if raw_change.get("op") == "add" and isinstance(raw_change.get("rule"), dict):
            target = _rule_target(raw_change, line, source_file)
            line = locate_line(
                json.dumps(document, ensure_ascii=False), json.dumps(target), default=line
            )
        else:
            target = _rule_target(raw_change, line, source_file)
        if target in seen_rules:
            raise spec_error(
                f"duplicate change for rule {target}",
                file=source_file,
                line=line,
                detail={"rule_id": target},
            )
        seen_rules.add(target)
        rule_changes.append(dict(raw_change))

    edge_changes: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str]] = set()
    for index, raw_change in enumerate(raw_edge_changes):
        line = index + 1
        if not isinstance(raw_change, dict):
            raise spec_error(
                f"edges[{index}] must be an object", file=source_file, line=line
            )
        target_edge = _edge_target(raw_change, line, source_file)
        if target_edge in seen_edges:
            raise spec_error(
                f"duplicate change for edge {target_edge[0]}->{target_edge[1]}",
                file=source_file,
                line=line,
            )
        seen_edges.add(target_edge)
        edge_changes.append(dict(raw_change))

    return ChangeSet(
        changeset_id=changeset_id,
        rule_changes=tuple(rule_changes),
        edge_changes=tuple(edge_changes),
        source_file=source_file,
    )


def load_changeset(path: str | Path) -> ChangeSet:
    source = Path(path)
    try:
        text = read_text(source)
    except OSError as exc:
        raise spec_error(
            f"cannot read changeset: {exc}", file=display_file(source), line=1
        ) from exc
    try:
        document = json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise spec_error(
            f"invalid changeset JSON: {exc.msg}",
            file=display_file(source),
            line=exc.lineno,
        ) from exc
    except ValueError as exc:
        raise spec_error(
            f"invalid changeset JSON: {exc}", file=display_file(source), line=1
        ) from exc
    return changeset_from_dict(document, source_file=display_file(source))
