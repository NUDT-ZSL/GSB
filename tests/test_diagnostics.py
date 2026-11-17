from __future__ import annotations

from fractions import Fraction

from supply_diag.diagnostics import DiagnosticEngine
from supply_diag.errors import E_CYCLE, E_HASH_MISMATCH

from .helpers import component, component_result, manifest


def test_hash_validation_sha256_and_hmac() -> None:
    parsed = manifest(
        [
            component("sha", algorithm="sha256"),
            component("hmac", algorithm="hmac-sha256", hmac_key="secret"),
        ]
    )
    result = DiagnosticEngine().diagnose(parsed).to_dict()
    assert component_result(result, "sha")["hash"]["status"] == "valid"
    assert component_result(result, "hmac")["hash"]["status"] == "valid"


def test_hash_mismatch_is_reported_and_blocks_completeness() -> None:
    bad = component("bad", digest="0" * 64)
    parsed = manifest([bad])
    result = DiagnosticEngine().diagnose(parsed).to_dict()
    item = component_result(result, "bad")
    assert item["hash"]["status"] == "mismatch"
    assert item["integrity_status"] == "incomplete"
    assert E_HASH_MISMATCH in {error["code"] for error in result["errors"]}


def test_signature_missing_propagates_to_downstream_consumers() -> None:
    parsed = manifest(
        [
            component(
                "root",
                dependencies=[{"target": "mid", "constraint": ">=1.0.0"}],
            ),
            component(
                "mid",
                dependencies=[{"target": "leaf", "constraint": ">=1.0.0"}],
            ),
            component("leaf", signature_status="unsigned"),
        ]
    )
    result = DiagnosticEngine().diagnose(parsed).to_dict()
    root = component_result(result, "root")
    mid = component_result(result, "mid")
    leaf = component_result(result, "leaf")
    assert root["integrity_status"] == "incomplete"
    assert mid["integrity_status"] == "incomplete"
    assert leaf["integrity_status"] == "incomplete"
    assert root["signature"]["missing_by"] == [
        {
            "source": "leaf",
            "source_status": "unsigned",
            "path": ["leaf", "mid", "root"],
        }
    ]


def test_unsigned_consumer_does_not_taint_signed_dependency() -> None:
    parsed = manifest(
        [
            component(
                "root",
                signature_status="unsigned",
                dependencies=[{"target": "lib", "constraint": ">=1.0.0"}],
            ),
            component("lib"),
        ]
    )
    result = DiagnosticEngine().diagnose(parsed).to_dict()
    assert component_result(result, "root")["integrity_status"] == "incomplete"
    assert component_result(result, "lib")["integrity_status"] == "complete"


def test_unresolved_dependency_blocks_completeness() -> None:
    parsed = manifest(
        [
            component(
                "root",
                dependencies=[{"target": "absent", "constraint": ">=1.0.0"}],
            )
        ]
    )
    result = DiagnosticEngine().diagnose(parsed).to_dict()
    assert component_result(result, "root")["integrity_status"] == "incomplete"


def test_cycle_detection_is_canonical_and_order_independent() -> None:
    components = [
        component(
            "c",
            dependencies=[{"target": "a", "constraint": "1.0.0"}],
        ),
        component(
            "a",
            dependencies=[{"target": "b", "constraint": "1.0.0"}],
        ),
        component(
            "b",
            dependencies=[{"target": "c", "constraint": "1.0.0"}],
        ),
        component("self", dependencies=[{"target": "self", "constraint": "1.0.0"}]),
    ]
    first = DiagnosticEngine().diagnose(manifest(components)).to_dict()
    second = DiagnosticEngine().diagnose(manifest(list(reversed(components)))).to_dict()
    assert first["cycles"] == second["cycles"]
    assert first["cycles"] == [
        {"nodes": ["a", "b", "c"], "cycle": ["a", "b", "c", "a"], "length": 3},
        {"nodes": ["self"], "cycle": ["self", "self"], "length": 1},
    ]
    assert any(error["code"] == E_CYCLE for error in first["errors"])
    assert all(
        component_result(first, component_id)["integrity_status"] == "incomplete"
        for component_id in ("a", "b", "c", "self")
    )


def test_cumulative_version_error_propagates_along_longest_chain() -> None:
    parsed = manifest(
        [
            component(
                "root",
                dependencies=[{"target": "mid", "constraint": "[1.0.0,2.0.0)"}],
            ),
            component(
                "mid",
                dependencies=[{"target": "leaf", "constraint": ">=1.0.0 <2.0.0"}],
            ),
            component("leaf"),
        ]
    )
    result = DiagnosticEngine().diagnose(parsed).to_dict()
    expected = {"root": 1, "mid": 2, "leaf": 3}
    for component_id, width in expected.items():
        item = component_result(result, component_id)
        bound = item["error_bounds"]["version"]["cumulative_width"]
        assert Fraction(bound["numerator"], bound["denominator"]) == Fraction(width)
    assert component_result(result, "leaf")["error_bounds"]["version"]["path"] == [
        "root",
        "mid",
        "leaf",
    ]


def test_uncertain_and_unsatisfiable_are_distinct() -> None:
    uncertain = manifest(
        [
            component("a", dependencies=[{"target": "t", "constraint": "<=1.0.0"}]),
            component("b", dependencies=[{"target": "t", "constraint": ">1.0.0"}]),
            component("t"),
        ]
    )
    uncertain_result = DiagnosticEngine().diagnose(uncertain).to_dict()
    assert component_result(uncertain_result, "t")["version_status"] == "uncertain"
    assert len(uncertain_result["uncertain_items"]) == 1

    unsatisfiable = manifest(
        [
            component("a", dependencies=[{"target": "t", "constraint": "<=1.0.0"}]),
            component("b", dependencies=[{"target": "t", "constraint": ">=3.0.0"}]),
            component("t"),
        ]
    )
    unsatisfiable_result = DiagnosticEngine().diagnose(unsatisfiable).to_dict()
    assert (
        component_result(unsatisfiable_result, "t")["version_status"]
        == "unsatisfiable"
    )
    assert unsatisfiable_result["uncertain_items"] == []

