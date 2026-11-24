from __future__ import annotations

from smuggle_detector.models import RuleRecord
from smuggle_detector.rules import build_rule_catalog


def rule(
    name: str,
    generation: int,
    effective_ms: int,
    *,
    direction: str = "both",
    patterns: tuple[str, ...] = ("CL_TE",),
    tombstone: bool = False,
    enabled: bool = True,
    index: int = 0,
) -> RuleRecord:
    return RuleRecord(
        canonical_name=name,
        generation=generation,
        effective_ms=effective_ms,
        direction=direction,
        patterns=patterns,
        tombstone=tombstone,
        enabled=enabled,
        source_category="test",
        record_index=index,
    )


def test_generation_snapshots_replace_wholesale_and_never_mix() -> None:
    catalog = build_rule_catalog(
        [
            rule("RULE_A", 1, 0),
            rule("RULE_B", 1, 0, patterns=("DUP_HEADER_CONFLICT",)),
            rule("RULE_A", 2, 1000, direction="s2c"),
        ]
    )
    early = catalog.select(500)
    late = catalog.select(1500)
    assert early.generation == 1
    assert {item.canonical_name for item in early.rules} == {"RULE_A", "RULE_B"}
    assert late.generation == 2
    assert {item.canonical_name for item in late.rules} == {"RULE_A"}
    assert late.rules[0].direction == "s2c"
    assert early.fingerprint != late.fingerprint


def test_tombstone_wins_timestamp_tie_and_removes_rule() -> None:
    catalog = build_rule_catalog(
        [
            rule("RULE_A", 1, 0),
            rule(
                "RULE_A",
                2,
                1000,
                patterns=(),
                tombstone=True,
                enabled=False,
                index=1,
            ),
        ]
    )
    view = catalog.select(2000)
    assert view.generation == 2
    assert view.rules == ()


def test_direction_or_rule_set_change_changes_fingerprint() -> None:
    base = [rule("RULE_A", 2, 1000), rule("RULE_B", 2, 1000)]
    changed_direction = [
        rule("RULE_A", 2, 1000, direction="c2s"),
        rule("RULE_B", 2, 1000),
    ]
    changed_set = [rule("RULE_A", 2, 1000)]
    first = build_rule_catalog(base)
    second = build_rule_catalog(changed_direction)
    third = build_rule_catalog(changed_set)
    assert first.fingerprint != second.fingerprint
    assert first.fingerprint != third.fingerprint


def test_repeated_high_authority_record_wins_within_generation() -> None:
    catalog = build_rule_catalog(
        [
            rule("RULE_A", 2, 1000, patterns=("CL_TE",)),
            rule("RULE_A", 2, 2000, patterns=("DUP_HEADER_CONFLICT",)),
        ]
    )
    view = catalog.select(3000)
    assert view.rules[0].effective_ms == 2000
    assert view.rules[0].patterns == ("DUP_HEADER_CONFLICT",)

