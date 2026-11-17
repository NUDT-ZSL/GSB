from __future__ import annotations

from copy import deepcopy

from supply_diag.diagnostics import DiagnosticEngine
from supply_diag.generator import _alpha
from supply_diag.manifest import parse_manifest
from supply_diag.report import build_report


def _projection(result: dict) -> list[tuple]:
    return [
        (
            component["component_id"],
            component["version_status"],
            component["integrity_status"],
            component["hash"]["status"],
            component["error_bounds"]["version"]["cumulative_width"],
            [
                (item["source"], tuple(item["path"]))
                for item in component["signature"]["missing_by"]
            ],
        )
        for component in result["components"]
    ]


def test_component_and_dependency_reordering_preserves_decisions() -> None:
    original = _alpha(1234)
    reversed_manifest = deepcopy(original)
    reversed_manifest["components"] = list(reversed(reversed_manifest["components"]))
    for component in reversed_manifest["components"]:
        component["dependencies"] = list(reversed(component["dependencies"]))

    first = DiagnosticEngine().diagnose(parse_manifest(original)).to_dict()
    second = DiagnosticEngine().diagnose(parse_manifest(reversed_manifest)).to_dict()
    assert _projection(first) == _projection(second)
    assert first["cycles"] == second["cycles"]
    assert first["truth_check"] == second["truth_check"]


def test_manifest_import_order_does_not_change_report_or_run_id() -> None:
    alpha = DiagnosticEngine().diagnose(parse_manifest(_alpha(7))).to_dict()
    beta = DiagnosticEngine().diagnose(parse_manifest(_alpha(7))).to_dict()
    beta["project_id"] = "another-project"
    first = build_report([alpha, beta])
    second = build_report([beta, alpha])
    assert first == second


def test_endpoint_spelling_does_not_change_normalized_decisions() -> None:
    base = _alpha(9)
    alternate = deepcopy(base)
    app = next(item for item in alternate["components"] if item["component_id"] == "app")
    app["dependencies"][0]["constraint"] = ">=1.2.0, <2.0.0"
    app["dependencies"][1]["constraint"] = {"lower": "1.0.0", "upper": "2.0.0"}
    first = DiagnosticEngine().diagnose(parse_manifest(base)).to_dict()
    second = DiagnosticEngine().diagnose(parse_manifest(alternate)).to_dict()
    assert _projection(first) == _projection(second)

