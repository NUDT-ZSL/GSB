from __future__ import annotations

import json
from pathlib import Path

from smuggling_engine.closure import compute_invalid_closure, find_cycles
from smuggling_engine.generator import generate_sample
from smuggling_engine.rules import load_rule_view


def _paths(sample):
    directory = Path(sample["directory"])
    paths = sample["manifest"]["paths"]
    return (
        directory / paths["rules_v1"],
        [directory / item for item in paths["v2_shards"]],
        directory / paths["v2_database"],
    )


def test_sync_truth_authority_matches_merged_view(standard_sample):
    v1, shards, database = _paths(standard_sample)
    view = load_rule_view(v1_path=v1, v2_shards=shards, v2_database=database)
    assert not view.rejects
    for marker in standard_sample["truth"]["rule_authority"]:
        entity = view.entities[marker["canonical_name"]]
        assert entity.winner.source == marker["expected_source"]
        assert entity.winner.generation == marker["generation"]
        assert entity.winner.effective_at_ms == marker["effective_at_ms"]
        assert entity.winner.direction == marker["direction"]
        assert entity.winner.tombstone == marker["tombstone"]
    assert view.entities["cl-te"].winner.tombstone
    assert view.entities["cl-te.strict"].winner.generation == 3
    assert len(view.entities["cl-te"].history) == 2
    assert [record.source for record in view.entities["cl-te"].history] == [
        "rules_v1",
        "rules_v2",
    ]


def test_v1_clock_is_the_only_relative_time_baseline(standard_sample):
    v1, shards, database = _paths(standard_sample)
    view = load_rule_view(v1_path=v1, v2_shards=shards, v2_database=database)
    document = json.loads(Path(v1).read_text(encoding="utf-8"))
    clock = document["observed_clock_ms"]
    trailing = next(
        record
        for record in view.entities["trailing"].history
        if record.source == "rules_v1"
    )
    assert trailing.effective_at_ms == clock + 12_000


def test_tombstone_and_dependency_closure(standard_sample):
    v1, shards, database = _paths(standard_sample)
    view = load_rule_view(v1_path=v1, v2_shards=shards, v2_database=database)
    closure = compute_invalid_closure(view)
    assert closure.cycle == ()
    assert list(closure.invalid_rules) == standard_sample["truth"]["expected_invalid_rules"]
    assert closure.reasons["cl-te"] == ["tombstoned"]
    assert closure.reasons["dup-header.audit"] == [
        "transitive_dependency_invalid"
    ]
    assert "disabled-rule" in closure.direct_invalid


def test_closure_is_independent_of_shard_import_order(standard_sample):
    v1, shards, database = _paths(standard_sample)
    forward = load_rule_view(v1_path=v1, v2_shards=shards, v2_database=database)
    reverse = load_rule_view(
        v1_path=v1, v2_shards=list(reversed(shards)), v2_database=database
    )
    assert compute_invalid_closure(forward).invalid_rules == compute_invalid_closure(
        reverse
    ).invalid_rules
    assert find_cycles(forward) == find_cycles(reverse)
    assert {
        name: entity.winner.to_dict() for name, entity in forward.entities.items()
    } == {
        name: entity.winner.to_dict() for name, entity in reverse.entities.items()
    }


def test_snapshot_change_forces_replacement_and_reports_dimensions(standard_sample):
    v1, shards, database = _paths(standard_sample)
    original = load_rule_view(v1_path=v1, v2_shards=shards, v2_database=database)
    document = json.loads(Path(v1).read_text(encoding="utf-8"))
    document["rules"] = document["rules"][:-1]
    changed_path = Path(standard_sample["directory"]) / "changed_v1.json"
    changed_path.write_text(json.dumps(document), encoding="utf-8")
    changed = load_rule_view(
        v1_path=changed_path,
        v2_shards=shards,
        v2_database=database,
        previous_view=original,
    )
    assert changed.replaced
    assert "snapshot_replaced" in changed.changed_dimensions
    assert "rule_removed" in changed.changed_dimensions
    assert changed.version != original.version


def test_invalid_v2_record_is_structured_and_other_records_survive(tmp_path):
    v1 = tmp_path / "v1.json"
    v1.write_text(
        json.dumps(
            {
                "observed_clock_ms": 123000,
                "rules": [
                    {
                        "rule_name": "BASE",
                        "effective_after_seconds": 1,
                        "match": "base",
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    shard = tmp_path / "part.json"
    shard.write_text(
        json.dumps(
            [
                {
                    "canonical_name": "bad",
                    "effective_at_ms": -1,
                    "generation": 2,
                    "direction": "request",
                    "tombstone": False,
                    "depends_on": ["missing"],
                    "match": "bad",
                },
                {
                    "canonical_name": "good",
                    "effective_at_ms": 124000,
                    "generation": 2,
                    "direction": "request",
                    "tombstone": False,
                    "depends_on": [],
                    "match": "good",
                },
            ]
        ),
        encoding="utf-8",
    )
    view = load_rule_view(v1_path=v1, v2_shards=[shard])
    assert "bad" not in view.entities
    assert "good" in view.entities
    reject = next(item for item in view.rejects if item.code == "E_V2_RULE_FIELD")
    assert reject.source == "rules_v2"
    assert reject.record_index == 1


def test_conflicting_v2_authority_is_rejected_without_order_bias(tmp_path):
    shard_one = tmp_path / "a.json"
    shard_two = tmp_path / "b.json"
    base = {
        "canonical_name": "same",
        "effective_at_ms": 1000,
        "generation": 2,
        "direction": "request",
        "tombstone": False,
        "depends_on": [],
    }
    shard_one.write_text(
        json.dumps([{**base, "match": "first"}]), encoding="utf-8"
    )
    shard_two.write_text(
        json.dumps([{**base, "match": "second"}]), encoding="utf-8"
    )
    forward = load_rule_view(v1_path=None, v2_shards=[shard_one, shard_two])
    reverse = load_rule_view(v1_path=None, v2_shards=[shard_two, shard_one])
    assert "same" not in forward.entities
    assert "same" not in reverse.entities
    assert [item.code for item in forward.rejects].count(
        "E_V2_AUTHORITY_CONFLICT"
    ) == 1
    assert [item.code for item in reverse.rejects].count(
        "E_V2_AUTHORITY_CONFLICT"
    ) == 1


def test_cycle_returns_stable_names(cycle_sample):
    v1, shards, database = _paths(cycle_sample)
    view = load_rule_view(v1_path=v1, v2_shards=shards, v2_database=database)
    closure = compute_invalid_closure(view)
    assert closure.cycle == ("cycle.a", "cycle.b")
    assert closure.to_dict()["cycle"] == {
        "code": "E_RULE_CYCLE",
        "rules": ["cycle.a", "cycle.b"],
    }


def test_v1_without_declared_clock_is_rejected_and_never_uses_system_time(
    tmp_path,
):
    path = tmp_path / "v1.json"
    path.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "rule_name": "X",
                        "effective_after_seconds": 3,
                        "match": "x",
                        "enabled": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    view = load_rule_view(v1_path=path)
    assert "x" not in view.entities
    assert any(item.code == "E_V1_MISSING_CLOCK" for item in view.rejects)

