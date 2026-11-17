from __future__ import annotations

import copy

from sensitive_flow.engine import (
    analyze_state,
    build_diagnostic_report,
    build_graph_data,
    build_state,
    replay,
)
from sensitive_flow.errors import E_DANGLING_MASK, E_MASK_BYPASS


def _analysis(samples: dict, snapshot_count: int) -> dict:
    state = build_state({"snapshots": samples["snapshots"]["snapshots"][:snapshot_count]})
    return analyze_state(state)[0]


def test_v1_v2_propagation_and_path_format_change(samples: dict) -> None:
    truth = samples["truth"]
    for count, label in ((1, "v1"), (2, "v2"), (3, "v3")):
        analysis = _analysis(samples, count)
        expected = truth["expected_by_version"][label]
        assert analysis["field_levels"] == expected["effective_levels"]

        observed_violations = [
            {
                "code": item["code"],
                "field_key": item["field_key"],
                "source_key": item["source_key"],
            }
            for item in analysis["violations"]
        ]
        assert observed_violations == expected["violations"]

    v2 = _analysis(samples, 2)
    v3 = _analysis(samples, 3)
    order_v2 = next(item for item in v2["fields"] if item["key"] == "orders[0].contact")
    order_v3 = next(item for item in v3["fields"] if item["key"] == "orders[0].contact")
    assert order_v2["path"] == "orders.0.contact"
    assert order_v3["path"] == "orders[0].contact"
    assert order_v3["path_history"] == [
        "orders[0].contact",
        "orders.0.contact",
        "orders[0].contact",
    ]


def test_v1_target_has_no_sensitivity_extension(samples: dict) -> None:
    analysis = _analysis(samples, 1)
    assert analysis["field_levels"]["customer.phone"] == 2
    assert analysis["field_levels"]["reports.phoneCopy"] == 0
    profile = next(item for item in analysis["derivations"] if item["id"] == "d-v1-phone-direct")
    assert profile["version"] == "v1"
    assert profile["transitive"] is False
    assert profile["propagation_enabled"] is False


def test_v2_nontransitive_source_does_not_raise_target(samples: dict) -> None:
    analysis = _analysis(samples, 2)
    assert analysis["field_levels"]["analytics.profile"] == 3
    assert analysis["field_levels"]["analytics.safeHash"] == 0
    edge = next(
        item for item in analysis["derivations"] if item["id"] == "d-v2-copy-nontrans"
    )
    assert edge["version"] == "v2"
    assert edge["transitive"] is False
    assert edge["propagation_enabled"] is False


def test_sample_truth_matches_every_declared_target(samples: dict) -> None:
    analysis = _analysis(samples, 2)
    declarations = [
        declaration
        for snapshot in samples["snapshots"]["snapshots"][:2]
        for declaration in snapshot["derivations"]
    ]
    for declaration in declarations:
        sync = declaration["version_sync"]
        assert sync["declaration_version"] == declaration["version"]
        assert sync["rule_version"] == samples["truth"]["rule_version"]
        expected = declaration["truth"]["expected_effective_target_level"]
        assert analysis["field_levels"][declaration["target"]] == expected


def test_mask_bypass_has_complete_upstream_path(samples: dict) -> None:
    analysis = _analysis(samples, 2)
    violation = next(item for item in analysis["violations"] if item["code"] == E_MASK_BYPASS)
    assert violation["field_key"] == "reports.phoneCopy"
    assert ["customer.phone", "reports.phoneCopy"] in violation["upstream_paths"]


def test_deleted_masked_source_reports_dangling(samples: dict) -> None:
    analysis = _analysis(samples, 3)
    dangling = [item for item in analysis["violations"] if item["code"] == E_DANGLING_MASK]
    assert {item["field_key"] for item in dangling} == {
        "analytics.audit",
        "reports.phoneCopy",
    }
    for violation in dangling:
        assert ["customer.phone", violation["field_key"]] in violation["upstream_paths"]


def test_invalid_entries_continue_and_are_indexed(samples: dict) -> None:
    state = build_state(samples["invalid_snapshots"])
    documents = {item["document_id"]: item for item in analyze_state(state)}

    invalid = documents["DOC-INVALID"]
    assert invalid["field_levels"]["good.a"] == 1
    assert invalid["field_levels"]["good.b"] == 1
    assert {item["code"] for item in invalid["rejections"]} == {
        "E_PATH",
        "E_PATH_INDEX",
        "E_LEVEL",
        "E_FIELD_DUPLICATE",
        "E_DERIVE_VERSION",
        "E_DERIVE_TARGET",
        "E_DERIVE_SOURCE",
    }
    for rejection in invalid["rejections"]:
        assert rejection["document_id"] == "DOC-INVALID"
        assert isinstance(rejection["item_index"], int)
        assert rejection["entry_number"] == rejection["item_index"] + 1

    assert documents["DOC-BAD-VERSION"]["rejections"][0]["code"] == "E_SNAPSHOT_VERSION"
    assert documents["DOC-BAD-VERSION"]["field_levels"] == {"x": 1}
    assert documents["DOC-ORDER"]["rejections"][0]["code"] == "E_SNAPSHOT_ORDER"
    assert documents["DOC-CONTINUED"]["field_levels"] == {"only.field": 1}


def test_cycle_rejection_contains_canonical_sequence(samples: dict) -> None:
    state = build_state(samples["cycle_snapshots"])
    analysis = analyze_state(state)[0]
    rejection = next(item for item in analysis["rejections"] if item["code"] == "E_DERIVE_CYCLE")
    cycle = rejection["details"]["cycle"]
    assert cycle[0] == cycle[-1]
    assert set(cycle) == {"a", "b", "c"}
    assert len(analysis["derivations"]) == 2


def test_report_and_graph_have_stable_shapes(samples: dict) -> None:
    state = replay(samples["snapshots"], samples["edits"])
    report_one = build_diagnostic_report(state)
    report_two = build_diagnostic_report(copy.deepcopy(state))
    assert report_one == report_two
    assert {
        "schema_version",
        "rule_version",
        "status",
        "summary",
        "recovery",
        "documents",
        "rejections",
        "state_hash",
    }.issubset(report_one)
    graph = build_graph_data(state)
    assert graph["summary"] == {"documents": 1, "nodes": 9, "edges": 4, "cycles": 0}
    assert graph["documents"][0]["nodes"] == sorted(
        graph["documents"][0]["nodes"], key=lambda item: item["id"]
    )
