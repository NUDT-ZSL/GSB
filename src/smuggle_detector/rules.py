"""Rule snapshot normalization, authority selection and generation views."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .constants import RULE_SEMANTICS_VERSION
from .io_utils import digest_value
from .models import RuleRecord


def _record_semantic_key(record: RuleRecord) -> tuple:
    return (
        record.generation,
        record.canonical_name,
        record.effective_ms,
        record.direction,
        record.patterns,
        record.tombstone,
        record.enabled,
    )


def _authority_key(record: RuleRecord) -> tuple:
    return (
        record.effective_ms,
        1 if record.tombstone or not record.enabled else 0,
        record.direction,
        record.patterns,
        record.canonical_name,
        record.enabled,
    )


@dataclass(frozen=True)
class RuleSnapshot:
    generation: int
    activation_ms: int
    fingerprint: str
    records: tuple[RuleRecord, ...]
    active_rules: tuple[RuleRecord, ...]

    def to_doc(self) -> dict:
        return {
            "generation": self.generation,
            "activation_ms": self.activation_ms,
            "fingerprint": self.fingerprint,
            "record_count": len(self.records),
            "active_rule_count": len(self.active_rules),
            "rules": [record.semantic_doc() for record in self.active_rules],
        }


@dataclass(frozen=True)
class RuleView:
    generation: int
    activation_ms: int
    fingerprint: str
    rules: tuple[RuleRecord, ...]

    def to_doc(self) -> dict:
        return {
            "generation": self.generation,
            "activation_ms": self.activation_ms,
            "fingerprint": self.fingerprint,
            "rule_count": len(self.rules),
            "rules": [record.canonical_name for record in self.rules],
        }

    def rules_for_direction(self, direction: str) -> tuple[RuleRecord, ...]:
        return tuple(
            record
            for record in self.rules
            if record.direction in ("both", direction)
        )


EMPTY_VIEW = RuleView(
    generation=0,
    activation_ms=0,
    fingerprint=digest_value(
        {
            "semantics_version": RULE_SEMANTICS_VERSION,
            "generation": 0,
            "rules": [],
        }
    ),
    rules=(),
)


class RuleCatalog:
    """A set of complete, mutually exclusive generation snapshots.

    Each generation is treated as a complete replacement boundary.  Within a
    generation, the highest effective timestamp wins for a canonical name; a
    tombstone wins an exact timestamp tie.  A connection selects exactly one
    snapshot, so records from two generations are never executed together.
    """

    def __init__(self, snapshots: Iterable[RuleSnapshot]) -> None:
        self.snapshots = tuple(sorted(snapshots, key=lambda item: item.generation))
        self.fingerprint = digest_value(
            {
                "semantics_version": RULE_SEMANTICS_VERSION,
                "snapshots": [
                    {
                        "generation": snapshot.generation,
                        "activation_ms": snapshot.activation_ms,
                        "fingerprint": snapshot.fingerprint,
                    }
                    for snapshot in self.snapshots
                ],
            }
        )

    def select(self, connection_time_ms: int) -> RuleView:
        eligible = [
            snapshot
            for snapshot in self.snapshots
            if snapshot.activation_ms <= connection_time_ms
        ]
        if not eligible:
            return EMPTY_VIEW
        snapshot = max(eligible, key=lambda item: item.generation)
        return RuleView(
            generation=snapshot.generation,
            activation_ms=snapshot.activation_ms,
            fingerprint=snapshot.fingerprint,
            rules=snapshot.active_rules,
        )

    def to_doc(self) -> dict:
        return {
            "semantics_version": RULE_SEMANTICS_VERSION,
            "catalog_fingerprint": self.fingerprint,
            "snapshots": [snapshot.to_doc() for snapshot in self.snapshots],
        }


def build_rule_catalog(records: Iterable[RuleRecord]) -> RuleCatalog:
    by_generation: dict[int, list[RuleRecord]] = {}
    for record in records:
        by_generation.setdefault(record.generation, []).append(record)

    snapshots: list[RuleSnapshot] = []
    for generation in sorted(by_generation):
        generation_records = sorted(
            by_generation[generation],
            key=_record_semantic_key,
        )
        authority: dict[str, RuleRecord] = {}
        for record in sorted(generation_records, key=_authority_key):
            authority[record.canonical_name] = record

        active = tuple(
            sorted(
                (
                    record
                    for record in authority.values()
                    if record.enabled
                    and not record.tombstone
                    and bool(record.patterns)
                ),
                key=lambda item: item.canonical_name,
            )
        )
        all_records = tuple(
            sorted(authority.values(), key=lambda item: item.canonical_name)
        )
        activation_ms = max(
            (record.effective_ms for record in generation_records),
            default=0,
        )
        fingerprint = digest_value(
            {
                "semantics_version": RULE_SEMANTICS_VERSION,
                "generation": generation,
                "records": [record.semantic_doc() for record in generation_records],
            }
        )
        snapshots.append(
            RuleSnapshot(
                generation=generation,
                activation_ms=activation_ms,
                fingerprint=fingerprint,
                records=all_records,
                active_rules=active,
            )
        )
    return RuleCatalog(snapshots)

