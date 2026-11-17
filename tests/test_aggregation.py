from __future__ import annotations

import json
from itertools import permutations

from audit_forge.engine import AuditEngine
from audit_forge.models import Event
from audit_forge.rules import (
    DEFAULT_RULES_DOCUMENT,
    case_identity,
    parse_rule_document,
)

from .conftest import generate_samples


def _ingest(rows: list[dict], rules=None) -> AuditEngine:
    config = rules or parse_rule_document(DEFAULT_RULES_DOCUMENT)
    engine = AuditEngine(config)
    for index, row in enumerate(rows, start=1):
        engine.ingest_event(Event.from_dict(row), "sample.jsonl", index)
    engine.recompute_cases()
    return engine


def test_shuffled_and_duplicate_import_have_identical_case_identity() -> None:
    ordered = _ingest(generate_samples.clean_events())
    shuffled = _ingest(generate_samples.deterministic_shuffle(
        generate_samples.clean_events(), "clean-v1"
    ))
    duplicates = _ingest(generate_samples.duplicate_events())

    assert case_identity(ordered.cases) == case_identity(shuffled.cases)
    assert case_identity(ordered.cases) == case_identity(duplicates.cases)
    assert len(ordered.cases) == 3
    duplicate_anomalies = [
        anomaly
        for anomaly in duplicates.ingest_anomalies.values()
        if anomaly["code"] == "E_DUPLICATE_EVENT"
    ]
    assert len(duplicate_anomalies) == 2


def test_rule_window_changes_rebuild_cases() -> None:
    default = _ingest(generate_samples.clean_events())
    short = parse_rule_document(
        {
            "version": "short-window-v1",
            "rules": [
                {
                    "id": "same-actor-object",
                    "match_fields": ["actor", "object"],
                    "window_ms": 150,
                }
            ],
        }
    )
    rebuilt = _ingest(generate_samples.clean_events(), rules=short)
    assert len(default.cases) == 3
    assert rebuilt.cases == []
    assert case_identity(default.cases) != case_identity(rebuilt.cases)


def test_first_event_wins_for_conflicting_event_id() -> None:
    first = generate_samples.event(
        "same-id", "trace-a", "alice", "read", "doc-1", 1000, 1
    )
    conflict = dict(first)
    conflict["actor"] = "mallory"
    engine = AuditEngine(parse_rule_document(DEFAULT_RULES_DOCUMENT))
    assert engine.ingest_event(Event.from_dict(first), "x", 1)
    assert not engine.ingest_event(Event.from_dict(conflict), "x", 2)
    assert engine.events["same-id"].actor == "alice"
    anomaly = next(
        item
        for item in engine.ingest_anomalies.values()
        if item["code"] == "E_EVENT_CONFLICT"
    )
    assert anomaly["conflicting_fields"] == ["actor"]


def test_every_legal_arrival_permutation_has_identical_case_identity() -> None:
    rows = [
        generate_samples.event(
            "p1", "trace-a", "alice", "read", "doc-1", 100, 1
        ),
        generate_samples.event(
            "p2", "trace-a", "alice", "read", "doc-1", 150, 2, "p1"
        ),
        generate_samples.event(
            "p3", "trace-b", "alice", "copy", "doc-1", 200, 1
        ),
        generate_samples.event(
            "p4", "trace-c", "bob", "read", "doc-2", 1000, 1
        ),
    ]
    identities = {
        json.dumps(
            case_identity(_ingest(list(order)).cases),
            sort_keys=True,
        )
        for order in permutations(rows)
    }
    assert len(identities) == 1
    identity = json.loads(next(iter(identities)))
    assert identity[0]["event_ids"] == ["p1", "p2", "p3"]

