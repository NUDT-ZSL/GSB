"""Plain data models and deterministic sample loading.

Every identifier is a plain string; a field is uniquely identified by the
composite key ``(table, name)`` rendered as ``table.name``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path


def field_ref(table: str, name: str) -> str:
    """Render the globally unique field identifier used on every edge."""
    return f"{table}.{name}"


def split_ref(ref: str) -> tuple[str, str]:
    table, _, name = ref.partition(".")
    return table, name


@dataclass(frozen=True)
class Field:
    table: str
    name: str
    data_type: str
    deprecated: bool = False

    @property
    def ref(self) -> str:
        return field_ref(self.table, self.name)

    @classmethod
    def from_dict(cls, payload: dict) -> "Field":
        return cls(
            table=payload["table"],
            name=payload["name"],
            data_type=payload["data_type"],
            deprecated=bool(payload.get("deprecated", False)),
        )


@dataclass(frozen=True)
class Edge:
    """A field-level lineage edge (upstream field -> downstream field)."""

    edge_id: str
    source_ref: str
    target_ref: str
    origin: str = "sample"

    @classmethod
    def from_dict(cls, payload: dict) -> "Edge":
        return cls(
            edge_id=payload["edge_id"],
            source_ref=payload["source_ref"],
            target_ref=payload["target_ref"],
            origin=payload.get("origin", "sample"),
        )


@dataclass(frozen=True)
class Rule:
    """One migration rule.

    ``rule_type`` is one of rename / split / merge / type_change / deprecate.
    Optional ``seq`` and ``depends_on`` express intra-version ordering.
    """

    rule_id: str
    rule_type: str
    source_ref: str | None = None
    target_ref: str | None = None
    new_type: str | None = None
    targets: tuple[str, ...] = ()
    weights: tuple[int, ...] = ()
    sources: tuple[str, ...] = ()
    seq: int | None = None
    depends_on: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, payload: dict) -> "Rule":
        return cls(
            rule_id=payload["rule_id"],
            rule_type=payload["rule_type"],
            source_ref=payload.get("source_ref"),
            target_ref=payload.get("target_ref"),
            new_type=payload.get("new_type"),
            targets=tuple(payload.get("targets", [])),
            weights=tuple(payload.get("weights", [])),
            sources=tuple(payload.get("sources", [])),
            depends_on=tuple(payload.get("depends_on", [])),
            seq=payload.get("seq"),
        )

    def touches(self) -> set[str]:
        """Field refs this rule reads or writes, used for order-conflict checks."""
        refs: set[str] = set()
        if self.source_ref:
            refs.add(self.source_ref)
        if self.target_ref:
            refs.add(self.target_ref)
        refs.update(self.targets)
        refs.update(self.sources)
        return refs


@dataclass
class Sample:
    """All deterministic inputs loaded from local JSON files."""

    fields: list[Field] = dataclass_field(default_factory=list)
    edges: list[Edge] = dataclass_field(default_factory=list)
    versions: dict[int, list[Rule]] = dataclass_field(default_factory=dict)

    @classmethod
    def load(cls, sample_dir: str | Path) -> "Sample":
        sample_dir = Path(sample_dir)
        fields = [Field.from_dict(item) for item in json.loads((sample_dir / "fields.json").read_text(encoding="utf-8"))]
        edges = [Edge.from_dict(item) for item in json.loads((sample_dir / "edges.json").read_text(encoding="utf-8"))]
        raw_rules = json.loads((sample_dir / "rules.json").read_text(encoding="utf-8"))
        versions: dict[int, list[Rule]] = {}
        for item in raw_rules:
            version = int(item["version"])
            versions.setdefault(version, []).extend(Rule.from_dict(rule) for rule in item["rules"])
        sample = cls(fields=fields, edges=edges, versions=versions)
        sample.validate_unique_inputs()
        return sample

    def validate_unique_inputs(self) -> None:
        field_refs = [field.ref for field in self.fields]
        if len(set(field_refs)) != len(field_refs):
            raise ValueError("duplicate field in fields.json")
        edge_ids = [edge.edge_id for edge in self.edges]
        if len(set(edge_ids)) != len(edge_ids):
            raise ValueError("duplicate edge_id in edges.json")

    def ordered_versions(self) -> list[int]:
        return sorted(self.versions)
