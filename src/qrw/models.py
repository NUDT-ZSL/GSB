from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class CompilerConfig:
    default_weight: float = 1.0
    default_enabled: bool = True
    default_max_depth: int = 1
    max_depth_limit: int = 4
    slot_points: float = 0.25
    depth_penalty: float = 0.05
    slot_name_types: tuple[tuple[str, str], ...] = (
        ("area", "AREA"),
        ("city", "GEO"),
        ("intent", "INTENT"),
        ("property", "PROPERTY"),
    )

    def __post_init__(self) -> None:
        if self.default_weight <= 0:
            raise ValueError("default_weight must be positive")
        if self.default_max_depth < 1:
            raise ValueError("default_max_depth must be at least 1")
        if self.max_depth_limit < 1:
            raise ValueError("max_depth_limit must be at least 1")
        if self.default_max_depth > self.max_depth_limit:
            raise ValueError("default_max_depth cannot exceed max_depth_limit")
        if self.slot_points < 0 or self.depth_penalty < 0:
            raise ValueError("scoring coefficients cannot be negative")
        for name, slot_type in self.slot_name_types:
            if not name or not slot_type:
                raise ValueError("slot_name_types entries must be non-empty")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["slot_name_types"] = dict(self.slot_name_types)
        return value

    def inferred_slot_type(self, name: str) -> str | None:
        return dict(self.slot_name_types).get(name)


@dataclass
class SourceLocation:
    dialect: str
    source: str
    line: int
    column: int = 1
    field: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SlotDeclaration:
    name: str
    slot_type: str
    source: SourceLocation
    declared: bool
    origin: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.slot_type,
            "declared": self.declared,
            "origin": self.origin,
            "source": self.source.to_dict(),
        }


@dataclass
class FillRecord:
    rule_id: str
    source_line: int
    field: str
    default: Any
    origin: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ParsedRule:
    rule_id: str
    kind: str
    pattern_text: str | None
    replacement_text: str | None
    replacement_nodes: list[dict[str, Any]] | None
    slots: list[SlotDeclaration]
    weight: float
    enabled: bool
    max_depth: int
    priority: int
    source: SourceLocation
    fills: list[FillRecord] = field(default_factory=list)


@dataclass
class ParsedTemplateSet:
    dialect: str
    source_name: str
    rules: list[ParsedRule]
    slot_types: dict[str, Any]
    raw_text: str
    fills: list[FillRecord] = field(default_factory=list)

