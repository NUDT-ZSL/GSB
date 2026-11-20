"""Immutable sample and class models."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Mapping


@dataclass(frozen=True)
class WredConfig:
    min_bytes: int
    max_bytes: int
    max_probability_ppm: int


@dataclass(frozen=True)
class ClassConfig:
    name: str
    parent: str | None
    cir_bps: int
    pir_bps: int
    committed_burst_bytes: int
    peak_burst_bytes: int
    queue_depth_bytes: int
    drop_policy: str
    wred: WredConfig | None
    weight: int
    guarantee_bps: int | None
    order: int
    children: tuple[str, ...] = ()

    @property
    def is_leaf(self) -> bool:
        return not self.children


@dataclass(frozen=True)
class ClassifierRule:
    rule_id: str
    kind: str
    target: str
    dscp: int | None = None
    five_tuple: tuple[tuple[str, Any], ...] | None = None


@dataclass(frozen=True)
class ArrivalEvent:
    seq: int
    bytes: int
    arrival_ms: int
    class_label: str
    resolved_class: str
    due_step: int
    dscp: int | None
    five_tuple: tuple[tuple[str, Any], ...] | None
    classifier_rule_id: str | None


@dataclass(frozen=True)
class Sample:
    sample_id: str
    injection_id: str
    seed: int
    step_ms: int
    total_steps: int
    window_ms: int
    throughput_tolerance_bps: Fraction
    schedule_tolerance_bytes: Fraction
    initial_fill: str
    classes: tuple[ClassConfig, ...]
    classifiers: tuple[ClassifierRule, ...]
    events: tuple[ArrivalEvent, ...]
    event_schedule: tuple[ArrivalEvent, ...]
    sample_digest: str
    config_digest: str
    arrivals_digest: str
    raw_package: Mapping[str, Any]

    @property
    def class_map(self) -> dict[str, ClassConfig]:
        return {item.name: item for item in self.classes}

    @property
    def roots(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.classes if item.parent is None)

    @property
    def leaves(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.classes if item.is_leaf)

    @property
    def children_map(self) -> dict[str, tuple[str, ...]]:
        return {item.name: item.children for item in self.classes}

    def ancestors(self, class_name: str) -> tuple[str, ...]:
        result: list[str] = []
        current = self.class_map[class_name].parent
        while current is not None:
            result.append(current)
            current = self.class_map[current].parent
        return tuple(reversed(result))

    def path(self, class_name: str) -> tuple[str, ...]:
        return self.ancestors(class_name) + (class_name,)

