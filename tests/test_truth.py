from __future__ import annotations

from copy import deepcopy
import json

from supply_diag.diagnostics import DiagnosticEngine
from supply_diag.generator import generate_samples
from supply_diag.manifest import parse_manifest, parse_manifest_file


def test_generated_truth_markers_are_consistent(tmp_path) -> None:
    generate_samples(tmp_path)
    for path in sorted((tmp_path / "manifests").glob("*.json")):
        parsed = parse_manifest_file(path)
        truth = DiagnosticEngine().diagnose(parsed).to_dict()["truth_check"]
        assert truth["inconsistent_count"] == 0
        assert truth["mismatch_count"] == 0
        assert truth["consistent_count"] == truth["checked_components"]


def test_tampered_sync_marker_is_located_and_counted(tmp_path) -> None:
    generate_samples(tmp_path)
    data = json.loads(
        (tmp_path / "manifests/delta-secure.json").read_text(encoding="utf-8")
    )
    data = deepcopy(data)
    data["components"][0]["truth"]["sync_marker"] = "0" * 64
    parsed = parse_manifest(data, source_path="<tampered>")
    result = DiagnosticEngine().diagnose(parsed).to_dict()
    truth = result["truth_check"]
    assert truth["inconsistent_count"] == 1
    assert truth["mismatches"][0]["field"] == "sync_marker"
    assert truth["mismatches"][0]["component_id"] == data["components"][0]["component_id"]

