from __future__ import annotations

from pathlib import Path

from smuggling_engine.engine import Engine, EngineConfig
from smuggling_engine.generator import verify_report_against_truth
from smuggling_engine.parsing import group_connections, parse_messages
from smuggling_engine.rules import load_rule_view


def test_invalidated_connections_only_are_reparsed_and_match_full(standard_sample):
    directory = Path(standard_sample["directory"])
    paths = standard_sample["manifest"]["paths"]
    messages, rejects = parse_messages(directory / paths["messages"])
    groups, _ = group_connections(messages)
    baseline_view = load_rule_view(v1_path=directory / paths["rules_v1"])
    new_view = load_rule_view(
        v1_path=directory / paths["rules_v1"],
        v2_shards=[directory / item for item in paths["v2_shards"]],
        v2_database=directory / paths["v2_database"],
    )
    engine = Engine(EngineConfig(concurrency=2, per_connection_timeout_ms=None))
    baseline = engine.run(groups, baseline_view, rejected=rejects)
    consistent, incremental, full = engine.verify_incremental(
        groups, new_view, baseline, rejected=rejects
    )
    assert consistent
    assert incremental.deterministic_dict() == full.deterministic_dict()
    assert set(incremental.metadata["recomputed_connections"]) == {
        "conn-01-clte",
        "conn-02-duplicate-conflict",
        "conn-03-chunk-hex",
        "conn-10-nested-clte",
    }
    assert set(incremental.metadata["reused_connections"]) == {
        "conn-04-trailing",
        "conn-05-clean",
        "conn-06-mixup-a",
        "conn-07-mixup-b",
        "conn-08-gap",
        "conn-09-response",
    }
    assert set(incremental.closure["affected_connections"]) == {
        "conn-01-clte",
        "conn-02-duplicate-conflict",
        "conn-10-nested-clte",
    }
    affected = {
        item.connection_id
        for item in incremental.connections
        if item.affected_by_invalid_rules
    }
    assert affected == set(incremental.closure["affected_connections"])


def test_rule_cycle_returns_all_cycle_names(cycle_sample):
    directory = Path(cycle_sample["directory"])
    paths = cycle_sample["manifest"]["paths"]
    messages, rejects = parse_messages(directory / paths["messages"])
    groups, _ = group_connections(messages)
    view = load_rule_view(
        v1_path=directory / paths["rules_v1"],
        v2_shards=[directory / item for item in paths["v2_shards"]],
        v2_database=directory / paths["v2_database"],
    )
    report = Engine(EngineConfig(concurrency=2)).run(
        groups, view, rejected=rejects
    )
    assert report.closure["cycle"] == {
        "code": "E_RULE_CYCLE",
        "rules": ["cycle.a", "cycle.b"],
    }
    assert {item.verdict for item in report.connections} == {"E_RULE_CYCLE"}
    assert verify_report_against_truth(
        report.to_dict(), cycle_sample["truth"]
    ) == {"ok": True, "mismatches": []}

